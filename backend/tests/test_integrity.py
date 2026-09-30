"""Regression tests for real audit defects, with isolated demo data and no network."""
import copy
import subprocess
import pytest
import test_pipeline as base
import analysis
import analyzer
import login_bridge
import runs
import schemas
import store
from store import query_all, query_one, jloads, new_id
from upstream_contract import map_detail


@pytest.fixture(autouse=True)
def demo():
    base._setting("demo_mode", "1")
    store.execute("DELETE FROM login_task")


def submit_for(run_id, snapshot_ids, mutate=lambda items: items):
    batch = analysis.prepare_analysis(run_id, snapshot_ids)
    items = [analyzer.analyze_snapshot(s) for s in batch["snapshots"]]
    return analysis.submit_analysis({"batch_token": batch["batch_token"],
        "submission_id": new_id("sub"), "model_label": "integrity-test",
        "analyses": mutate(items)})


def test_failed_analysis_does_not_complete_and_retry_preserves_success(monkeypatch):
    rid = base._run_demo("信贷 产品经理 integrity mixed")
    snapshots = base.PIPELINE._snapshot_ids(rid)
    assert len(snapshots) > 1
    store.execute("UPDATE run_job SET analysis_id=NULL, analysis_error=NULL WHERE run_id=?", (rid,))
    existing_detail_failures = base.PIPELINE._failed_count(rid)
    # Existing checkpoint claims every snapshot completed: receipts must override it.
    failed_sid = snapshots[0]
    def invalid(items):
        for item in items:
            if item["snapshot_id"] == failed_sid:
                assert item["findings"]
                item["findings"][0]["evidence"][0]["quote"] = "not present in original"
        return items
    original = analysis.auto_analyze
    monkeypatch.setattr(analysis, "auto_analyze", lambda run, ids: submit_for(run, ids, invalid))
    base.PIPELINE._organize_phase(rid)
    run = runs.get_run(rid)
    assert run["stage"] == "partial" and run["failed_count"] == existing_detail_failures + 1
    assert base.PIPELINE._accounting(rid)["analysis_failed"] == 1
    assert failed_sid not in jloads(run["checkpoint"], {})["analyzed_snapshot_ids"]
    successful = {r["snapshot_id"]: r["analysis_id"] for r in query_all(
        "SELECT * FROM run_job WHERE run_id=? AND analysis_id IS NOT NULL", (rid,))}
    called = []
    def retry(run, ids):
        called.extend(ids)
        return original(run, ids)
    monkeypatch.setattr(analysis, "auto_analyze", retry)
    base.PIPELINE._organize_phase(rid)
    assert called == [failed_sid]
    assert runs.get_run(rid)["failed_count"] == existing_detail_failures
    for sid, aid in successful.items():
        assert query_one("SELECT analysis_id FROM run_job WHERE run_id=? AND snapshot_id=?", (rid, sid))["analysis_id"] == aid


@pytest.mark.parametrize("mode", ["missing", "duplicate"])
def test_submission_requires_exactly_one_item_per_snapshot(mode):
    rid = base._run_demo("信贷 产品经理 coverage " + mode)
    ids = base.PIPELINE._snapshot_ids(rid)[:2]
    def mutate(items):
        return items[1:] if mode == "missing" else items + [copy.deepcopy(items[0])]
    result = submit_for(rid, ids, mutate)
    assert result["failed_count"] == 1 and result["ok_count"] == 1
    receipt = query_one("SELECT * FROM run_job WHERE run_id=? AND snapshot_id=?", (rid, ids[0]))
    assert receipt["analysis_id"] is None and receipt["analysis_error"]


def test_fullness_is_not_inferred_from_nonempty_description():
    mapped = map_detail({"job_id": "j", "title": "t", "company": "c",
                         "description": "负责系统建设。" * 80}, security_id="s")
    assert mapped["detail_status"] == "partial"
    assert mapped["detail_source"] == "cli_detail_unverified"
    assert mapped["mapping_warning"]


def test_retry_feedback_is_scoped_to_failed_snapshot_in_same_run():
    rid = base._run_demo("信贷 产品经理 validation feedback")
    ids = base.PIPELINE._snapshot_ids(rid)[:2]
    error = {"code": "INVALID_ANALYSIS", "field": "result_status",
             "message": "排除类别的岗位不得写入正常公司事项"}
    store.execute("UPDATE run_job SET analysis_error=? WHERE run_id=? AND snapshot_id=?",
                  (store.jdumps([error]), rid, ids[0]))
    batch = analysis.prepare_analysis(rid, ids)
    by_id = {s["snapshot_id"]: s for s in batch["snapshots"]}
    assert by_id[ids[0]]["validation_feedback"] == [error]
    assert "validation_feedback" not in by_id[ids[1]]
    content = analysis._batch_to_user_content(batch)
    payload = jloads(content.split("\n\n", 1)[0], {})
    assert payload["snapshots"][0]["validation_feedback"] == [error]
    assert "不得为了通过校验改变岗位分类" in content


