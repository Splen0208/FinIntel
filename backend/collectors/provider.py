# -*- coding: utf-8 -*-
"""依赖发现、校验与启动冻结（指令 §5-C5；增量设计 T08）。

本机已复现的真实失败（不是理论风险）
------------------------------------
``shutil.which("opencli")`` 在 Windows 上命中的是**无后缀 Unix shell shim**，
内容形如::

    exec "$basedir/node" "$basedir/node_modules/@jackwener/opencli/dist/src/main.js" "$@"

Python 的 ``asyncio.create_subprocess_exec`` **无法直接执行**它（既不是 PE，也不是 ``.cmd``）。
历史实现把它当可执行文件直接启动 → 最终拿到的是 opencli 的**命令清单 JSON**，
任务以 ``CONNECTION_UNAVAILABLE`` 结束（真机 run_cc0a4cc5…）。同时也不能假设
"npm 安装必定在 shim 同目录带 ``node.exe``"。

本模块做三件事
--------------
1. **发现 + 校验**：把一个"线索"（配置路径 / PATH 命中 / 常见安装布局）解析成**可信的
   启动组合**：

   - ``exec`` 模式：真实的可执行文件（Windows 上校验 ``MZ`` 魔数）；
   - ``node_entry`` 模式：``node`` 解释器 + JS 入口（两者都必须存在且可读）。

   ``.cmd`` / ``.ps1`` / ``.bat`` / 无后缀 shell shim **只作线索**，绝不直接当可执行文件；
   非 PE 的 ``.exe`` 亦降级为线索继续解析。

2. **冻结**：任务启动时解析一次，把 handle 固定进 ``research_run.config_snapshot.provider``；
   任务期间**不再** ``which``，外部配置后改也不影响本任务（等价 ``channel_state`` 的
   "指纹 + state/checked_at" 模型）。

3. **三种失败分开报**（`G` 与 `C5` 的共同要求）：

   - ``DEPENDENCY_MISSING``——装不上 / 找不到依赖；
   - ``CONNECTION_UNAVAILABLE``——依赖在，但会话/浏览器通道连不上；
   - ``AUTH_REQUIRED``——通道在，但账号未登录。

   绝不合并成一句"采集失败"。

边界（本模块**不做**的事）
--------------------------
- 只做**只读**发现与校验：不安装任何东西、不启动交互式登录、不规避平台风控；
- 不记录、不打印任何密码 / Cookie / token；handle 只存**路径与版本**；
- 测试注入虚拟文件系统（``FileSystem`` 的替身），**不启动任何真实进程**。
"""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from collectors.contracts import CollectorError, ERROR_CODES

# ---------------------------------------------------------------------------
# 平台 / 常量
# ---------------------------------------------------------------------------

KIND_X = "x"
KIND_XHS = "xhs"

# kind → 上游平台标识（出现在 argv/错误码里）
PLATFORM_BY_KIND: dict[str, str] = {KIND_X: "twitter", KIND_XHS: "xiaohongshu"}

# 面向用户的依赖名
DISPLAY_BY_KIND: dict[str, str] = {KIND_X: "twitter-cli", KIND_XHS: "OpenCLI"}

# 启动模式
LAUNCH_EXEC = "exec"              # 直接启动一个真实可执行文件
LAUNCH_NODE_ENTRY = "node_entry"  # node 解释器 + JS 入口

# node 可执行文件名（Windows 带 .exe）
NODE_EXECUTABLE_NAMES: tuple[str, ...] = ("node.exe", "node") if os.name == "nt" else ("node",)

# OpenCLI 的入口相对包目录的路径（本机 1.8.8 实测）
OPENCLI_ENTRY_TAIL: tuple[str, ...] = ("dist", "src", "main.js")
OPENCLI_PACKAGE_PARTS: tuple[str, ...] = ("node_modules", "@jackwener", "opencli")
OPENCLI_ENTRY_REL: tuple[str, ...] = OPENCLI_PACKAGE_PARTS + OPENCLI_ENTRY_TAIL

# 冻结进 ``research_run.config_snapshot.provider`` 的键名
PROVIDER_KEY = "provider"

