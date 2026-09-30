# -*- coding: utf-8 -*-
"""社交采集适配器契约测试（指令 §5-C 的 C1 / C2 / C3 / C4；增量设计 T06 / T07）。

**本文件全部是离线测试**：
- 子进程 = ``social_testkit.fake_opencli`` 打桩的 ``asyncio.create_subprocess_exec``，
  **不启动任何真实进程、不访问任何平台**；
- 输入 = ``backend/fixtures/social/*.json`` 的**脱敏形状样本**（结构与本机真实 CLI 一致，
  内容全部编造），文件头均有 ``__OFFLINE_FIXTURE__: true``；
- 数据库：本文件不写任何库（适配器层不落库）。

离线通过 ≠ 平台可用。这里证明的是"适配器对每一种响应形状的行为正确"，
不是"小红书 / X 当前能采到数据"。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(BACKEND_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import social_testkit as kit  # noqa: E402
from collectors.contracts import CollectorError  # noqa: E402
from collectors.x_source import XSource  # noqa: E402
from collectors.xhs_source import XhsSource, xhs_note_id  # noqa: E402

FIXTURE_DIR = BACKEND_DIR / "fixtures" / "social"
FIXTURE_FILES = ("xhs_search.json", "xhs_note.json", "xhs_comments.json",
                 "x_search.json", "x_tweet.json")

# 哨兵假值：测试里出现的任何"凭据样式"字符串都必须是这种明显假值。
SENTINEL_AUTH_TOKEN = "SENTINEL_SECRET_AUTH_TOKEN"
SENTINEL_CT0 = "SENTINEL_SECRET_CT0"


def load_fixture(name: str) -> dict:
    """读取脱敏形状样本；文件头必须是 ``__OFFLINE_FIXTURE__: true``。"""
    path = FIXTURE_DIR / name
    assert path.is_file(), f"缺少 fixture：{path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("__OFFLINE_FIXTURE__") is True, f"{name} 未标记为离线样本"
    assert data.get("__note__"), f"{name} 缺少说明注释"
    return data


def _xhs_exe(tmp_path: Path) -> str:
    return kit.fake_cli_path(tmp_path)


def _x_exe(tmp_path: Path) -> str:
    exe = tmp_path / "twitter.exe"
    exe.write_text("", encoding="utf-8")
    return str(exe)


@pytest.fixture
def x_auth(monkeypatch):
    """注入 X 凭据哨兵值（不是真实 token，只用于让适配器通过配置检查）。"""
    monkeypatch.setenv("TWITTER_AUTH_TOKEN", SENTINEL_AUTH_TOKEN)
    monkeypatch.setenv("TWITTER_CT0", SENTINEL_CT0)
    return True


# ---------------------------------------------------------------------------
# 0. fixture 自身的可信度（离线样本必须可被一眼识别）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_files_are_marked_offline_samples(name: str) -> None:
    """离线：每个 fixture 都必须显式声明"脱敏形状样本，非真实数据"。"""
    data = load_fixture(name)
    note = str(data["__note__"])
    assert "脱敏" in note and "非真实数据" in note, note
    assert "不得当作平台可用性证据" in note, note
    assert data.get("shape_source"), f"{name} 必须写明形状来源（真实文件路径）"
    assert data.get("cli_version"), f"{name} 必须写明 CLI 版本"


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixtures_contain_only_sentinel_secrets(name: str) -> None:
    """离线：fixture 里出现的凭据样式值只能是哨兵假值，不得夹带真实 token。"""
    raw = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    for match in re.finditer(r"(?i)(xsec_token|auth_token|ct0|token|secret)\s*[:=]\s*([^\s,;&\"']+)",
                             raw):
        value = match.group(2)
        assert value.startswith("SENTINEL_SECRET_"), \
            f"{name} 出现非哨兵的凭据样式值：{value}"


def test_xhs_note_id_parser_matches_opencli_shape() -> None:
    """离线：note ID 解析与 OpenCLI 的 parseNoteId 同口径（且不把整条 URL 当 ID）。"""
    assert xhs_note_id("https://www.xiaohongshu.com/search_result/657a0f11sample0001"
                       "?xsec_token=SENTINEL_SECRET_XSEC_TOKEN_A") == "657a0f11sample0001"
    assert xhs_note_id("https://www.xiaohongshu.com/explore/abcdef012345") == "abcdef012345"
    assert xhs_note_id("https://www.xiaohongshu.com/user/profile/u1/abcdef012345") == "abcdef012345"
    assert xhs_note_id("657a0f11sample0001") == "657a0f11sample0001"
    assert xhs_note_id("") is None


# ---------------------------------------------------------------------------
# C1：小红书正文的 ID 不能靠强制覆盖（离线）
# ---------------------------------------------------------------------------

def test_xhs_note_without_identity_stays_unverified(tmp_path, monkeypatch) -> None:
    """离线：真实 note 输出没有 note_id → 保留"未验证"，不得强行覆盖成别的目标。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    rec = asyncio.run(XhsSource(exe).detail(fixture["locator"]))

    assert rec.source_url_verified is False, "没有身份证据时不得声称已验证"
    assert rec.source_id == fixture["expected_id"], \
        f"未验证也不允许绑定别的目标：{rec.source_id}"
    assert rec.evidence_text, "正文仍应被读出"
    assert rec.detail_status == "complete"


