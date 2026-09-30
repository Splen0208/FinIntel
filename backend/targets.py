"""finintel targets：目标 JD 统一表示（library / paste 两条来源 → 同一 evidence_text + section_ranges）。

library 来源从 job_snapshot 读 responsibilities_text/requirements_text，复用
analyzer.build_evidence_text() 组装并物化到 job_target（快照不可变，等价安全）；
paste 来源直接用粘贴文本组装。evidence_text 生成后永不 UPDATE（D5/§7.7）。
"""
from __future__ import annotations

from typing import Any, Optional

import analyzer
import schemas
import store
from store import assert_scope, get_conn, jdumps, jloads, new_id, query_all, query_one, utcnow_iso


class TargetError(Exception):
    def __init__(self, code: str, message: str, operator_action: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.operator_action = operator_action


def create_target(session_id: str, source_kind: str, snapshot_id: Optional[str] = None,
                  job_id: Optional[str] = None, title: Optional[str] = None,
                  company_raw: Optional[str] = None,
                  responsibilities_text: Optional[str] = None,
                  requirements_text: Optional[str] = None) -> dict:
    if source_kind not in ("library", "paste"):
        raise TargetError("INVALID_ANALYSIS", "source_kind 必须为 library 或 paste",
                          operator_action="选择 JD 来源")

    if source_kind == "library":
        snap = None
        if snapshot_id:
            snap = query_one("SELECT * FROM job_snapshot WHERE id=?", (snapshot_id,))
        elif job_id:
            snap = query_one(
                "SELECT * FROM job_snapshot WHERE job_id=? ORDER BY fetched_at DESC LIMIT 1", (job_id,))
        if not snap:
            raise TargetError("INVALID_ANALYSIS", "未找到对应的岗位快照",
                              operator_action="在岗位库中选择一个已采集详情的岗位")
        if snap["detail_status"] not in ("complete", "partial"):
            raise TargetError("DETAIL_MISSING", f"快照状态为 {snap['detail_status']}，无可用正文",
                              operator_action="选择正文完整的岗位")
        job = query_one("SELECT * FROM job WHERE id=?", (snap["job_id"],))
        responsibilities_text = snap["responsibilities_text"]
        requirements_text = snap["requirements_text"]
        title = job["title"] if job else title
        company_raw = job["company_name_raw"] if job else company_raw
        evidence_text, ranges = analyzer.build_evidence_text(responsibilities_text, requirements_text)
        evidence_text = schemas.normalize_evidence_text(evidence_text)
        target_id = new_id("jtgt")
        conn = get_conn()
        sql = ("INSERT INTO job_target (id, session_id, source_kind, snapshot_id, job_id, title, "
               "company_display, evidence_text, responsibilities_text, requirements_text, "
               "section_ranges, mapping_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(sql, "jobseeker")
        conn.execute(sql, (target_id, session_id or None, source_kind, snap["id"], snap["job_id"],
                           title or "", company_raw, evidence_text, responsibilities_text,
                           requirements_text, jdumps(_ranges_dict(ranges)), schemas.MAPPING_VERSION,
                           utcnow_iso()))
        conn.commit()
    else:  # paste
        if not (responsibilities_text or "").strip() and not (requirements_text or "").strip():
            raise TargetError("INVALID_ANALYSIS", "paste 来源至少需提供职责或任职要求之一",
                              operator_action="粘贴 JD 正文")
        evidence_text, ranges = analyzer.build_evidence_text(responsibilities_text, requirements_text)
        evidence_text = schemas.normalize_evidence_text(evidence_text)
        if len(evidence_text) > schemas.MAX_JD_TEXT_CHARS:
            raise TargetError("INVALID_ANALYSIS", f"JD 正文超过 {schemas.MAX_JD_TEXT_CHARS} 字符，请精简")
        target_id = new_id("jtgt")
        conn = get_conn()
        sql = ("INSERT INTO job_target (id, session_id, source_kind, snapshot_id, job_id, "
               "pasted_title, pasted_company_raw, title, company_display, evidence_text, "
               "responsibilities_text, requirements_text, section_ranges, mapping_version, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
        assert_scope(sql, "jobseeker")
        conn.execute(sql, (target_id, session_id or None, source_kind, None, None,
                           title, company_raw, title or "", company_raw, evidence_text,
                           responsibilities_text, requirements_text, jdumps(_ranges_dict(ranges)),
                           schemas.MAPPING_VERSION, utcnow_iso()))
        conn.commit()

    # 绑定会话并推进状态
    if session_id:
        conn = get_conn()
        upd = ("UPDATE jobseeker_session SET job_target_id=?, research_version=research_version+1, "
               "status=CASE WHEN resume_id IS NULL THEN 'jd_ready' ELSE 'resume_ready' END, updated_at=? WHERE id=?")
        assert_scope(upd, "jobseeker")
        conn.execute(upd, (target_id, utcnow_iso(), session_id))
        conn.commit()

    return target_view(target_id)


def _ranges_dict(ranges: dict) -> dict:
    """build_evidence_text 返回 list 区间，转成 JSON 友好的可序列化结构。"""
    out = {}
    for k, v in ranges.items():
        out[k] = [int(v[0]), int(v[1])] if v else None
    return out


def get_target(job_target_id: str) -> Optional[dict]:
    return query_one("SELECT * FROM job_target WHERE id=?", (job_target_id,))


def target_view(job_target_id: str) -> dict:
    t = query_one("SELECT * FROM job_target WHERE id=?", (job_target_id,))
    if not t:
        raise TargetError("INVALID_ANALYSIS", "目标 JD 不存在")
    return {
        "id": t["id"],
        "source_kind": t["source_kind"],
        "snapshot_id": t["snapshot_id"],
        "job_id": t["job_id"],
        "title": t["title"],
        "company_display": t["company_display"],
        "evidence_text": t["evidence_text"],
        "responsibilities_text": t["responsibilities_text"],
        "requirements_text": t["requirements_text"],
        "section_ranges": jloads(t["section_ranges"], {}),
        "char_count": len(t["evidence_text"]),
        "created_at": t["created_at"],
    }
