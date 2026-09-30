"""User-selected social background, kept separate from JD and resume facts."""
from __future__ import annotations
from urllib.parse import urlsplit,urlunsplit
import schemas
import social_store
import store
from store import query_one,query_all,get_conn,new_id,utcnow_iso,jdumps,jloads

MAX_MATERIALS=8

class ResearchError(Exception):
    def __init__(self,message,code='INVALID_ANALYSIS'):
        self.code=code
        self.message=message
        super().__init__(message)

def _session(session_id):
    row=query_one('SELECT * FROM jobseeker_session WHERE id=?',(session_id,))
    if not row: raise ResearchError('面试准备会话不存在')
    return row

def _source(material):
    if material.get('source_snapshot_id'):
        return query_one('''SELECT s.evidence_text,s.detail_status AS source_status,i.platform,i.canonical_url
            FROM social_snapshot s JOIN social_item i ON i.id=s.item_id WHERE s.id=?''',(material['source_snapshot_id'],))
    return query_one('''SELECT s.evidence_text,s.content_status AS source_status,i.platform,i.canonical_url
        FROM social_comment_snapshot s JOIN social_comment c ON c.id=s.comment_id
        JOIN social_item i ON i.id=c.item_id WHERE s.id=?''',(material['comment_snapshot_id'],))

def _public_url(value):
    try:
        url=urlsplit(value or '')
        if url.scheme not in ('https','http') or url.username or url.password: return None
        return urlunsplit((url.scheme,url.netloc,url.path,'',''))
    except ValueError: return None

def materials_view(session_id):
    session=_session(session_id)
    rows=query_all('SELECT * FROM interview_material WHERE session_id=? ORDER BY created_at,id',(session_id,))
    for row in rows:
        source=_source(row)
        text=source['evidence_text'] if source else ''
        if not text or text[row['start']:row['end']]!=row['quote']:
            raise ResearchError('背景材料的引用与保存原文不一致，未交给模型')
        start=max(0,row['start']-350)
        end=min(len(text),row['end']+350)
        row['context_text']=text[start:end]
        row['context_start']=start
        row['material_id']=row['id']
        row['source_status']=source['source_status']
    version=int(session['research_version'])
    plan_version=int(session['prep_research_version'])
    has_plan=bool(query_one('SELECT id FROM prep_task WHERE session_id=? LIMIT 1',(session_id,)))
    return {'materials':rows,'version':version,'plan_version':plan_version,
        'plan_stale':has_plan and version!=plan_version,'origin_run_id':session.get('origin_run_id'),
        'boundary':'X／小红书材料是公开讨论背景，不能作为岗位要求、本人经历或真实面试题的证明。'}