def test_xhs_note_matching_id_marks_url_verified(tmp_path, monkeypatch) -> None:
    """离线：上游给出 id 且与 locator 一致 → source_url_verified=True。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["matching_id"]["payload"])])

    rec = asyncio.run(XhsSource(exe).detail(fixture["locator"]))

    assert rec.source_url_verified is True
    assert rec.source_id == fixture["expected_id"]


def test_xhs_note_mismatched_id_raises_detail_redirected(tmp_path, monkeypatch) -> None:
    """离线：note 返回 id 与 locator 不一致 → DETAIL_REDIRECTED，且不产出正文绑定。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["redirected_id"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).detail(fixture["locator"]))

    assert exc.value.code == "DETAIL_REDIRECTED", exc.value.code
    assert exc.value.evidence.get("returned_id") == "657a0f11sample9999"


def test_xhs_note_mismatched_page_url_raises_detail_redirected(tmp_path, monkeypatch) -> None:
    """离线：正文落地页指向别篇 → DETAIL_REDIRECTED（不把别页正文绑成本条目）。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["redirected_page_url"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).detail(fixture["locator"]))

    assert exc.value.code == "DETAIL_REDIRECTED", exc.value.code


def test_xhs_detail_accepts_explicit_expected_id(tmp_path, monkeypatch) -> None:
    """离线：调用方显式给出目标 ID 时也走同一条一致性校验。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["matching_id"]["payload"])])

    rec = asyncio.run(XhsSource(exe).detail(
        fixture["locator"], expected_id=fixture["expected_id"]))

    assert rec.source_url_verified is True
    assert rec.source_id == fixture["expected_id"]


def test_xhs_detail_requires_locator(tmp_path) -> None:
    """离线：没有 locator 时明确报 LOCATOR_MISSING，不静默返回空正文。"""
    exe = _xhs_exe(tmp_path)
    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).detail(""))
    assert exc.value.code == "LOCATOR_MISSING", exc.value.code


def test_xhs_note_empty_list_is_contract_error(tmp_path, monkeypatch) -> None:
    """离线：note 返回空数组不是"正文为空"，而是拿不到业务响应。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["empty_list"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).detail(fixture["locator"]))
    assert exc.value.code == "CONTRACT_CHANGED", exc.value.code


def test_xhs_manifest_payload_is_dependency_missing_even_when_rc0(tmp_path, monkeypatch) -> None:
    """离线：returncode=0 但 stdout 是命令清单 → 必须报错（本机已复现的真实失败）。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["manifest_shape"]["payload"],
                                  returncode=0)])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).detail(fixture["locator"]))

    assert exc.value.code == "DEPENDENCY_MISSING", exc.value.code