# 发现候选时使用的可执行文件名（按优先级）
X_WHICH_NAMES: tuple[str, ...] = ("twitter.exe", "twitter")
XHS_WHICH_NAMES: tuple[str, ...] = ("opencli.cmd", "opencli.exe", "opencli.ps1", "opencli")

# 只作"线索"的文件后缀（可能是 shell shim，绝不能直接当可执行文件）
_SHIM_SUFFIXES: frozenset[str] = frozenset({"", ".cmd", ".ps1", ".bat"})

# 三种失败码（必须分开）
FAILURE_DEPENDENCY = "DEPENDENCY_MISSING"
FAILURE_CONNECTION = "CONNECTION_UNAVAILABLE"
FAILURE_AUTH = "AUTH_REQUIRED"
FAILURE_CLASSES: tuple[str, ...] = (FAILURE_DEPENDENCY, FAILURE_CONNECTION, FAILURE_AUTH)

# 兜底断言：错误码必须是 contracts 里的唯一来源
assert FAILURE_DEPENDENCY in ERROR_CODES
assert FAILURE_CONNECTION in ERROR_CODES
assert FAILURE_AUTH in ERROR_CODES


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_kind(kind: object) -> str:
    """把 ``x`` / ``twitter`` / ``xhs`` / ``xiaohongshu`` 归一为内部 kind。"""
    text = str(kind or "").strip().lower()
    if text in ("x", "twitter"):
        return KIND_X
    if text in ("xhs", "xiaohongshu", "redbook", "xiao-hong-shu"):
        return KIND_XHS
    raise CollectorError(
        FAILURE_DEPENDENCY,
        f"未知的采集来源 {kind!r}",
        retryable=False,
        operator_action="只支持 X（twitter-cli）与小红书（OpenCLI）",
    )


def install_hint(kind: str) -> str:
    """给出**可执行**的安装/配置指引（不含任何凭据）。"""
    label = DISPLAY_BY_KIND.get(kind, kind)
    if kind == KIND_X:
        return (f"确认 {label} 已安装（在终端执行 twitter --version 应输出 0.8.x），"
                "或在本项目设置页填写 twitter 可执行文件的**绝对路径**后重试")
    return (f"确认 {label} 已安装（可用 node <安装目录>/node_modules/@jackwener/opencli/"
            "dist/src/main.js --version 自检），或在本项目设置页填写 opencli 的绝对路径；"
            "注意不要填写无后缀的 Unix shell shim（Windows 上无法直接执行）")


# ---------------------------------------------------------------------------
# 只读文件系统抽象（测试注入虚拟 FS，避免依赖真实磁盘）
# ---------------------------------------------------------------------------

class FileSystem:
    """真实文件系统的只读视图；测试可替换为虚拟实现。"""

    def is_file(self, path: os.PathLike[str] | str) -> bool:
        try:
            return Path(path).is_file()
        except OSError:
            return False

    def is_dir(self, path: os.PathLike[str] | str) -> bool:
        try:
            return Path(path).is_dir()
        except OSError:
            return False

    def is_readable(self, path: os.PathLike[str] | str) -> bool:
        try:
            return Path(path).is_file() and os.access(str(path), os.R_OK)
        except OSError:
            return False

    def has_windows_pe_magic(self, path: os.PathLike[str] | str) -> bool:
        """读前两个字节判断是否是 Windows PE（``MZ``）。非 Windows 恒为 False。"""
        if os.name != "nt":
            return False
        try:
            with open(path, "rb") as handle:
                return handle.read(2) == b"MZ"
        except OSError:
            return False


