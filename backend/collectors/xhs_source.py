# -*- coding: utf-8 -*-
"""小红书采集源：OpenCLI 优先（任务书 §6）。

- 搜索笔记 → 读正文 → 读评论及回复，保存父子关系。
- xsec_token 等定位符来自当前搜索结果，不编造、不硬编码。
- 登录态只复用用户已授权的浏览器会话（OpenCLI Browser Bridge）。

契约加固（指令 §5-C 的 C1 / C2 / C3）
------------------------------------
**C1 正文与目标笔记的一致性**
OpenCLI 1.8.8 的 ``note`` 命令返回 ``[{field, value}, ...]``，字段只有
``title / author / content / likes / collects / comments / tags``，**没有 note_id**。
因此：

- 不再把详情的 ``source_id`` 强行覆盖成"上一次搜索的 ID"来假装一致；
- 目标 ID 只能从请求用的 locator（URL）本身解析出来；
- 只有当上游**额外**给出了身份证据（``id`` / ``note_id`` / ``pageUrl``）且与目标一致时，
  才置 ``source_url_verified=True``；
- 证据与目标不一致 → 抛 ``DETAIL_REDIRECTED``，绝不把别页正文绑定成本条目；
- 拿不到证据 → ``source_url_verified=False``（"未验证"，不是"已否定"）。

**C2 空列表与结构错误**
``_map_search`` / ``detail`` / ``comments`` 的每个解包分支都穷举了：缺少预期 key、
错误信封、错误类型都不会退化成 ``[]``。即使 ``returncode == 0`` 也要验证业务响应
（本机已复现：opencli 被错误解析时会把**命令清单 JSON** 打到 stdout）。
空列表必须区分三种语义：``empty_ok`` / ``empty_unverified`` / ``hidden_by_platform``，
被平台隐藏或限制**不能**当成"0 条评论"。

**C3 评论字段有限**
OpenCLI 1.8.8 的 ``comments`` 输出字段为
``rank / author / userId / profileUrl / text / likes / time / is_reply / reply_to / images``，
**没有可靠的 comment_id**，``reply_to`` 只是页面展示的回复对象名，**不是评论 ID**。
因此：

- 无平台 ID 的评论用稳定合成键，``identity_quality='synthetic'``；
- 合成键 seed **剔除易变字段**（点赞数），降低"编辑后产生新键"的概率；
- **同文碰撞风险**：同一作者在同一时间发两条完全相同的评论时，现有字段无法区分，
  合成键必然碰撞。我们不猜序号（那会破坏重复读取的幂等性），而是合并成一条并在
  ``warnings`` 里如实记录碰撞条数；
- **编辑后新键风险**：评论被作者编辑后正文变化 → 合成键变化 → 下次读取会生成新行。
  这是合成键的已知代价，不通过"模糊匹配作者+时间"去强行归并（那会把不同评论并成一条）；
- 无法确认直接父关系 → ``parent_missing=1``，``parent_source_id`` / ``root_source_id``
  保持 None，**不猜**"上一条非回复就是父节点"来构建漂亮的评论树；
- 原始相对时间（"3 天前"）必须落到 ``published_at_raw``，``published_at`` 置 None，
  ``published_at_quality='relative_unparsed'``，**绝不**把"3 天前"当 UTC 时间写入。
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse
import hashlib
import re

from collectors.contracts import (
    CapabilitySet, CollectorError, CommentBatch, CommentRecord, ContentRecord,
    SearchBatch,
    RESULT_SEMANTICS_EMPTY_OK, RESULT_SEMANTICS_EMPTY_UNVERIFIED,
    RESULT_SEMANTICS_HIDDEN, RESULT_SEMANTICS_OK,
    error_envelope_code, int_or_none, looks_like_manifest,
    map_upstream_error_code, normalize_published_at,
)
from collectors.runtime import XHS_READ_COMMANDS, parse_json_or_none, run_command

# 笔记 ID / 定位路径：与 OpenCLI clis/xiaohongshu/note-helpers.js 的 parseNoteId 同口径，
# 但对路径段本身放宽（离线 fixture 的 ID 不一定是全 hex）。
_NOTE_ID_RE = re.compile(
    r"/(?:explore|note|search_result|discovery/item)/([^/?#]+)", re.IGNORECASE)
_USER_NOTE_ID_RE = re.compile(r"/user/profile/[^/?#]+/([^/?#]+)", re.IGNORECASE)

# 结果列表所在的键（按优先级）。注意：裸列表也是合法形状（--format json 直接打印数组）。
_LIST_KEYS = ("data", "items", "results", "notes", "comments")

# 平台明示"隐藏/限制/需登录"的信号字段（出现在信封里时才可信）
_LOGIN_WALL_KEYS = ("loginWall", "login_wall", "requiresLogin", "requires_login")
_SECURITY_KEYS = ("securityBlock", "security_block", "SECURITY_BLOCK", "riskControl")
_HIDDEN_KEYS = ("hidden", "commentsDisabled", "comments_disabled",
                "commentsClosed", "restricted", "blocked")


def xhs_note_id(locator: str) -> Optional[str]:
    """从笔记 URL（或裸 ID）解析出笔记 ID；解析不到返回 None。

    与 OpenCLI ``parseNoteId`` 的区别：这里**不**把整个 URL 当成 ID 返回，
    因为那会把带 token 的完整 URL 变成 source_id。
    """
    text = (locator or "").strip()
    if not text:
        return None
    match = _NOTE_ID_RE.search(text) or _USER_NOTE_ID_RE.search(text)
    if match:
        return match.group(1)
    if "/" in text or "?" in text:
        return None
    return text or None


def _public_note_path(locator: str) -> str:
    """取 URL 的 path 用于合成键 seed（不含查询参数，避免 token 进 hash 输入）。"""
    try:
        return urlparse(locator).path or locator
    except ValueError:
        return locator


class XhsSource:
    """小红书采集源，封装 opencli 的只读调用。"""

    platform = "xiaohongshu"

    def __init__(self, exe: str):
        self.exe = exe

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            platform=self.platform,
            search=True,
            detail=True,
            comments=True,
            replies=True,          # --with-replies
            time_filter=False,
            sort=True,
            pagination=False,       # --limit 有上限，无 cursor
        )

    # ------------------------------------------------------------------
    # 公共：执行 + 业务响应校验（C2）
    # ------------------------------------------------------------------
    async def _call(self, subcommand: str, positional: tuple[str, ...],
                    options: dict[str, Any], timeout_seconds: int) -> Any:
        """执行一次只读命令，并把 stdout 解成业务 payload。

        **即使 returncode == 0 也要验证业务响应**（指令 C2）。
        """
        result = await run_command(
            self.exe, subcommand, positional=positional, options=options,
            allowed_commands=XHS_READ_COMMANDS, platform="xiaohongshu",
            timeout_seconds=timeout_seconds,
        )
        return self._decode(result, subcommand)

    def _decode(self, result: dict[str, Any], subcommand: str) -> Any:
        """把子进程结果解成业务 payload，穷举所有失败分支。"""
        if result["returncode"] != 0 or result["timed_out"]:
            raise self._error_from(result)
        payload = parse_json_or_none(result["stdout"])
        if payload is None:
            raise CollectorError(
                "CONTRACT_CHANGED",
                f"小红书 {subcommand} 未返回有效 JSON（returncode=0 也不代表业务成功）",
                evidence={"stdout_head": result["stdout"][:200]})
        # 本机已复现的真实失败：解析到的 opencli 未识别 argv，把命令清单打到 stdout
        if looks_like_manifest(payload):
            raise CollectorError(
                "DEPENDENCY_MISSING",
                "小红书 CLI 返回的是命令清单而不是业务结果（可执行程序未被正确解析）",
                operator_action="在设置页固定 opencli 的 node 入口与 main.js 后重试",
                evidence={"stdout_head": result["stdout"][:200]})
        upstream_code = error_envelope_code(payload)
        if upstream_code:
            code, message = map_upstream_error_code(upstream_code, "小红书")
            raise CollectorError(code, message, upstream_code=upstream_code,
                                 evidence={"payload_keys": sorted(payload)[:10]})
        return payload

    def _require_list(self, payload: Any, subcommand: str) -> tuple[list[Any], str]:
        """取出结果列表；任何"不是列表"的情况都报契约错，不退化成 []。"""
        if isinstance(payload, list):
            return payload, "bare_list"
        if isinstance(payload, dict):
            for key in _LIST_KEYS:
                if key not in payload:
                    continue
                value = payload[key]
                if value is None:
                    # 显式 null 是"上游没给"，不是"0 条"
                    raise CollectorError(
                        "CONTRACT_CHANGED",
                        f"小红书 {subcommand} 的 {key} 为 null，无法判定是否有内容",
                        evidence={"payload_keys": sorted(payload)[:10]})
                if not isinstance(value, list):
                    raise CollectorError(
                        "CONTRACT_CHANGED",
                        f"小红书 {subcommand} 的 {key} 不是列表",
                        evidence={"payload_type": type(value).__name__})
                return value, key
            raise CollectorError(
                "CONTRACT_CHANGED",
                f"小红书 {subcommand} 缺少结果列表",
                evidence={"payload_keys": sorted(payload)[:10]})
        raise CollectorError("CONTRACT_CHANGED",
                             f"小红书 {subcommand} 返回结构变化",
                             evidence={"payload_type": type(payload).__name__})

    def _hidden_signal(self, payload: Any) -> Optional[tuple[str, str]]:
        """识别平台明示的隐藏/限制信号；返回 (错误码, 说明)，无信号返回 None。

        被平台隐藏或限制**不能**当成"0 条评论"——这是 C2 的核心之一。
        """
        if not isinstance(payload, dict):
            return None
        for key in _LOGIN_WALL_KEYS:
            if payload.get(key):
                return ("AUTH_REQUIRED",
                        "小红书明示需要登录才能查看评论（不是 0 条评论）")
        for key in _SECURITY_KEYS:
            if payload.get(key):
                return ("VERIFICATION_REQUIRED",
                        "小红书触发安全限制，评论不可读（不是 0 条评论）")
        for key in _HIDDEN_KEYS:
            if payload.get(key):
                return ("VERIFICATION_REQUIRED",
                        f"小红书明示评论不可见/被限制（{key}），不得当作 0 条评论")
        return None

    def _empty_semantics(self, payload: Any) -> str:
        """判定空列表属于哪一种语义（C2）。

        只有当上游**明示**总数为 0 时才算 ``empty_ok``；否则一律
        ``empty_unverified``——因为"确实没有 / 被隐藏 / 没渲染出来"在响应里不可区分。
        """
        if isinstance(payload, dict):
            for key in ("total", "count", "total_count", "reported_total", "comment_count"):
                value = payload.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    return RESULT_SEMANTICS_EMPTY_OK if int(value) == 0 \
                        else RESULT_SEMANTICS_EMPTY_UNVERIFIED
                if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
                    return RESULT_SEMANTICS_EMPTY_OK if int(value.strip()) == 0 \
                        else RESULT_SEMANTICS_EMPTY_UNVERIFIED
        return RESULT_SEMANTICS_EMPTY_UNVERIFIED

    # ------------------------------------------------------------------
    async def search(self, query: str, *, limit: int = 5,
                     sort: str = "comprehensive", timeout_seconds: int = 90) -> SearchBatch:
        options: dict[str, Any] = {"--format": "json", "--limit": limit}
        if sort:
            options["--sort"] = sort
        payload = await self._call("search", (query,), options, timeout_seconds)
        records, semantics, warnings = self._map_search(payload)
        return SearchBatch(records=records, completeness="full",
                           warnings=warnings,
                           coverage={"retrieval_mode": "keyword_search",
                                     "result_semantics": semantics})

    def _map_search(self, payload: Any) -> tuple[list[ContentRecord], str, list[str]]:
        """映射搜索结果；返回 ``(records, result_semantics, warnings)``。

        真实形状（OpenCLI 1.8.8 ``clis/xiaohongshu/search.js`` 的 columns）：
        ``rank / title / author / likes / published_at / url``（外加 ``author_url``）。
        """
        warnings: list[str] = []
        hidden = self._hidden_signal(payload)
        if hidden:
            raise CollectorError(hidden[0], hidden[1])

        rows, _container = self._require_list(payload, "search")
        if not rows:
            semantics = self._empty_semantics(payload)
            if semantics == RESULT_SEMANTICS_EMPTY_UNVERIFIED:
                warnings.append(
                    "搜索返回 0 条，但无法区分「确实没有结果」与「被平台隐藏/限制或未渲染」")
            return ([], semantics, warnings)

        records: list[ContentRecord] = []
        for it in rows:
            if not isinstance(it, dict):
                warnings.append("搜索结果含非对象行，已跳过（不是空结果）")
                continue
            note_id = str(it.get("id") or it.get("note_id") or "").strip()
            url = str(it.get("url") or it.get("note_url") or "").strip()
            if url:
                parsed = urlparse(url)
                if parsed.scheme != "https" or parsed.hostname not in (
                        "www.xiaohongshu.com", "xiaohongshu.com"):
                    raise CollectorError("CONTRACT_CHANGED", "笔记来源链接域名不匹配",
                                         evidence={"url_host": parsed.hostname})
                note_id = note_id or (parsed.path.rstrip("/").split("/")[-1] if parsed.path else "")
            if not note_id and not url:
                warnings.append("搜索结果存在既无 ID 也无 URL 的行，已跳过")
                continue
            published_at, published_raw, published_quality = normalize_published_at(
                it.get("published_at") or it.get("time") or it.get("date"))
            likes = int_or_none(it.get("likes"))
            records.append(ContentRecord(
                platform=self.platform,
                source_id=note_id or url,
                canonical_url=url or None,
                title=str(it.get("title") or "") or None,
                evidence_text=str(it.get("desc") or it.get("title") or ""),
                language="zh",
                published_at=published_at,
                fetched_at="",
                detail_status="summary",   # 搜索结果是摘要，正文需 detail 再读
                author_key=str(it.get("author") or it.get("author_name") or "") or None,
                metrics={"likes": likes,
                         "likes_present": likes is not None},
                published_at_raw=published_raw,
                published_at_quality=published_quality,
                # 搜索列表里的 URL 来自搜索页本身，尚未证明能打开目标笔记
                source_url_verified=False,
            ))
        if not records:
            warnings.append("搜索返回了行但没有一行可映射成笔记，无法判定是否真的没有结果")
        return (records, RESULT_SEMANTICS_OK, warnings)

    # ------------------------------------------------------------------
    async def detail(self, note_url: str, *, expected_id: Optional[str] = None,
                     timeout_seconds: int = 90) -> ContentRecord:
        """读单篇笔记正文（需完整带 xsec_token 的 URL）。

        C1：目标 ID 只从 locator 解析；上游给出身份证据且与之一致才置
        ``source_url_verified=True``；不一致抛 ``DETAIL_REDIRECTED``。
        """
        if not note_url or not str(note_url).strip():
            raise CollectorError("LOCATOR_MISSING",
                                 "缺少笔记定位 URL，无法读取正文",
                                 operator_action="从搜索结果重新定位该笔记后重试")
        target_id = expected_id or xhs_note_id(note_url)
        payload = await self._call("note", (note_url,), {"--format": "json"}, timeout_seconds)
        data = self._note_object(payload)

        # --- C1 一致性校验：绝不把别页正文绑定成目标笔记 ---
        returned_id = str(data.get("id") or data.get("note_id") or data.get("noteId") or "").strip()
        page_url = str(data.get("pageUrl") or data.get("page_url") or
                       data.get("url") or data.get("note_url") or "").strip()
        verified = False
        if target_id:
            if returned_id:
                if returned_id != target_id:
                    raise CollectorError(
                        "DETAIL_REDIRECTED",
                        "小红书返回的笔记 ID 与请求目标不一致，已丢弃该正文（未绑定到目标笔记）",
                        evidence={"expected_id": target_id, "returned_id": returned_id})
                verified = True
            elif page_url:
                page_id = xhs_note_id(page_url)
                if page_id:
                    if page_id != target_id:
                        raise CollectorError(
                            "DETAIL_REDIRECTED",
                            "小红书正文落地页与请求目标不一致，已丢弃该正文",
                            evidence={"expected_id": target_id, "page_id": page_id})
                    verified = True
        # 拿不到身份证据 → 保留"未验证"，不强行覆盖、也不伪造

        content = str(data.get("content") or data.get("desc") or data.get("text") or "")
        title = str(data.get("title") or "") or None
        truncated = bool(data.get("truncated")) or content.rstrip().endswith(("…", "..."))
        if content and truncated:
            detail_status = "partial"
        else:
            detail_status = "complete" if content else "missing"

        published_at, published_raw, published_quality = normalize_published_at(
            data.get("time") or data.get("published_at") or data.get("date"))
        likes = int_or_none(data.get("likes"))
        collects = int_or_none(data.get("collects"))
        comment_count = int_or_none(data.get("comments"))
        return ContentRecord(
            platform=self.platform,
            # 未验证时也不允许返回别的目标：source_id 只能是请求目标（或空）
            source_id=target_id or returned_id or note_url,
            canonical_url=note_url,
            title=title,
            evidence_text=content,
            language="zh",
            published_at=published_at,
            fetched_at="",
            detail_status=detail_status,
            author_key=str(data.get("author") or data.get("author_name") or "") or None,
            metrics={"likes": likes, "collects": collects, "comments": comment_count,
                     "likes_present": likes is not None,
                     "collects_present": collects is not None,
                     "comments_present": comment_count is not None,
                     "truncated": truncated},
            published_at_raw=published_raw,
            published_at_quality=published_quality,
            source_url_verified=verified,
        )

    def _note_object(self, payload: Any) -> dict[str, Any]:
        """把 note 响应解成对象。

        真实形状是 ``[{field, value}, ...]``（columns 为 field/value）；
        兼容 ``{data: [...]}`` 与直接对象。空数组/非对象一律报契约错。
        """
        data = payload
        if isinstance(payload, dict):
            inner = payload.get("data")
            data = inner if inner is not None else payload
        if isinstance(data, list):
            if not data:
                raise CollectorError(
                    "CONTRACT_CHANGED",
                    "小红书 note 返回空列表，无法确认正文（不是「正文为空」）")
            if all(isinstance(r, dict) and "field" in r and "value" in r for r in data):
                data = {str(r.get("field")): r.get("value") for r in data}
            elif len(data) == 1 and isinstance(data[0], dict):
                data = data[0]
            else:
                raise CollectorError(
                    "CONTRACT_CHANGED",
                    "小红书 note 返回结构变化（既不是 field/value 数组也不是单对象）",
                    evidence={"row_count": len(data)})
        if not isinstance(data, dict):
            raise CollectorError("CONTRACT_CHANGED", "小红书 note 返回结构变化",
                                 evidence={"payload_type": type(data).__name__})
        return data

    # ------------------------------------------------------------------
    async def comments(self, note_url: str, *, limit: int = 20,
                       with_replies: bool = True, timeout_seconds: int = 120) -> CommentBatch:
        """读笔记评论（含楼中楼）。

        C3：无平台 ID → 合成键（seed 剔除点赞数）；无法确认父关系 → parent_missing；
        原始相对时间 → published_at_raw，published_at 置 None。
        """
        if not note_url or not str(note_url).strip():
            raise CollectorError("LOCATOR_MISSING",
                                 "缺少笔记定位 URL，无法读取评论",
                                 operator_action="从搜索结果重新定位该笔记后重试")
        options: dict[str, Any] = {"--format": "json", "--limit": limit}
        if with_replies:
            options["--with-replies"] = True
        payload = await self._call("comments", (note_url,), options, timeout_seconds)
        return self._map_comments(payload, note_url, limit=limit)

    def _map_comments(self, payload: Any, note_url: str, *, limit: int) -> CommentBatch:
        """映射评论结果（含空列表的三态语义判定）。"""
        hidden = self._hidden_signal(payload)
        if hidden:
            raise CollectorError(hidden[0], hidden[1],
                                 evidence={"result_semantics": RESULT_SEMANTICS_HIDDEN})

        rows, _container = self._require_list(payload, "comments")
        if not rows:
            semantics = self._empty_semantics(payload)
            warnings: list[str] = []
            if semantics == RESULT_SEMANTICS_EMPTY_UNVERIFIED:
                warnings.append(
                    "评论返回 0 条，但无法区分「确实没有评论」与「被平台隐藏/限制或未渲染」")
            return CommentBatch(comments=[], reported_total=0, root_count=0, reply_count=0,
                                stop_reason=("empty_confirmed" if semantics == RESULT_SEMANTICS_EMPTY_OK
                                             else "empty_unverified"),
                                warnings=warnings, result_semantics=semantics,
                                limit_semantics="root_comments")

        note_path = _public_note_path(note_url)
        comments: list[CommentRecord] = []
        seen: set[str] = set()
        collisions = 0
        skipped = 0
        root_count = 0
        reply_count = 0
        # --limit 在小红书是"根评论上限"，回复不计入该上限（OpenCLI comments.js 已证实）
        roots_seen = 0
        stop_reason = "no_more_rendered"

        for c in rows:
            if not isinstance(c, dict):
                skipped += 1
                continue
            text = str(c.get("text") or c.get("content") or "").strip()
            if not text:
                skipped += 1
                continue
            is_reply = bool(c.get("is_reply"))
            if not is_reply:
                roots_seen += 1
                if roots_seen > limit:
                    stop_reason = "root_limit_reached"
                    break
                root_count += 1
            else:
                reply_count += 1

            cid = str(c.get("id") or c.get("comment_id") or "").strip()
            synthetic = not cid
            if synthetic:
                # seed 只含稳定字段：笔记路径 + 作者标识 + 正文 + 原始时间。
                # **不含点赞数**（易变，会导致"编辑/点赞后产生新键"）。
                seed = "|".join([note_path,
                                 str(c.get("userId") or c.get("author") or ""),
                                 text,
                                 str(c.get("time") or "")])
                cid = "synthetic_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
            if cid in seen:
                # 同文碰撞：现有字段无法区分两条完全相同的评论。
                # 不猜序号（会破坏重复读取的幂等性），合并成一条并如实记录。
                collisions += 1
                continue
            seen.add(cid)

            # OpenCLI 的 reply_to 只是页面展示的回复对象名，不是评论 ID → 不当父 ID。
            parent_id = c.get("parent_id") or c.get("parentId") or None
            root_id = c.get("root_id") or c.get("rootId") or None
            has_parent_hint = is_reply or bool(str(c.get("reply_to") or "").strip())
            published_at, published_raw, published_quality = normalize_published_at(
                c.get("time") or c.get("published_at") or c.get("date"))

            comments.append(CommentRecord(
                source_comment_id=cid,
                parent_source_id=str(parent_id) if parent_id else None,
                # 不猜 root：只有平台明确给了才记录
                root_source_id=str(root_id) if root_id else None,
                author_key=str(c.get("author") or c.get("userId") or "") or None,
                evidence_text=text,
                published_at=published_at,
                fetched_at="",
                content_status="complete",
                like_count=int_or_none(c.get("likes")),
                parent_missing=bool(has_parent_hint and not parent_id),
                identity_quality="synthetic" if synthetic else "platform",
                published_at_raw=published_raw,
                published_at_quality=published_quality,
            ))

        warnings = []
        if collisions:
            warnings.append(
                f"有 {collisions} 条评论因作者/正文/时间完全相同而无法区分，已合并为同一合成键"
                "（不猜序号，以保证重复读取幂等）")
        if skipped:
            warnings.append(f"有 {skipped} 条评论行结构异常或正文为空，已跳过")
        if stop_reason == "no_more_rendered":
            warnings.append("评论列表是否已取尽无法确认（上游无游标、无总数保证）")
        return CommentBatch(comments=comments, has_more=None,
                            reported_total=len(comments),
                            root_count=root_count, reply_count=reply_count,
                            sort=None, stop_reason=stop_reason,
                            warnings=warnings,
                            result_semantics=RESULT_SEMANTICS_OK,
                            limit_semantics="root_comments")

    # ------------------------------------------------------------------
    def _error_from(self, result: dict[str, Any]) -> CollectorError:
        combined = (result.get("stdout", "") + "\n" + result.get("stderr", ""))
        if result.get("timed_out"):
            return CollectorError("TIMEOUT", "小红书采集超时", retryable=True,
                                  operator_action="稍后重试")
        if "Browser Bridge extension not connected" in combined or \
                "BROWSER_CONNECT" in combined:
            return CollectorError("CONNECTION_UNAVAILABLE",
                                  "小红书需要 OpenCLI 浏览器会话，但扩展未连接",
                                  operator_action="在 Chrome 启用 OpenCLI 扩展并登录小红书后重试")
        if any(x in combined for x in ("SECURITY_BLOCK", "安全限制", "访问链接异常",
                                       "ACCOUNT_RISK", "ENVIRONMENT_RISK")):
            return CollectorError("VERIFICATION_REQUIRED", "小红书要求平台验证，采集已暂停")
        if "AUTH_REQUIRED" in combined or "requires login" in combined or \
                "AuthRequiredError" in combined:
            return CollectorError("AUTH_REQUIRED", "小红书需要登录，请在已授权的浏览器中完成登录")
        if "429" in combined or "RATE_LIMITED" in combined:
            return CollectorError("RATE_LIMITED", "小红书限制请求，采集已暂停")
        if "ambiguous_option" in combined:
            return CollectorError("CONTRACT_CHANGED", "小红书页面结构变化，搜索筛选面板不匹配",
                                  operator_action="稍后重试或改用其他关键词")
        if result.get("returncode") == -1:
            return CollectorError("DEPENDENCY_MISSING", "opencli 不可用",
                                  operator_action="检查 opencli 安装")
        return CollectorError("CONNECTION_UNAVAILABLE", "小红书采集失败，请查看连接诊断",
                              retryable=True, operator_action="检查后重试")


__all__ = ["XhsSource", "xhs_note_id"]