def test_xhs_note_truncated_marks_partial(tmp_path, monkeypatch) -> None:
    """离线：正文被截断 → detail_status=partial，不当完整正文。"""
    fixture = load_fixture("xhs_note.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["truncated"]["payload"])])

    rec = asyncio.run(XhsSource(exe).detail(fixture["locator"]))

    assert rec.detail_status == "partial", rec.detail_status


# ---------------------------------------------------------------------------
# C2：空列表与结构错误（离线）
# ---------------------------------------------------------------------------

def test_xhs_search_maps_fixture_rows(tmp_path, monkeypatch) -> None:
    """离线：真实 search 形状（rank/title/author/likes/url/published_at）能正确映射。"""
    fixture = load_fixture("xhs_search.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XhsSource(exe).search("风控岗位", limit=5))

    assert len(batch.records) == 3, [r.source_id for r in batch.records]
    first = batch.records[0]
    assert first.source_id == "657a0f11sample0001"
    assert first.title == "风控岗转行三个月，说点真实的体感"
    assert first.detail_status == "summary", "搜索结果是摘要，不是正文"
    assert first.metrics["likes"] == 12000, first.metrics   # "1.2万"
    assert first.canonical_url.startswith("https://www.xiaohongshu.com/search_result/")
    # 搜索列表 URL 来自搜索页，尚未证明能打开目标笔记
    assert first.source_url_verified is False


def test_xhs_search_missing_list_key_raises_contract_changed(tmp_path, monkeypatch) -> None:
    """离线：缺少结果列表 → CONTRACT_CHANGED，不得退化成 SearchBatch(records=[])。"""
    fixture = load_fixture("xhs_search.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["missing_list_key"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).search("关键词"))
    assert exc.value.code == "CONTRACT_CHANGED", exc.value.code


def test_xhs_search_null_data_is_contract_error(tmp_path, monkeypatch) -> None:
    """离线：data 为 null 是"上游没给"，不是"0 条"。"""
    fixture = load_fixture("xhs_search.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["null_data"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).search("关键词"))
    assert exc.value.code == "CONTRACT_CHANGED", exc.value.code


def test_xhs_search_error_envelope_is_auth_required(tmp_path, monkeypatch) -> None:
    """离线：错误信封必须按错误码处理，不能当空结果。"""
    fixture = load_fixture("xhs_search.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["error_envelope"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).search("关键词"))
    assert exc.value.code == "AUTH_REQUIRED", exc.value.code


def test_xhs_search_empty_semantics_distinguished(tmp_path, monkeypatch) -> None:
    """离线：空列表必须区分「确实 0 条」与「无法判定」。"""
    fixture = load_fixture("xhs_search.json")
    exe = _xhs_exe(tmp_path)

    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["empty_ok"]["payload"])])
    confirmed = asyncio.run(XhsSource(exe).search("关键词"))
    assert confirmed.records == []
    assert confirmed.coverage["result_semantics"] == "empty_ok"
    assert confirmed.warnings == []

    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["empty_unverified"]["payload"])])
    unknown = asyncio.run(XhsSource(exe).search("关键词"))
    assert unknown.records == []
    assert unknown.coverage["result_semantics"] == "empty_unverified"
    assert any("无法区分" in w for w in unknown.warnings), unknown.warnings


def test_xhs_hidden_signal_is_not_empty_result(tmp_path, monkeypatch) -> None:
    """离线：平台明示隐藏/限制 → 报错，绝不当"0 条"。"""
    fixture = load_fixture("xhs_search.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["hidden_by_platform"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).search("关键词"))
    assert exc.value.code == "AUTH_REQUIRED", exc.value.code
    assert "不是 0 条" in exc.value.message


def test_xhs_invalid_json_is_contract_changed(tmp_path, monkeypatch) -> None:
    """离线：stdout 不是 JSON（即使 rc=0）→ CONTRACT_CHANGED。"""
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json("这不是 JSON", returncode=0)])
    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).search("关键词"))
    assert exc.value.code == "CONTRACT_CHANGED", exc.value.code


