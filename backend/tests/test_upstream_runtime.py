"""upstream_runtime 运行实例自证的回归测试。

重点守住两条曾真实踩过的坑：
1. 解析不到端口时**不得**写 server.json（测试进程 import app 会覆盖真实例 → 误报 stale）。
2. code_mtime 必须是 **backend/ 全部模块的 max mtime**，而不是只看入口 app.py
   （只改子模块时旧口径会显示"代码未变"，导致"实例跑旧代码"被误判）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import upstream_runtime as ur  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def main() -> int:
    backend = ur.BACKEND_DIR

    # 1) code_mtime = 全部 backend/*.py 的最大值
    mt, mt_file = ur._code_mtime()
    check("code_mtime 可解析", mt is not None and mt_file is not None, f"mt={mt} file={mt_file}")
    mtimes = {}
    for p in backend.glob("*.py"):
        if p.name.startswith("_"):
            continue
        mtimes[p.name] = int(p.stat().st_mtime)
    check("code_mtime 等于全部模块 max", mt == max(mtimes.values()),
          f"got={mt} max={max(mtimes.values())}")
    check("code_mtime_file 指向最新模块", mt_file == max(mtimes, key=mtimes.get),
          f"got={mt_file} want={max(mtimes, key=mtimes.get)}")

    # 2) 排除项确实被排除（下划线前缀脚本、__pycache__ 不在 glob 结果里）
    check("不递归到 __pycache__", not any("__pycache__" in k for k in mtimes), str(list(mtimes)[:3]))
    check("排除下划线前缀脚本", not any(k.startswith("_") for k in mtimes))

    # 3) 只改子模块的场景：把某个子模块的 mtime 调到最新，code_mtime 必须跟着变
    victim = backend / "user_view.py"
    old = victim.stat().st_mtime
    bumped = max(mtimes.values()) + 3600  # 必须比现有最新模块还新，才能成为新的 max
    try:
        os.utime(victim, (bumped, bumped))
        mt2, mt_file2 = ur._code_mtime()
        check("只改子模块也能被感知", mt2 == int(victim.stat().st_mtime),
              f"mt2={mt2} victim={int(victim.stat().st_mtime)}")
        check("code_mtime_file 指向被改的子模块", mt_file2 == "user_view.py", str(mt_file2))
        check("旧口径（只看 app.py）确实会漏掉", int((backend / "app.py").stat().st_mtime) < mt2)
    finally:
        os.utime(victim, (old, old))
    mt3, _ = ur._code_mtime()
    check("恢复 mtime 后结果复原", mt3 == mt, f"{mt3} != {mt}")

    # 4) 无端口 → 不落盘（保护正在运行的真实例记录）
    before = ur.read_server_json()
    saved = os.environ.pop("FININTEL_PORT", None)
    argv = list(sys.argv)
    sys.argv = ["pytest"]
    try:
        payload = ur.write_server_json()
        check("无端口时 port=None", payload.get("port") is None, str(payload.get("port")))
        after = ur.read_server_json()
        check("无端口时不覆盖 server.json", after == before,
              f"before={before and before.get('pid')} after={after and after.get('pid')}")
    finally:
        sys.argv = argv
        if saved is not None:
            os.environ["FININTEL_PORT"] = saved

    # 5) 有端口 → 正常写入且字段齐全。
    #    用一个没人监听的端口，测完把原记录原样写回，避免污染正在运行实例的记录。
    import json

    os.environ["FININTEL_PORT"] = "18766"
    try:
        payload = ur.write_server_json()
        check("有端口时写入 port", payload.get("port") == 18766, str(payload.get("port")))
        disk = ur.read_server_json()
        check("落盘含 code_mtime_file", disk and "code_mtime_file" in disk, str(disk and list(disk)))
        check("落盘 pid 为本进程", disk and disk.get("pid") == os.getpid(),
              f"{disk and disk.get('pid')} != {os.getpid()}")
    finally:
        if saved is None:
            os.environ.pop("FININTEL_PORT", None)
        else:
            os.environ["FININTEL_PORT"] = saved
        try:
            if before is None:
                ur.SERVER_JSON.unlink()
            else:
                ur.SERVER_JSON.write_text(
                    json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    check("测后原实例记录已还原", ur.read_server_json() == before)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
