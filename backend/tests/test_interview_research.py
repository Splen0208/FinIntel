"""Offline evidence-backed interview workflow; no platform or model network calls."""
import copy
from pathlib import Path
import pytest
import store
import matching
import prep
import resume
import reports
import targets
import social_store
import interview_research as research
from social_testkit import isolated_db, with_run, seed_note, seed_comment

FIXTURES=Path(__file__).resolve().parent.parent/'fixtures'

@pytest.fixture
def db(monkeypatch):
    with isolated_db():
        monkeypatch.setattr('ai_provider.ai_config',lambda:{'configured':False})
        store.execute("INSERT OR REPLACE INTO app_setting(key,value) VALUES('demo_mode','1')")
        yield

def seed_job():
    rid=with_run(task_type='boss_research',source='demo')
    jid,sid=store.new_id('job'),store.new_id('snap')
    now=store.utcnow_iso()
    resp='负责信贷产品需求分析、用户研究与业务流程设计。'
    req='本科及以上学历，具备产品经理经验，熟悉数据分析与需求文档编写。'
    store.execute('''INSERT INTO job(id,source,identity_key,company_name_raw,title,first_seen_at,last_seen_at)
        VALUES (?,?,?,?,?,?,?)''',(jid,'demo',jid,'合成演示公司','合成信贷产品经理',now,now))
    store.execute('''INSERT INTO job_snapshot(id,job_id,content_hash,fetched_at,detail_status,evidence_text,
        responsibilities_text,requirements_text,mapping_version,source_url) VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (sid,jid,'offline-original',now,'complete',resp+'\n'+req,resp,req,'evidence_map_v1','https://www.zhipin.com/job_detail/offline.html'))
    store.execute('INSERT INTO run_job(run_id,job_id,snapshot_id,detail_outcome,job_ref) VALUES(?,?,?,?,?)',
        (rid,jid,sid,'complete',jid))
    return rid,jid,sid

def seed_finding(platform='x',comment=False):
    rid=with_run(task_type='x_topics' if platform=='x' else 'xhs_career',source=platform)
    text='__OFFLINE_FIXTURE__ 公开讨论：产品设计需要把用户反馈与可验证的业务数据结合。'
    note=seed_note(rid,store.new_id('offline'),text,platform=platform,
        canonical_url='https://x.com/offline/status/100?auth_token=offline' if platform=='x' else 'https://www.xiaohongshu.com/explore/offline?xsec_token=offline')
    aid=social_store.save_analysis(rid,input_hash=rid,schema_version='offline',prompt_version='offline',model_label='offline',status='ok',is_current=True)
    fid=social_store.save_finding(aid,kind='topic',title='合成背景讨论',summary_zh='合成示例，不代表平台事实')
    quote='产品设计需要把用户反馈与可验证的业务数据结合。'
    start=text.index(quote)
    ids={'snapshot_id':note['snapshot_id']}
    if comment:
        c=seed_comment(rid,note['item_id'],text)
        ids={'comment_snapshot_id':c['comment_snapshot_id']}
    social_store.save_evidence(fid,quote=quote,start=start,end=start+len(quote),stance='support',**ids)
    return fid,note

def prepared_session():
    rid,jid,snapshot=seed_job()
    sid=research.start_from_job(jid,rid)['session']['id']
    fid,_=seed_finding('x')
    research.add_finding(sid,fid)
    fid,_=seed_finding('xiaohongshu',True)
    research.add_finding(sid,fid)
    rv=resume.import_text('合成演示简历', (FIXTURES/'resumes/resume_high_match.txt').read_text(encoding='utf-8'))
    matching.update_session(sid,resume_id=rv['resume']['id'])
    assert matching.auto_match(sid)['ok_count']>0
    return sid

def submit_body(prepared):
    body=prep.rule_prep(prepared)
    return {'batch_token':prepared['batch_token'],'submission_id':store.new_id('sub'),
        'model_label':'rule-matcher-demo-v1',**body,'research_questions':research.demo_questions(prepared)}

def test_freezes_selected_run_snapshot_and_rejects_other_run(db):
    rid,jid,snap=seed_job()
    store.execute('''INSERT INTO job_snapshot(id,job_id,content_hash,fetched_at,detail_status,evidence_text,
        responsibilities_text,mapping_version) VALUES(?,?,?,?,?,?,?,?)''',
        ('offline-newer',jid,'newer','2099-01-01T00:00:00Z','complete','新版职责','新版职责','evidence_map_v1'))
    data=research.start_from_job(jid,rid)
    assert data['job_target']['snapshot_id']==snap
    assert '新版' not in targets.target_view(data['job_target']['id'])['evidence_text']
    with pytest.raises(research.ResearchError): research.start_from_job(jid,rid,'offline-newer')
    with pytest.raises(research.ResearchError): research.start_from_job(jid,'wrong-run')

def test_selected_evidence_is_readonly_deduplicated_and_owned(db):
    sid=matching.create_session()['session']['id']
    fid,note=seed_finding('xiaohongshu',True)
    raw=store.query_one('SELECT * FROM social_snapshot WHERE id=?',(note['snapshot_id'],))
    view=research.add_finding(sid,fid)
    again=research.add_finding(sid,fid)
    assert len(again['materials'])==1 and again['version']==view['version']
    row=again['materials'][0]
    assert row['comment_snapshot_id'] and row['platform']=='xiaohongshu'
    assert '?' not in row['source_url'] and 'xsec_token' not in row['source_url']
    assert store.query_one('SELECT * FROM social_snapshot WHERE id=?',(note['snapshot_id'],))==raw
    other=matching.create_session()['session']['id']
    with pytest.raises(research.ResearchError): research.remove_material(other,row['id'])

def test_whole_three_source_flow_outputs_quotes_and_report(db,monkeypatch,tmp_path):
    sid=prepared_session()
    p=prep.prepare_prep(sid)
    content=store.jloads(prep._prep_to_user_content(p),{})
    assert 'category' in content['output_contract']['$defs']['FocusItem']['required']
    assert {m['platform'] for m in content['research_background']['materials']}=={'x','xiaohongshu'}
    # Source text must not be copied into resume or JD requirements.
    assert '公开讨论' not in content['resume']['evidence_text']
    assert '用户反馈与可验证' not in content['jd']['evidence_text']
    assert prep.submit_prep(submit_body(p))['ok_count']==1
    v=prep.prep_view(sid)['research_background']
    assert len(v['questions'])==2 and not v['plan_stale']
    md=reports._build_prep_body(store.query_one('SELECT * FROM jobseeker_session WHERE id=?',(sid,)))['markdown']
    assert '公开讨论背景与练习问题' in md and 'https://x.com/offline/status/100' in md
    assert '不代表招聘方真实面试问题' in md
    research.remove_material(sid,v['materials'][0]['id'])
    assert research.questions_view(sid)['plan_stale']
    md=reports._build_prep_body(store.query_one('SELECT * FROM jobseeker_session WHERE id=?',(sid,)))['markdown']
    assert '计划已过期' in md

@pytest.mark.parametrize('invalid',['quote','other_material','missing_limits','missing_coverage','stale'])
def test_rejects_invalid_or_stale_model_evidence_without_replacing_plan(db,invalid):
    sid=prepared_session()
    initial=prep.prepare_prep(sid)
    assert prep.submit_prep(submit_body(initial))['ok_count']==1
    old=[q['id'] for q in research.questions_view(sid)['questions']]
    p=prep.prepare_prep(sid)
    body=submit_body(p)
    if invalid=='quote': body['research_questions'][0]['quote']='编造原文'
    if invalid=='other_material': body['research_questions'][0]['material_id']='foreign-material'
    if invalid=='missing_limits': body['research_questions'][0]['limitations']=[]
    if invalid=='missing_coverage': body['research_questions'].pop()
    if invalid=='stale':
        fid,_=seed_finding('x'); research.add_finding(sid,fid)
    with pytest.raises(prep.PrepError): prep.submit_prep(body)
    assert [q['id'] for q in research.questions_view(sid)['questions']]==old

def test_resume_change_requires_rematching_and_marks_plan_stale(db):
    sid=prepared_session()
    assert prep.auto_prep(sid)['ok_count']==1
    p=prep.prepare_prep(sid)
    r=resume.import_text('另一份合成简历',(FIXTURES/'resumes/resume_transferable.txt').read_text(encoding='utf-8'))
    matching.update_session(sid,resume_id=r['resume']['id'])
    assert research.questions_view(sid)['plan_stale']
    assert matching.get_session(sid)['session']['status']=='resume_ready'
    with pytest.raises(prep.PrepError): prep.submit_prep(submit_body(p))
    with pytest.raises(prep.PrepError): prep.prepare_prep(sid)

def test_stale_match_batch_cannot_mark_new_inputs_matched(db):
    sid=prepared_session()
    p=matching.prepare_match(sid)
    body=matching.rule_match(p)
    r=resume.import_text('另一份合成简历',(FIXTURES/'resumes/resume_transferable.txt').read_text(encoding='utf-8'))
    matching.update_session(sid,resume_id=r['resume']['id'])
    with pytest.raises(matching.MatchError) as error:
        matching.submit_match({'batch_token':p['batch_token'],'submission_id':store.new_id('sub'),
            'model_label':'offline',**body})
    assert error.value.code=='VERSION_CONFLICT'
    assert matching.get_session(sid)['session']['status']=='resume_ready'

def test_material_limit_rejects_ninth_evidence(db):
    sid=matching.create_session()['session']['id']
    for _ in range(8):
        fid,_=seed_finding();research.add_finding(sid,fid)
    fid,_=seed_finding()
    with pytest.raises(research.ResearchError): research.add_finding(sid,fid)
    assert len(research.materials_view(sid)['materials'])==8

def test_real_model_contract_receives_selected_sources(db,monkeypatch):
    sid=prepared_session()
    p=prep.prepare_prep(sid)
    expected=submit_body(p)
    seen=[]
    def model(system,user,**kwargs):
        seen.append(store.jloads(user,{}))
        return {k:expected[k] for k in ['focus_items','prep_tasks','research_questions']}
    monkeypatch.setattr('ai_provider.chat_json',model)
    monkeypatch.setattr('ai_provider.ai_config',lambda:{'configured':True,'provider':'offline','model':'contract'})
    assert prep.prep_via_model(p)['ok_count']==1
    assert len(seen[0]['research_background']['materials'])==2

def test_model_repairs_only_unique_exact_selected_quote_offsets(db,monkeypatch):
    sid=prepared_session()
    p=prep.prepare_prep(sid)
    expected=submit_body(p)
    for q in expected['research_questions']: q['start'],q['end']=99999,100001
    monkeypatch.setattr('ai_provider.chat_json',lambda *a,**kw:copy.deepcopy(expected))
    monkeypatch.setattr('ai_provider.ai_config',lambda:{'configured':True,'provider':'offline','model':'contract'})
    result=prep.prep_via_model(p)
    assert result['ok_count']==1 and result['repaired_offsets']>=2
    p=prep.prepare_prep(sid)
    expected['research_questions'][0]['quote']='编造的未选原文'
    with pytest.raises(prep.PrepError): prep.prep_via_model(p)

def test_api_bridge_and_material_routes(db):
    from fastapi.testclient import TestClient
    import app
    rid,jid,_=seed_job()
    with TestClient(app.app) as client:
        r=client.post('/api/interview/start',json={'job_id':jid,'run_id':rid}).json()
        assert r['ok']
        sid=r['data']['session']['id']
        fid,_=seed_finding()
        added=client.post(f'/api/js/sessions/{sid}/research',json={'finding_id':fid}).json()
        assert added['ok'] and len(added['data']['materials'])==1
        mid=added['data']['materials'][0]['id']
        assert client.delete(f'/api/js/sessions/{sid}/research/{mid}').json()['ok']
        assert not client.post(f'/api/js/sessions/{sid}/research',json={'finding_id':'missing'}).json()['ok']

@pytest.mark.parametrize('base,structured', [('https://api.deepseek.com/v1',True),('http://127.0.0.1:9999/v1',False)])
def test_model_json_transport_contract(db,monkeypatch,base,structured):
    import ai_provider,json
    monkeypatch.setattr(ai_provider,'_require_config',lambda:(base,'offline-model','offline-key'))
    captured=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}).encode()
    def send(req,**kwargs):
        captured.append(json.loads(req.data));return Response()
    monkeypatch.setattr(ai_provider.urllib.request,'urlopen',send)
    assert ai_provider.chat_json('JSON','offline')['ok']
    if structured:
        assert captured[0]['response_format']=={'type':'json_object'}
        assert captured[0]['max_tokens']==8192
    else: assert 'response_format' not in captured[0]

def test_large_jd_matching_batches_preserve_all_requirements(db,monkeypatch):
    sid=prepared_session()
    p=matching.prepare_match(sid)
    original=matching.rule_match(p)
    p['jd']['evidence_text'] += '\n__OFFLINE_FIXTURE__ '+('原文背景。'*250)
    requirements=[];rows=[]
    for i in range(11):
        req=copy.deepcopy(original['requirements'][0]);req['ref']=f'R{i}'
        row=copy.deepcopy(original['match_rows'][0]);row['requirement_ref']=f'R{i}'
        requirements.append(req);rows.append(row)
    batches=[]
    def model(system,user,**kwargs):
        inp=store.jloads(user,{})
        if 'selected_requirements' not in inp: return {'requirements':requirements}
        refs={r['ref'] for r in inp['selected_requirements']};batches.append(refs)
        return {'match_rows':[r for r in rows if r['requirement_ref'] in refs],'clarifications':[]}
    monkeypatch.setattr('ai_provider.chat_json',model)
    monkeypatch.setattr('ai_provider.ai_config',lambda:{'configured':True,'provider':'offline','model':'batch-contract'})
    assert matching.match_via_model(p)['ok_count']==1
    assert [len(b) for b in batches]==[5,5,1]
    assert len(matching.match_view(sid)['rows'])==11

def test_browser_source_does_not_inherit_other_cookie_channel_auth_error(db,monkeypatch):
    import channel_state,health,social_source_status
    monkeypatch.setattr(channel_state,'channel_view',lambda:{'browser_source':'existing-browser',
        'state':'ready','check_source':'doctor','checked_at':'2026-09-30T00:00:00Z'})
    monkeypatch.setattr(health,'cached_layer_view',lambda *a,**kw:pytest.fail('Cannot read another channel cookie cache'))
    view=social_source_status.check_platform('zhipin').as_view()
    assert view['connection_state']=='ready' and view['auth_state']=='unknown'
    assert view['status']=='unknown' and view['last_error_code'] is None