def test_xhs_comments_maps_fixture_rows(tmp_path, monkeypatch) -> None:
    """离线：真实 comments 形状（无 comment_id、含 is_reply/reply_to）能正确映射。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))

    assert len(batch.comments) == 4, [c.source_comment_id for c in batch.comments]
    assert batch.root_count == fixture["expected"]["root_count"]
    assert batch.reply_count == fixture["expected"]["reply_count"]
    assert all(c.identity_quality == "synthetic" for c in batch.comments)
    assert len({c.source_comment_id for c in batch.comments}) == 4


def test_xhs_comments_missing_metrics_are_none(tmp_path, monkeypatch) -> None:
    """离线：缺 like 数为 None（未知），不得填 0。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["missing_metrics"]["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))

    assert batch.comments[0].like_count is None, batch.comments[0].like_count


def test_xhs_comments_dict_not_list_is_contract_error(tmp_path, monkeypatch) -> None:
    """离线：data 不是列表 → 契约错误。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["dict_not_list"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    assert exc.value.code == "CONTRACT_CHANGED", exc.value.code


def test_xhs_comments_non_dict_row_is_warned_not_empty(tmp_path, monkeypatch) -> None:
    """离线：坏行被跳过但必须留下 warning，不能静默变成"0 条评论"。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["non_dict_row"]["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))

    assert len(batch.comments) == 1
    assert any("跳过" in w for w in batch.warnings), batch.warnings


def test_xhs_comments_empty_semantics_distinguished(tmp_path, monkeypatch) -> None:
    """离线：评论空列表的两种语义必须可区分。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)

    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["empty_ok"]["payload"])])
    confirmed = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    assert confirmed.result_semantics == "empty_ok"
    assert confirmed.warnings == []

    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["empty_unverified"]["payload"])])
    unknown = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    assert unknown.result_semantics == "empty_unverified"
    assert unknown.reported_total == 0
    assert any("无法区分" in w for w in unknown.warnings), unknown.warnings


@pytest.mark.parametrize("variant,expected_code", [
    ("hidden_by_platform", "VERIFICATION_REQUIRED"),
    ("security_block", "VERIFICATION_REQUIRED"),
])
def test_xhs_comments_hidden_is_not_zero_comments(tmp_path, monkeypatch,
                                                  variant: str, expected_code: str) -> None:
    """离线：被平台隐藏/限制不能当"0 条评论"。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"][variant]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    assert exc.value.code == expected_code, exc.value.code
    assert "0 条评论" in exc.value.message


# ---------------------------------------------------------------------------
# C3：小红书评论字段有限（离线）
# ---------------------------------------------------------------------------

def test_xhs_relative_time_kept_raw_not_written_as_utc(tmp_path, monkeypatch) -> None:
    """离线：相对时间必须保留 raw，published_at 置 None（不得当 UTC 时间写入）。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    by_raw = {c.published_at_raw: c for c in batch.comments}

    assert "3 天前" in by_raw, [c.published_at_raw for c in batch.comments]
    assert by_raw["3 天前"].published_at is None, "相对时间不得写进 published_at"
    assert by_raw["3 天前"].published_at_quality == "relative_unparsed"
    assert by_raw["昨天"].published_at is None
    assert by_raw["昨天"].published_at_quality == "relative_unparsed"


def test_xhs_absolute_time_is_parsed(tmp_path, monkeypatch) -> None:
    """离线：可解析的绝对时间正常进 published_at，raw 同时保留。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    abs_row = next(c for c in batch.comments if c.published_at_raw == "2026-09-21T09:30:00Z")

    assert abs_row.published_at == "2026-09-21T09:30:00Z"
    assert abs_row.published_at_quality == "absolute"


def test_xhs_date_only_time_is_not_fabricated(tmp_path, monkeypatch) -> None:
    """离线：只有日期（"09-22"）不得被当成时刻，published_at 置 None 并保留 raw。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    row = next(c for c in batch.comments if c.published_at_raw == "09-22")

    assert row.published_at is None, "不得把无年份的日期串当时刻写入"
    assert row.published_at_quality in ("relative_unparsed", "unparsed"), row


def test_xhs_comment_parent_and_root_unknown(tmp_path, monkeypatch) -> None:
    """离线：reply_to 是展示名，不能当父 ID；无法确认关系就标记未知。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))
    reply = next(c for c in batch.comments if c.published_at_raw == "昨天")

    assert reply.parent_source_id is None, "reply_to 不是评论 ID"
    assert reply.parent_missing is True, "无法确认父关系必须标记"
    assert reply.root_source_id is None, "不得猜 root 来构建评论树"
    assert all(c.root_source_id is None for c in batch.comments), "不得猜父子关系"