def test_partial_body_survives_storage_and_reaches_analysis(monkeypatch):
    rid = runs.create_run("partial body", {"source": "demo", "queries": ["消费金融"]})["id"]
    runs.do_search(rid, "q1", page=1)
    refs = [r["job_ref"] for r in query_all("SELECT job_ref FROM run_job WHERE run_id=? LIMIT 1", (rid,))]
    # Demo detail source has the same storage pipeline as real detail data.
    assert refs
    store.execute("UPDATE job_snapshot SET detail_status='partial' WHERE job_id IN (SELECT job_id FROM run_job WHERE run_id=? AND job_ref=?)", (rid, refs[0]))
    import source_demo as target
    original_detail = target.detail
    def partial(*args, **kwargs):
        result = original_detail(*args, **kwargs)
        result["detail_status"] = "partial"
        result["mapping_warning"] = "unverified"
        return result
    monkeypatch.setattr(target, "detail", partial)
    result = runs.fetch_details(rid, refs)
    sid = result["outcomes"][0]["snapshot_id"]
    assert query_one("SELECT detail_status FROM job_snapshot WHERE id=?", (sid,))["detail_status"] == "partial"
    runs.finish_collection(rid)
    batch = analysis.prepare_analysis(rid, [sid])
    assert batch["snapshots"][0]["detail_status"] == "partial"


def test_generic_statement_filter_preserves_specific_business_object():
    assert analysis.is_generic_statement("独立主导项目，跨团队沟通、协调资源，对项目结果负责。")
    assert not analysis.is_generic_statement("协调数据团队建设反欺诈规则引擎")


def test_new_fullness_receipt_does_not_mutate_an_old_snapshot():
    rid=base._run_demo('信贷 产品经理 immutable completeness')
    row=query_one('SELECT * FROM run_job WHERE run_id=? AND snapshot_id IS NOT NULL LIMIT 1',(rid,))
    sid=row['snapshot_id']
    store.execute("UPDATE job_snapshot SET detail_status='partial',detail_source='demo' WHERE id=?",(sid,))
    other=runs.create_run('full body receipt',{'source':'demo','queries':['消费金融']})['id']
    ref=new_id('ref')
    store.execute('INSERT INTO run_job(run_id,job_id,detail_outcome,job_ref) VALUES(?,?,?,?)',
        (other,row['job_id'],'pending',ref))
    result=runs.fetch_details(other,[ref])
    new_sid=result['outcomes'][0]['snapshot_id']
    assert new_sid != sid
    assert query_one('SELECT detail_status FROM job_snapshot WHERE id=?',(sid,))['detail_status']=='partial'
    assert query_one('SELECT detail_status FROM job_snapshot WHERE id=?',(new_sid,))['detail_status']=='complete'


def test_other_run_analysis_cannot_change_original_directions():
    rid = base._run_demo("信贷 产品经理 isolated versions")
    before = {r["id"]: r["evidence_count"] for r in query_all("SELECT * FROM run_direction WHERE run_id=?", (rid,))}
    selected = query_one("SELECT * FROM run_job WHERE run_id=? AND analysis_id IS NOT NULL", (rid,))
    other = runs.create_run("other version", {"source": "demo", "queries": ["消费金融"]})["id"]
    store.execute("INSERT INTO run_job(run_id,job_id,snapshot_id,detail_outcome,job_ref) VALUES(?,?,?,?,?)",
        (other, selected["job_id"], selected["snapshot_id"], "complete", new_id("ref")))
    runs.finish_collection(other)
    submit_for(other, [selected["snapshot_id"]])
    new_aid = query_one("SELECT analysis_id FROM run_job WHERE run_id=?", (other,))["analysis_id"]
    assert new_aid != selected["analysis_id"]
    store.execute("UPDATE finding SET statement='不应出现在原任务的新版本方向' WHERE analysis_id=?", (new_aid,))
    base.PIPELINE._build_directions(rid)
    after = {r["id"]: r["evidence_count"] for r in query_all("SELECT * FROM run_direction WHERE run_id=?", (rid,))}
    assert before == after
    # Removing an accepted direction and rebuilding must remove its stale materialization.
    store.execute("UPDATE finding SET review_status='excluded' WHERE analysis_id=?", (selected["analysis_id"],))
    base.PIPELINE._build_directions(rid)
    for row in query_all("SELECT * FROM run_direction WHERE run_id=?", (rid,)):
        for fid in jloads(row["finding_ids"], []):
            assert query_one("SELECT analysis_id FROM finding WHERE id=?", (fid,))["analysis_id"] != selected["analysis_id"]


def test_taskkill_failure_uses_bounded_fallback(monkeypatch):
    class Process:
        pid = 123
        def __init__(self): self.terminated = False
        def terminate(self): self.terminated = True
        def wait(self, timeout): assert timeout <= 5
    process = Process()
    def failed(*args, **kwargs):
        assert kwargs["timeout"] <= 3
        return subprocess.CompletedProcess(args, 1)
    monkeypatch.setattr(login_bridge.subprocess, "run", failed)
    login_bridge._kill_tree(process)
    assert process.terminated