def add_finding(session_id,finding_id):
    _session(session_id)
    finding=query_one('''SELECT sf.id,sf.title,sf.summary_zh,sa.run_id
        FROM social_finding sf JOIN social_analysis sa ON sa.id=sf.analysis_id WHERE sf.id=?''',(finding_id,))
    if not finding:
        raise ResearchError('只能选择已有 X／小红书结论的原文证据')
    evidence=social_store.evidence_by_finding(finding_id)
    candidates=[]
    seen=set()
    for row in evidence[:3]:
        quote=row.get('quote') or ''
        start,end=row.get('start'),row.get('end')
        source={'source_snapshot_id':row.get('snapshot_id'),'comment_snapshot_id':row.get('comment_snapshot_id')}
        raw=_source(source)
        if not raw or raw['platform'] not in ('x','xiaohongshu'):
            raise ResearchError('材料来源不受支持')
        text=raw['evidence_text'] if raw else ''
        if not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(text) or text[start:end]!=quote:
            raise ResearchError('结论存在无法核验的原文证据，未加入准备材料')
        if len(quote)>1500: raise ResearchError('单条背景引用过长，请选择较短的结论证据')
        duplicate=query_one('''SELECT id FROM interview_material WHERE session_id=?
            AND COALESCE(source_snapshot_id,'')=? AND COALESCE(comment_snapshot_id,'')=? AND start=? AND end=?''',
            (session_id,source['source_snapshot_id'] or '',source['comment_snapshot_id'] or '',start,end))
        key=(source['source_snapshot_id'],source['comment_snapshot_id'],start,end)
        if not duplicate and key not in seen:
            candidates.append((row,source,raw))
            seen.add(key)
    if not evidence: raise ResearchError('该结论尚无可核验的原文证据')
    count=query_one('SELECT COUNT(*) n FROM interview_material WHERE session_id=?',(session_id,))['n']
    if count+len(candidates)>MAX_MATERIALS: raise ResearchError('每份面试准备最多选择 8 条背景证据，请先移除不需要的材料')
    conn=get_conn()
    with conn:
        for row,source,raw in candidates:
            conn.execute('''INSERT INTO interview_material
                (id,session_id,platform,source_snapshot_id,comment_snapshot_id,source_run_id,title,source_url,quote,start,end,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (new_id('im'),session_id,raw['platform'],source['source_snapshot_id'],source['comment_snapshot_id'],
                 finding['run_id'],(finding['title'] or finding['summary_zh'] or '')[:300],_public_url(raw['canonical_url']),
                 row['quote'],row['start'],row['end'],utcnow_iso()))
        if candidates:
            conn.execute('UPDATE jobseeker_session SET research_version=research_version+1,updated_at=? WHERE id=?',(utcnow_iso(),session_id))
    return materials_view(session_id)

def start_from_job(job_id,run_id=None,snapshot_id=None):
    """Freeze the selected result's snapshot, never substitute a newer job revision."""
    import matching
    import targets
    if run_id:
        relation=query_one('SELECT snapshot_id FROM run_job WHERE run_id=? AND job_id=?',(run_id,job_id))
        if not relation or not relation['snapshot_id']:
            raise ResearchError('该岗位在本次搜索中尚无可用正文')
        if snapshot_id and snapshot_id!=relation['snapshot_id']:
            raise ResearchError('岗位快照不属于所选搜索结果')
        snapshot_id=relation['snapshot_id']
    if not snapshot_id:
        raise ResearchError('请选择搜索结果中的岗位快照')
    snap=query_one('SELECT * FROM job_snapshot WHERE id=? AND job_id=?',(snapshot_id,job_id))
    if not snap or snap['detail_status'] not in ('complete','partial') or not snap['evidence_text'].strip():
        raise ResearchError('该岗位尚无可用 JD 正文，请选择已读取详情的岗位')
    sid=matching.create_session()['session']['id']
    try:
        targets.create_target(sid,'library',snapshot_id=snapshot_id,job_id=job_id)
        conn=get_conn()
        with conn:
            conn.execute('UPDATE jobseeker_session SET origin_run_id=? WHERE id=?',(run_id,sid))
    except Exception:
        conn=get_conn()
        with conn:
            conn.execute('DELETE FROM job_target WHERE session_id=?',(sid,))
            conn.execute('DELETE FROM jobseeker_session WHERE id=?',(sid,))
        raise
    return matching.get_session(sid)

def remove_material(session_id,material_id):
    _session(session_id)
    row=query_one('SELECT id FROM interview_material WHERE id=? AND session_id=?',(material_id,session_id))
    if not row: raise ResearchError('背景材料不属于当前会话')
    conn=get_conn()
    with conn:
        conn.execute('DELETE FROM interview_material WHERE id=? AND session_id=?',(material_id,session_id))
        conn.execute('UPDATE jobseeker_session SET research_version=research_version+1,updated_at=? WHERE id=?',(utcnow_iso(),session_id))
    return materials_view(session_id)

def validate_questions(session_id,version,questions,allowed_ids):
    view=materials_view(session_id)
    if view['version']!=version: raise ResearchError('背景材料已变更，请重新生成准备计划','VERSION_CONFLICT')
    materials={row['id']:row for row in view['materials'] if row['id'] in allowed_ids}
    if not isinstance(questions,list) or len(questions)>MAX_MATERIALS:
        raise ResearchError('背景问题应为最多 8 项的列表')
    if materials and not questions: raise ResearchError('已选择背景材料，但模型未返回带原文依据的背景练习问题')
    for item in questions:
        if not isinstance(item,dict): raise ResearchError('背景问题格式不正确')
        material=materials.get(item.get('material_id'))
        if not material: raise ResearchError('背景问题引用了未选择或其他会话的材料')
        text=_source(material)['evidence_text']
        start,end=item.get('start'),item.get('end')
        quote=item.get('quote')
        if (not isinstance(start,int) or not isinstance(end,int) or not isinstance(quote,str)
            or not material['start']<=start<end<=material['end'] or text[start:end]!=quote):
            raise ResearchError('背景问题引用必须逐字匹配所选原文证据')
        if not isinstance(item.get('question'),str) or not 1<=len(item['question'])<=300:
            raise ResearchError('背景练习问题缺失或过长')
        if not isinstance(item.get('why_relevant'),str) or not item['why_relevant'].strip():
            raise ResearchError('背景练习问题需说明与目标岗位的联系')
        limitations=item.get('limitations')
        if not isinstance(limitations,list) or not limitations or any(not isinstance(v,str) or not v.strip() for v in limitations):
            raise ResearchError('背景练习问题必须保留不确定性说明')
        if schemas.has_banned_phrase(item['question']+' '+item['why_relevant']):
            raise ResearchError('背景材料不能生成确定录用或面试结果的承诺')
    if set(materials)!=set(item['material_id'] for item in questions):
        raise ResearchError('所选背景证据未全部覆盖，请重新生成背景练习问题')
    return questions

def save_questions(session_id,version,questions):
    conn=get_conn()
    with conn:
        conn.execute('DELETE FROM interview_research_question WHERE session_id=?',(session_id,))
        for item in questions:
            conn.execute('''INSERT INTO interview_research_question
                (id,session_id,material_id,question,why_relevant,quote,start,end,limitations,research_version,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(new_id('irq'),session_id,item['material_id'],item['question'],
                 item['why_relevant'],item['quote'],item['start'],item['end'],jdumps(item['limitations']),version,utcnow_iso()))
        conn.execute('UPDATE jobseeker_session SET prep_research_version=? WHERE id=?',(version,session_id))

def questions_view(session_id):
    view=materials_view(session_id)
    questions=query_all('''SELECT q.*,m.platform,m.source_url,m.source_run_id,m.title
        FROM interview_research_question q JOIN interview_material m ON m.id=q.material_id
        WHERE q.session_id=? ORDER BY q.created_at,q.id''',(session_id,))
    for row in questions: row['limitations']=jloads(row['limitations'],[])
    view['questions']=questions
    return view

def demo_questions(prepared):
    return [{'material_id':row['id'],'question':'这段公开讨论提出了什么问题？你会怎样核验，并说明它与目标岗位的关系？',
        'why_relevant':'结合目标 JD 做背景研究与表达练习；不把公开讨论当成岗位事实。',
        'quote':row['quote'],'start':row['start'],'end':row['end'],
        'limitations':['合成演示练习，不代表招聘方真实面试问题。']}
        for row in prepared.get('research_background',{}).get('materials',[])][:MAX_MATERIALS]