def test_xhs_synthetic_key_stable_when_likes_change(tmp_path, monkeypatch) -> None:
    """离线：点赞数变化不改变合成键（seed 已剔除易变字段）。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    base = fixture["payload"]
    edited = fixture["variants"]["likes_changed"]["payload"]
    kit.fake_opencli(monkeypatch, [kit.ok_json(base), kit.ok_json(edited)])
    src = XhsSource(exe)

    first = asyncio.run(src.comments(fixture["locator"], limit=20))
    second = asyncio.run(src.comments(fixture["locator"], limit=20))

    base_row = next(c for c in first.comments if "第一条样本评论" in c.evidence_text)
    edited_row = second.comments[0]
    assert edited_row.source_comment_id == base_row.source_comment_id, \
        "点赞数变化不应产生新合成键"
    assert base_row.identity_quality == "synthetic"


def test_xhs_same_text_collision_is_reported(tmp_path, monkeypatch) -> None:
    """离线：同作者同正文同时间的两条评论无法区分 → 如实记录碰撞，不假装采到两条。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["same_text_collision"]["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=20))

    assert len(batch.comments) == 1, "碰撞后只保留一条（现有字段无法区分）"
    assert any("碰撞" in w or "无法区分" in w for w in batch.warnings), batch.warnings


def test_xhs_limit_is_root_comment_cap(tmp_path, monkeypatch) -> None:
    """离线：--limit 是根评论上限；已取根评论的回复不得被 Python 侧切片丢掉。"""
    fixture = load_fixture("xhs_comments.json")
    exe = _xhs_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XhsSource(exe).comments(fixture["locator"], limit=2))

    assert batch.root_count == 2, batch.root_count
    assert batch.reply_count == 1, "根评论的回复应随该根一起保留"
    assert batch.limit_semantics == "root_comments"
    assert batch.stop_reason == "root_limit_reached"


# ---------------------------------------------------------------------------
# C4：X 契约与脱敏 fixture（离线）
# ---------------------------------------------------------------------------

def test_x_search_maps_real_envelope(tmp_path, monkeypatch, x_auth) -> None:
    """离线：twitter-cli 真实信封 {ok, schema_version, data:[...]} + camelCase 字段。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XSource(exe).search("consumer credit", max_count=10,
                                            since="2026-09-21"))

    assert len(batch.records) == fixture["expected"]["record_count"]
    first = batch.records[0]
    assert first.source_id == "1900000000000000001"
    assert first.canonical_url == "https://x.com/i/status/1900000000000000001"
    assert first.published_at == "2026-09-21T08:15:00Z", first.published_at
    assert first.published_at_quality == "absolute"
    assert first.metrics["likes"] == 120
    assert first.metrics["views"] == 15000
    assert first.language == "en"


def test_x_search_bare_list_supported(tmp_path, monkeypatch, x_auth) -> None:
    """离线：裸数组（tweets_to_json 的输出）也要支持。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["bare_list"]["payload"])])

    batch = asyncio.run(XSource(exe).search("q"))

    assert len(batch.records) == 1
    assert batch.records[0].source_id == "1900000000000000010"


def test_x_search_nested_tweets_supported(tmp_path, monkeypatch, x_auth) -> None:
    """离线：data 里再嵌 tweets 数组也能取出。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["nested_tweets"]["payload"])])

    batch = asyncio.run(XSource(exe).search("q"))

    assert len(batch.records) == 1
    assert batch.records[0].source_id == "1900000000000000011"


def test_x_missing_metric_is_none_not_zero(tmp_path, monkeypatch, x_auth) -> None:
    """离线：缺 views/bookmarks → None，绝不填 0。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XSource(exe).search("q"))
    row = next(r for r in batch.records if r.source_id == "1900000000000000004")

    assert row.metrics["views"] is None, row.metrics
    assert row.metrics["bookmarks"] is None, row.metrics
    assert row.metrics["likes"] == 5, "显式给出的 0/数值不得被改成 None"


def test_x_missing_metrics_block_is_all_none(tmp_path, monkeypatch, x_auth) -> None:
    """离线：整条 metrics 缺失 → 所有互动数为 None。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["no_metrics_at_all"]["payload"])])

    batch = asyncio.run(XSource(exe).search("q"))
    metrics = batch.records[0].metrics

    assert metrics["metrics_present"] is False
    assert all(metrics[k] is None for k in
               ("likes", "retweets", "replies", "quotes", "views", "bookmarks")), metrics


def test_x_author_key_is_stable_anon_not_screen_name(tmp_path, monkeypatch, x_auth) -> None:
    """离线：author_key 必须是稳定匿名键，不得直存明文 screen_name。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    batch = asyncio.run(XSource(exe).search("q"))

    for rec in batch.records:
        assert rec.author_key is None or rec.author_key.startswith("anon_"), rec.author_key
        for name in fixture["expected"]["author_key_never_contains"]:
            assert name not in (rec.author_key or ""), rec.author_key
    # 哨兵断言：正文里也不应出现我们写入的明文 screen_name
    first = batch.records[0]
    assert "sample_author_one" not in first.evidence_text