def _default_layout_candidates(kind: str) -> list[Path]:
    """常见安装布局的候选**线索**路径（缺失即跳过，不做任何写入）。"""
    home = Path.home()
    out: list[Path] = []
    if kind == KIND_X:
        out.append(home / ".local" / "bin" / "twitter.exe")
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / "uv" / "tools" / "twitter-cli" / "Scripts" / "twitter.exe")
            out.append(Path(appdata) / "uv" / "tools" / "twitter-cli" / "Scripts" / "twitter")
    elif kind == KIND_XHS:
        # 独立安装用于当前 Chrome 扩展；旧 WorkBuddy 内置安装仅作后备。
        out.append(home / ".opencli" / "node_modules" / "@jackwener" / "opencli")
        node_versions = home / ".workbuddy" / "binaries" / "node" / "versions"
        try:
            version_dirs = sorted(
                (p for p in node_versions.iterdir() if p.is_dir()), reverse=True)
        except OSError:
            version_dirs = []
        for version_dir in version_dirs:
            out.append(version_dir / "opencli.cmd")
            out.append(version_dir / "opencli")
            out.append(version_dir / "node_modules" / "@jackwener" / "opencli")
    return out


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderHandle:
    """一次**冻结**的采集依赖启动组合。

    - ``exec``：``argv_prefix() == [exe_path]``；
    - ``node_entry``：``argv_prefix() == [node_path, entry_path]``。

    只含**路径与版本**，绝不含凭据。
    """

    kind: str
    display_name: str
    platform: str
    launch_mode: str
    exe_path: str
    node_path: Optional[str] = None
    entry_path: Optional[str] = None
    version: Optional[str] = None
    session_key: str = "default"
    verified: bool = False
    resolved_at: Optional[str] = None
    discovery_source: str = ""

    def argv_prefix(self) -> list[str]:
        """启动该依赖所需的 argv 前缀（后续由调用方追加子命令）。"""
        if self.launch_mode == LAUNCH_NODE_ENTRY:
            if not self.node_path or not self.entry_path:
                raise CollectorError(
                    FAILURE_DEPENDENCY,
                    f"{self.display_name} 的 node 入口未解析完整，拒绝启动",
                    operator_action=install_hint(self.kind),
                )
            return [str(self.node_path), str(self.entry_path)]
        if not self.exe_path:
            raise CollectorError(
                FAILURE_DEPENDENCY,
                f"{self.display_name} 的可执行文件为空，拒绝启动",
                operator_action=install_hint(self.kind),
            )
        return [str(self.exe_path)]

    def fingerprint(self) -> str:
        """启动组合指纹：任一维变化即"新组合"（与 channel_state 同口径）。"""
        payload = "\x1f".join((
            self.kind, self.launch_mode, self.exe_path or "",
            self.node_path or "", self.entry_path or "", self.version or "",
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def as_view(self) -> dict[str, Any]:
        """普通视图：只有事实，**不含任何路径**。"""
        return {
            "kind": self.kind,
            "platform": self.platform,
            "launch_mode": self.launch_mode,
            "version": self.version,
            "verified": bool(self.verified),
            "fingerprint": self.fingerprint(),
        }

    def as_diagnostic_view(self) -> dict[str, Any]:
        """诊断视图：额外给出路径（路径不是凭据）。"""
        view = self.as_view()
        view.update({
            "exe_path": self.exe_path,
            "node_path": self.node_path,
            "entry_path": self.entry_path,
            "discovery_source": self.discovery_source,
            "resolved_at": self.resolved_at,
        })
        return view

    def as_snapshot(self) -> dict[str, Any]:
        """冻结进 ``config_snapshot.provider`` 的形态（含路径，供本任务复用）。"""
        return {
            "kind": self.kind,
            "platform": self.platform,
            "launch_mode": self.launch_mode,
            "exe_path": self.exe_path,
            "node_path": self.node_path,
            "entry_path": self.entry_path,
            "version": self.version,
            "session_key": self.session_key,
            "verified": bool(self.verified),
            "fingerprint": self.fingerprint(),
            "discovery_source": self.discovery_source,
            "frozen_at": self.resolved_at,
        }


def handle_from_snapshot(payload: Optional[Mapping[str, Any]]) -> ProviderHandle:
    """从冻结快照还原 handle（缺字段按空值归一，绝不回落全局设置）。"""
    data = dict(payload or {})
    kind = str(data.get("kind") or "")
    return ProviderHandle(
        kind=kind,
        display_name=DISPLAY_BY_KIND.get(kind, kind or "unknown"),
        platform=str(data.get("platform") or PLATFORM_BY_KIND.get(kind, "")),
        launch_mode=str(data.get("launch_mode") or LAUNCH_EXEC),
        exe_path=str(data.get("exe_path") or ""),
        node_path=(str(data["node_path"]) if data.get("node_path") else None),
        entry_path=(str(data["entry_path"]) if data.get("entry_path") else None),
        version=(str(data["version"]) if data.get("version") else None),
        session_key=str(data.get("session_key") or "default"),
        verified=bool(data.get("verified")),
        resolved_at=(str(data["frozen_at"]) if data.get("frozen_at") else None),
        discovery_source=str(data.get("discovery_source") or ""),
    )


@dataclass(frozen=True)
class ProviderStatus:
    """依赖 / 连接 / 认证三层状态；``.error()`` 把它们**分开**映射成错误码。

    - ``dependency_ok=False`` → ``DEPENDENCY_MISSING``；
    - ``connection_ok is False`` → ``CONNECTION_UNAVAILABLE``；
    - ``authenticated is False`` → ``AUTH_REQUIRED``；
    - 否则 ``None``（未发现问题；``None`` 表示"未知"，不是"通过"）。
    """

    kind: str
    dependency_ok: bool = True
    connection_ok: Optional[bool] = None
    authenticated: Optional[bool] = None
    detail: str = ""

    def error(self) -> Optional[CollectorError]:
        return classify_failure(
            self.kind,
            dependency_ok=self.dependency_ok,
            connection_ok=self.connection_ok,
            authenticated=self.authenticated,
            detail=self.detail,
        )


def classify_failure(
    kind: object,
    *,
    dependency_ok: bool = True,
    connection_ok: Optional[bool] = None,
    authenticated: Optional[bool] = None,
    detail: str = "",
) -> Optional[CollectorError]:
    """把三层状态映射为**互不混淆**的 ``CollectorError``；无问题返回 ``None``。

    顺序固定：依赖 → 连接 → 认证（依赖没装就谈不上连接/登录）。
    """
    resolved = normalize_kind(kind)
    label = DISPLAY_BY_KIND[resolved]
    note = f"（{detail}）" if detail else ""
    if not dependency_ok:
        return CollectorError(
            FAILURE_DEPENDENCY,
            f"{label} 依赖不可用{note}",
            retryable=False,
            operator_action=install_hint(resolved),
        )
    if connection_ok is False:
        return CollectorError(
            FAILURE_CONNECTION,
            f"{label} 依赖已就绪，但采集会话/浏览器通道未连接{note}",
            retryable=True,
            operator_action="在已授权的浏览器里连接对应的采集通道后重试；本步骤不会自动打开或切换浏览器",
        )
    if authenticated is False:
        return CollectorError(
            FAILURE_AUTH,
            f"{label} 采集通道可用，但账号未登录或登录态失效{note}",
            retryable=False,
            operator_action="在已授权的浏览器里完成一次登录后重试",
        )
    return None


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------

class ProviderResolver:
    """发现 → 校验 → 冻结。

    默认走真实文件系统；测试注入 ``fs`` / ``which`` / ``layout_candidates``，
    **不启动任何进程**。
    """

    def __init__(
        self,
        *,
        fs: Optional[FileSystem] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        layout_candidates: Optional[Callable[[str], Sequence[Path]]] = None,
        version_probe: Optional[Callable[[ProviderHandle], tuple[bool, Optional[str]]]] = None,
        extra_search_dirs: Sequence[Path] = (),
        session_key: str = "default",
        os_name: Optional[str] = None,
        max_node_search_depth: int = 8,
    ) -> None:
        self.fs = fs or FileSystem()
        self._which = which or shutil.which
        self._layout_candidates = layout_candidates or _default_layout_candidates
        self.version_probe = version_probe
        self.extra_search_dirs = tuple(Path(p) for p in extra_search_dirs)
        self.session_key = str(session_key or "default")
        self.os_name = os.name if os_name is None else os_name
        self.max_node_search_depth = max(1, int(max_node_search_depth))

    # -- 公共入口 ---------------------------------------------------------

    def resolve(self, kind: object, *, configured: Optional[str] = None) -> ProviderHandle:
        """发现并校验一个可信的启动组合；失败抛 ``DEPENDENCY_MISSING``。"""
        resolved = normalize_kind(kind)
        tried: list[str] = []
        rejections: list[str] = []
        for path, source in self._candidate_paths(resolved, configured):
            tried.append(f"{path}（来源：{source}）")
            candidate = self._resolve_candidate(resolved, path, source)
            if candidate is None:
                rejections.append(f"{path}：无法解析出可信启动组合")
                continue
            if not self.verify(candidate):
                rejections.append(f"{path}：校验未通过（缺失/不可读/非可执行）")
                continue
            version: Optional[str] = None
            if self.version_probe is not None:
                ok, version = self.version_probe(candidate)
                if not ok:
                    rejections.append(f"{path}：--version 探针失败")
                    continue
            return replace(candidate, verified=True, version=version)
        raise self._dependency_missing(resolved, tried, rejections, configured)

    def verify(self, handle: ProviderHandle) -> bool:
        """结构性校验：启动所需的每个文件都必须存在且可读。"""
        if handle.launch_mode == LAUNCH_NODE_ENTRY:
            if not handle.node_path or not handle.entry_path:
                return False
            return (self._readable(handle.node_path) and self._readable(handle.entry_path))
        if not handle.exe_path:
            return False
        if not self._readable(handle.exe_path):
            return False
        # Windows 上可执行文件必须有 PE 魔数；否则只是被误当成可执行文件的 shim。
        if self.os_name == "nt" and not self.fs.has_windows_pe_magic(handle.exe_path):
            return False
        return True

    def freeze_into(self, run_id: str, handle: ProviderHandle) -> dict[str, Any]:
        """把 handle 冻结进 ``research_run.config_snapshot.provider``（唯一出口）。"""
        import store

        row = store.query_one("SELECT config_snapshot FROM research_run WHERE id=?", (run_id,))
        snapshot = store.jloads(row["config_snapshot"] if row else None, {}) or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot[PROVIDER_KEY] = handle.as_snapshot()
        snapshot["provider_frozen_at"] = handle.resolved_at or _utcnow()
        store.execute("UPDATE research_run SET config_snapshot=? WHERE id=?",
                      (store.jdumps(snapshot), run_id))
        return snapshot[PROVIDER_KEY]

    # -- 候选发现 ---------------------------------------------------------

    def _which_names(self, kind: str) -> tuple[str, ...]:
        return X_WHICH_NAMES if kind == KIND_X else XHS_WHICH_NAMES

    def _candidate_paths(self, kind: str, configured: Optional[str]) -> list[tuple[Path, str]]:
        seen: set[str] = set()
        out: list[tuple[Path, str]] = []

        def add(candidate: object, source: str) -> None:
            if candidate is None:
                return
            text = str(candidate).strip()
            if not text:
                return
            path = Path(text)
            key = os.path.normcase(str(path))
            if key in seen:
                return
            seen.add(key)
            out.append((path, source))

        add(configured, "configured")
        for name in self._which_names(kind):
            add(self._which(name), "path")
        for base in self.extra_search_dirs:
            for name in self._which_names(kind):
                add(base / name, "extra_dir")
        for path in self._layout_candidates(kind):
            add(path, "layout")
        return out

    def _readable(self, path: object) -> bool:
        return bool(path) and self.fs.is_file(path) and self.fs.is_readable(path)

    def _find_node(self, start: object) -> Optional[str]:
        """从 ``start`` 起向上有限层查找 node 解释器；找不到再回退 ``which``。"""
        base = Path(str(start))
        chain = [base, *list(base.parents)][: self.max_node_search_depth]
        for directory in chain:
            for name in NODE_EXECUTABLE_NAMES:
                candidate = directory / name
                if self._readable(candidate) and self._looks_like_binary(candidate):
                    return str(candidate)
        for name in NODE_EXECUTABLE_NAMES:
            found = self._which(name)
            if found and self._readable(found):
                return str(found)
        return None

    def _looks_like_binary(self, path: object) -> bool:
        """Windows 上必须带 PE 魔数；其它平台只要求可读（node 自身不是 PE）。"""
        if self.os_name == "nt":
            return self.fs.has_windows_pe_magic(path)
        return True

    def _opencli_node_entry(self, clue: Path) -> Optional[tuple[str, str]]:
        """把一条线索解析成 ``(node, entry)``；解析不到返回 None。

        覆盖两种布局：

        1. 线索是 shim / ``.cmd`` / ``.ps1``（其**同目录**或祖先目录含 node 与
           ``node_modules/@jackwener/opencli/dist/src/main.js``）；
        2. 线索**本身就是包目录** ``.../@jackwener/opencli``（入口在其 ``dist/src/main.js``）。
        """
        entry_candidates: list[Path] = []
        if self.fs.is_dir(clue):
            entry_candidates.append(clue.joinpath(*OPENCLI_ENTRY_TAIL))
        entry_candidates.append(clue.parent.joinpath(*OPENCLI_ENTRY_REL))
        for entry in entry_candidates:
            if not self._readable(entry):
                continue
            node = self._find_node(entry)
            if node is not None:
                return (node, str(entry))
        return None

    def _resolve_candidate(self, kind: str, path: Path,
                           source: str) -> Optional[ProviderHandle]:
        """把一个候选解析成 handle；无法构造可信组合返回 None。"""
        suffix = path.suffix.lower()

        # 1) 直接给出 JS 入口 → node + js
        if suffix == ".js":
            if not self._readable(path):
                return None
            node = self._find_node(path.parent)
            if node is None:
                return None
            return self._build(kind, LAUNCH_NODE_ENTRY, exe_path=str(path),
                               node_path=node, entry_path=str(path), source=source)

        # 2) 真实可执行文件（PE）→ 直接 exec
        if suffix == ".exe" or suffix not in _SHIM_SUFFIXES:
            if self._readable(path) and self._looks_like_binary(path):
                return self._build(kind, LAUNCH_EXEC, exe_path=str(path), source=source)
            # 非 PE 的 .exe / 其它后缀 → 继续当线索解析（不直接启动）

        # 3) shim / .cmd / .ps1 / .bat / 非 PE 文件 → 只作线索
        combo = self._opencli_node_entry(path)
        if combo is not None:
            node, entry = combo
            return self._build(kind, LAUNCH_NODE_ENTRY, exe_path=str(path),
                               node_path=node, entry_path=entry, source=source)
        return None

    def _build(self, kind: str, launch_mode: str, *, exe_path: str,
               source: str, node_path: Optional[str] = None,
               entry_path: Optional[str] = None) -> ProviderHandle:
        return ProviderHandle(
            kind=kind,
            display_name=DISPLAY_BY_KIND.get(kind, kind),
            platform=PLATFORM_BY_KIND.get(kind, ""),
            launch_mode=launch_mode,
            exe_path=exe_path,
            node_path=node_path,
            entry_path=entry_path,
            session_key=self.session_key,
            verified=False,
            resolved_at=_utcnow(),
            discovery_source=source,
        )

    def _dependency_missing(self, kind: str, tried: Sequence[str],
                            rejections: Sequence[str],
                            configured: Optional[str]) -> CollectorError:
        label = DISPLAY_BY_KIND.get(kind, kind)
        detail = "；".join(list(rejections)[:5]) if rejections else "未发现任何候选安装位置"
        return CollectorError(
            FAILURE_DEPENDENCY,
            f"未找到可用的 {label} 采集依赖：{detail}",
            retryable=False,
            operator_action=install_hint(kind),
            evidence={
                "kind": kind,
                "configured": bool(configured),
                "tried": list(tried)[:8],
                "rejections": list(rejections)[:8],
            },
        )


# ---------------------------------------------------------------------------
# 默认探针（生产用；测试注入替身，不真跑进程）
# ---------------------------------------------------------------------------

def default_version_probe(handle: ProviderHandle, *, timeout_seconds: int = 15
                          ) -> tuple[bool, Optional[str]]:
    """执行 ``<启动前缀> --version`` 并解析出形如 ``1.8.8`` 的版本号。

    只在**生产**解析路径使用；单测一律注入替身，不启动真实进程。
    子进程环境走 ``upstream_config.sanitized_child_env()``（去除回环代理）。
    """
    import re
    import subprocess

    from upstream_config import sanitized_child_env

    try:
        argv = handle.argv_prefix() + ["--version"]
    except CollectorError:
        return (False, None)
    try:
        completed = subprocess.run(
            argv, capture_output=True, timeout=max(1, int(timeout_seconds)),
            env=sanitized_child_env(), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return (False, None)
    text = (completed.stdout or b"")[:4096].decode("utf-8", "replace")
    text += (completed.stderr or b"")[:4096].decode("utf-8", "replace")
    match = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    if match:
        return (True, match.group(0))
    return (completed.returncode == 0, None)


# ---------------------------------------------------------------------------
# 模块级单例 + 便捷函数（供 social_pipeline / app 接线使用）
# ---------------------------------------------------------------------------

RESOLVER = ProviderResolver()


def resolve_provider(kind: object, *, configured: Optional[str] = None,
                     version_probe: Optional[Callable[[ProviderHandle],
                                                      tuple[bool, Optional[str]]]] = None
                     ) -> ProviderHandle:
    """模块级便捷入口：解析 + 冻结前的发现。"""
    resolver = RESOLVER if version_probe is None else ProviderResolver(version_probe=version_probe)
    return resolver.resolve(kind, configured=configured)


def resolve_opencli_launch(configured: Optional[str] = None) -> ProviderHandle:
    """小红书（OpenCLI）启动组合：``collectors/runtime.py`` 用它替换旧硬假设。"""
    return RESOLVER.resolve(KIND_XHS, configured=configured)


def is_real_executable(path: os.PathLike[str] | str) -> bool:
    """真实可执行体：可读，且（Windows 上）带 PE 魔数。

    无后缀 shell shim / ``.cmd`` / ``.ps1`` 一律不满足（Windows 上不是 PE）。
    """
    fs = RESOLVER.fs
    candidate = Path(path)
    if not (fs.is_file(candidate) and fs.is_readable(candidate)):
        return False
    if os.name == "nt":
        return fs.has_windows_pe_magic(candidate)
    return True


def resolve_twitter_launch(configured: Optional[str] = None) -> ProviderHandle:
    """X（twitter-cli）启动组合。"""
    return RESOLVER.resolve(KIND_X, configured=configured)


def handle_for_run(run: Optional[Mapping[str, Any]]) -> Optional[ProviderHandle]:
    """从任务快照读取**冻结**的 handle；未冻结返回 None（绝不重新发现）。"""
    snapshot = (run or {}).get("config_snapshot") or {}
    payload = snapshot.get(PROVIDER_KEY) if isinstance(snapshot, Mapping) else None
    if not isinstance(payload, Mapping) or not payload.get("exe_path"):
        return None
    return handle_from_snapshot(payload)


def freeze_into(run_id: str, handle: ProviderHandle) -> dict[str, Any]:
    """模块级便捷入口：把 handle 冻结进 ``research_run.config_snapshot.provider``。"""
    return RESOLVER.freeze_into(run_id, handle)


def require_frozen_handle(run: Optional[Mapping[str, Any]], kind: object) -> ProviderHandle:
    """任务必须使用冻结的 handle；未冻结抛 ``PROVIDER_NOT_FROZEN``。"""
    resolved = normalize_kind(kind)
    handle = handle_for_run(run)
    if handle is None or handle.kind != resolved:
        label = DISPLAY_BY_KIND[resolved]
        raise CollectorError(
            "PROVIDER_NOT_FROZEN",
            f"任务未冻结 {label} 的启动组合，拒绝发起采集",
            retryable=False,
            operator_action="先由任务启动流程解析并冻结 provider，再发起采集",
        )
    return handle


__all__ = [
    "KIND_X", "KIND_XHS", "PLATFORM_BY_KIND", "DISPLAY_BY_KIND", "PROVIDER_KEY",
    "LAUNCH_EXEC", "LAUNCH_NODE_ENTRY",
    "FAILURE_DEPENDENCY", "FAILURE_CONNECTION", "FAILURE_AUTH", "FAILURE_CLASSES",
    "FileSystem", "ProviderHandle", "ProviderStatus", "ProviderResolver",
    "RESOLVER",
    "classify_failure", "default_version_probe", "freeze_into", "handle_for_run",
    "handle_from_snapshot", "install_hint", "is_real_executable", "normalize_kind",
    "require_frozen_handle", "resolve_opencli_launch", "resolve_provider",
    "resolve_twitter_launch",
]