def test_x_author_key_is_stable_across_reads(tmp_path, monkeypatch, x_auth) -> None:
    """离线：同一作者两次读取得到同一匿名键（可用作"同一作者"口径）。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"]),
                                   kit.ok_json(fixture["payload"])])
    src = XSource(exe)

    a = asyncio.run(src.search("q"))
    b = asyncio.run(src.search("q"))

    assert [r.author_key for r in a.records] == [r.author_key for r in b.records]


def test_x_search_error_envelope_is_auth_required(tmp_path, monkeypatch, x_auth) -> None:
    """离线：rc=0 但 ok=false 的错误信封必须按错误码处理，不能当空结果。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["error_envelope"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).search("q"))

    assert exc.value.code == "AUTH_REQUIRED", exc.value.code
    assert exc.value.upstream_code == "not_authenticated"


def test_x_search_missing_list_key_is_contract_changed(tmp_path, monkeypatch, x_auth) -> None:
    """离线：缺少结果列表 → CONTRACT_CHANGED，不得退化成空数组。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["missing_list_key"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).search("q"))
    assert exc.value.code == "CONTRACT_CHANGED", exc.value.code


def test_x_search_empty_semantics_distinguished(tmp_path, monkeypatch, x_auth) -> None:
    """离线：X 的空列表同样要区分「确实 0 条」与「无法判定」。"""
    fixture = load_fixture("x_search.json")
    exe = _x_exe(tmp_path)

    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["empty_ok"]["payload"])])
    ok = asyncio.run(XSource(exe).search("q"))
    assert ok.records == []
    assert ok.coverage["result_semantics"] == "empty_ok"

    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["empty_unverified"]["payload"])])
    unknown = asyncio.run(XSource(exe).search("q"))
    assert unknown.coverage["result_semantics"] == "empty_unverified"
    assert any("无法区分" in w for w in unknown.warnings), unknown.warnings


def test_x_tweet_locate_by_id_not_first_element(tmp_path, monkeypatch, x_auth) -> None:
    """离线：tweet 命令返回「目标 + 回复」混合列表 → 必须按 id 定位，不能取第一条。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["payload"])])

    rec = asyncio.run(XSource(exe).detail(fixture["target_id"]))

    assert rec.source_id == fixture["expected"]["resolved_id"], rec.source_id
    assert rec.source_id != fixture["expected"]["must_not_be_first_element_id"]
    assert rec.evidence_text == fixture["expected"]["evidence_text"]
    assert rec.source_url_verified is True, "按 ID 命中目标 → 可标记已验证"
    assert rec.metrics["likes"] == 300


def test_x_tweet_target_absent_raises_detail_redirected(tmp_path, monkeypatch, x_auth) -> None:
    """离线：返回列表里没有目标 id → DETAIL_REDIRECTED，绝不返回第一条。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["target_absent"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).detail(fixture["target_id"]))

    assert exc.value.code == "DETAIL_REDIRECTED", exc.value.code
    assert exc.value.evidence.get("requested_id") == fixture["target_id"]


def test_x_tweet_empty_ok_is_detail_redirected(tmp_path, monkeypatch, x_auth) -> None:
    """离线：上游明示 0 条 → 目标不存在（DETAIL_REDIRECTED），不是契约错。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch, [kit.ok_json(fixture["variants"]["empty_ok"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).detail(fixture["target_id"]))
    assert exc.value.code == "DETAIL_REDIRECTED", exc.value.code


def test_x_tweet_empty_unverified_is_contract_error(tmp_path, monkeypatch, x_auth) -> None:
    """离线：空列表但无法判定 → 契约错误（不是"正文为空"）。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["empty_unverified"]["payload"])])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).detail(fixture["target_id"]))
    assert exc.value.code == "CONTRACT_CHANGED", exc.value.code


def test_x_long_post_truncated_is_partial(tmp_path, monkeypatch, x_auth) -> None:
    """离线：长帖被截断 → detail_status=partial，不当完整正文。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["long_post_truncated"]["payload"])])

    rec = asyncio.run(XSource(exe).detail("1900000000000000040"))

    assert rec.detail_status == "partial", rec.detail_status
    assert rec.metrics["truncated"] is True


def test_x_long_post_article_text_used(tmp_path, monkeypatch, x_auth) -> None:
    """离线：给出更完整的 articleText → 用它作正文，状态 complete。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["long_post_with_article_text"]["payload"])])

    rec = asyncio.run(XSource(exe).detail("1900000000000000041"))

    assert rec.evidence_text.endswith("应被采用为证据正文。"), rec.evidence_text
    assert rec.detail_status == "complete", rec.detail_status
    assert rec.title == "样本长文标题"


def test_x_quote_and_retweet_recorded(tmp_path, monkeypatch, x_auth) -> None:
    """离线：引用原帖 ID 与转发人都必须记录，且转发人同样匿名化。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["quote_and_retweet"]["payload"])])

    rec = asyncio.run(XSource(exe).detail("1900000000000000050"))

    assert rec.metrics["is_retweet"] is True
    assert rec.metrics["quoted_tweet_id"] == "1900000000000000051"
    assert rec.metrics["retweeted_by"].startswith("anon_"), rec.metrics["retweeted_by"]
    assert "sample_retweeter" not in rec.metrics["retweeted_by"]
    # is_retweet 只是标记：source_id 仍是这条转推自己的 ID，去重口径是 (platform, source_id)
    assert rec.source_id == "1900000000000000050"


def test_x_twitter_created_at_only_is_parsed(tmp_path, monkeypatch, x_auth) -> None:
    """离线：只有 Twitter 原生时间串也能解析成 UTC ISO8601。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["twitter_created_at_only"]["payload"])])

    rec = asyncio.run(XSource(exe).detail("1900000000000000060"))

    assert rec.published_at == "2026-09-25T10:20:30Z", rec.published_at
    assert rec.published_at_quality == "absolute"


def test_x_unknown_date_kept_raw(tmp_path, monkeypatch, x_auth) -> None:
    """离线：无法识别的时间 → published_at 置 None，原文进 published_at_raw。"""
    fixture = load_fixture("x_tweet.json")
    exe = _x_exe(tmp_path)
    kit.fake_opencli(monkeypatch,
                     [kit.ok_json(fixture["variants"]["unknown_date_format"]["payload"])])

    rec = asyncio.run(XSource(exe).detail("1900000000000000070"))

    assert rec.published_at is None, rec.published_at
    assert rec.published_at_raw == "not-a-real-date"
    assert rec.published_at_quality == "unparsed"


def test_x_manifest_payload_is_dependency_missing(tmp_path, monkeypatch, x_auth) -> None:
    """离线：rc=0 但 stdout 是命令清单 → DEPENDENCY_MISSING。"""
    exe = _x_exe(tmp_path)
    manifest = [
        {"site": "twitter", "name": "search", "columns": ["id"],
         "modulePath": "twitter/search.js"},
        {"site": "twitter", "name": "tweet", "columns": ["id"],
         "modulePath": "twitter/tweet.js"},
    ]
    kit.fake_opencli(monkeypatch, [kit.ok_json(manifest, returncode=0)])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).search("q"))
    assert exc.value.code == "DEPENDENCY_MISSING", exc.value.code


def test_x_without_credentials_is_auth_required(tmp_path, monkeypatch) -> None:
    """离线：未配置凭据 → AUTH_REQUIRED，且不发起子进程。"""
    monkeypatch.delenv("TWITTER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_CT0", raising=False)
    exe = _x_exe(tmp_path)
    rec = kit.fake_opencli(monkeypatch, [])

    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).search("q"))

    assert exc.value.code == "AUTH_REQUIRED", exc.value.code
    assert rec.count == 0, "未配置凭据时不应发起任何子进程"


def test_x_detail_requires_locator(tmp_path, monkeypatch, x_auth) -> None:
    """离线：缺推文 ID → LOCATOR_MISSING。"""
    exe = _x_exe(tmp_path)
    with pytest.raises(CollectorError) as exc:
        asyncio.run(XSource(exe).detail(""))
    assert exc.value.code == "LOCATOR_MISSING", exc.value.code
