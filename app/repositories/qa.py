"""Owner-scoped frozen QA inputs and immutable results; no model invocation."""
import json
from uuid import uuid4
from sqlalchemy import text
from app.annotation_model import digest
from app.repositories import chat


def by_request(c,owner,conversation_id,request_id):
    chat.conversation(c,owner,conversation_id)
    row=c.execute(text('SELECT r.* FROM agent_qa_runs r WHERE r.conversation_id=:c AND r.client_request_id=:request'),{'c':conversation_id,'request':request_id}).mappings().one_or_none()
    return dict(row) if row else None


def question(c,owner,conversation_id,message_id,include_history=True):
    conv=chat.conversation(c,owner,conversation_id)
    if conv['archived']:raise chat.Conflict('会话已归档，请先取消归档。')
    if conv['person_id'] is None:raise chat.Invalid('请先创建绑定监控人物的会话。')
    row=c.execute(text('SELECT * FROM agent_chat_messages WHERE conversation_id=:c AND message_id=:m'),{'c':conversation_id,'m':message_id}).mappings().one_or_none()
    if row is None:raise chat.Missing()
    if not include_history:return {**dict(row),'person_id':conv['person_id'],'history':[],'history_window_truncated':False}
    history=[dict(v) for v in c.execute(text("SELECT m.message_id,m.position,left(m.content,800) AS question,char_length(m.content)>800 AS question_truncated,left(a.answer->>'answer_zh',800) AS answer_zh,a.answer->'research_steps' AS research_steps,a.answer->'evidence_gaps_zh' AS evidence_gaps_zh,a.answer->'source_resources' AS source_resources,a.run_id AS answer_run_id "
        "FROM agent_chat_messages m LEFT JOIN LATERAL (SELECT a.answer,r.run_id FROM agent_qa_runs r JOIN agent_qa_results a USING(run_id) "
        "WHERE r.message_id=m.message_id AND a.status IN ('completed','abstained') "
        "AND NOT EXISTS(SELECT 1 FROM annotation_samples held CROSS JOIN LATERAL jsonb_array_elements(coalesce(r.context->'sources','[]'::jsonb)) src "
        "WHERE held.split='blind' AND held.origin='source' AND (held.event_key=src->>'event_id' OR held.snapshot->>'event_version_id'=src->>'version_id')) "
        "ORDER BY a.created_at DESC,a.result_id DESC LIMIT 1) a ON true "
        "WHERE m.conversation_id=:c AND m.position<:position ORDER BY m.position DESC LIMIT 51"),
        {'c':conversation_id,'position':row['position']}).mappings()]
    return {**dict(row),'person_id':conv['person_id'],'history':list(reversed(history[:50])), 'history_window_truncated':len(history)>50}


def save_prepared(c,owner,conversation_id,request_id,spec,fingerprint,context,model,prompt):
    conv=chat.conversation(c,owner,conversation_id,lock=True)
    if conv['archived']:raise chat.Conflict('会话已归档。')
    if str(conv['person_id'])!=context['person_id'] or str(conversation_id)!=context['conversation_id'] or str(spec['message_id'])!=context['message_id']:raise chat.Invalid('问答输入身份不匹配。')
    row=c.execute(text('INSERT INTO agent_qa_runs(run_id,conversation_id,message_id,client_request_id,request_hash,request_spec,context,context_hash,model_id,prompt_version) '
        'VALUES (:id,:c,:m,:request,:hash,CAST(:spec AS jsonb),CAST(:context AS jsonb),:context_hash,:model,:prompt) '
        'ON CONFLICT(conversation_id,client_request_id) DO NOTHING RETURNING run_id'),
        {'id':uuid4(),'c':conversation_id,'m':spec['message_id'],'request':request_id,'hash':fingerprint,
         'spec':json.dumps(spec,ensure_ascii=False),'context':json.dumps(context,ensure_ascii=False),'context_hash':digest(context),'model':model,'prompt':prompt}).scalar_one_or_none()
    value=by_request(c,owner,conversation_id,request_id)
    if value['request_hash']!=fingerprint:raise chat.Conflict('相同提交标识已用于不同问答范围。')
    from app import research_sources
    if context['prompt_version']==research_sources.PROMPT and row is not None:
        answer=research_sources.validate(context,research_sources.answer(context))
        save_result(c,value['run_id'],'completed' if answer['outcome']=='answered' else 'abstained',answer,digest(answer),
                    {'model_requests':0,'reported_cost_usd':'0','network_requests':context['source_lookup']['network_requests']},kind='local_source_navigation')
    elif not context['sources'] and not context.get('background_sources') and context.get('research_task')!='method_checklist' and row is not None:
        from app.agent_qa import abstention
        answer=abstention(context,'本次有限检索未取得可用原文；这不代表人物没有发表相关内容。')
        save_result(c,value['run_id'],'abstained',answer,digest(answer),{'model_requests':0,'reported_cost_usd':'0'},kind='local_abstention')
    return value


def detail(c,owner,conversation_id,run_id):
    chat.conversation(c,owner,conversation_id)
    row=c.execute(text("SELECT r.*,a.result_id,a.answer,a.execution_kind,a.failure_code,a.receipt,a.created_at AS result_created_at, "
        "coalesce(a.status,CASE WHEN x.run_id IS NOT NULL THEN 'executing_or_unfinished' ELSE 'prepared' END) AS status "
        "FROM agent_qa_runs r LEFT JOIN agent_qa_results a USING(run_id) LEFT JOIN agent_qa_executions x USING(run_id) "
        'WHERE r.conversation_id=:c AND r.run_id=:run'),{'c':conversation_id,'run':run_id}).mappings().one_or_none()
    if row is None:raise chat.Missing()
    return dict(row)


def listing(c,owner,conversation_id,limit=20,cursor=None,message_id=None):
    chat.conversation(c,owner,conversation_id)
    if cursor is not None:detail(c,owner,conversation_id,cursor)
    return [dict(r) for r in c.execute(text("SELECT r.run_id,r.message_id,r.model_id,r.prompt_version,r.created_at,a.execution_kind, "
        "a.answer->>'answer_zh' AS answer_zh,coalesce(a.status,CASE WHEN x.run_id IS NOT NULL THEN 'executing_or_unfinished' ELSE 'prepared' END) AS status "
        'FROM agent_qa_runs r LEFT JOIN agent_qa_results a USING(run_id) LEFT JOIN agent_qa_executions x USING(run_id) '
        'WHERE r.conversation_id=:c AND (CAST(:message AS uuid) IS NULL OR r.message_id=:message) '
        'AND (CAST(:cursor AS uuid) IS NULL OR (r.created_at,r.run_id)<(SELECT created_at,run_id FROM agent_qa_runs WHERE run_id=:cursor AND conversation_id=:c)) '
        'ORDER BY r.created_at DESC,r.run_id DESC LIMIT :limit'),{'c':conversation_id,'message':message_id,'cursor':cursor,'limit':limit+1}).mappings()]


def claim(c,owner,conversation_id,run_id,request_hash):
    conv=chat.conversation(c,owner,conversation_id,lock=True)
    if conv['archived']:raise chat.Conflict('会话已归档。')
    row=detail(c,owner,conversation_id,run_id)
    if row['status']!='prepared':raise chat.Conflict('该问答已执行、结束或需要核对，不能重复调用模型。')
    c.execute(text('INSERT INTO agent_qa_executions(run_id,request_hash) VALUES (:id,:hash)'),{'id':run_id,'hash':request_hash})
    return row


def save_result(c,run_id,status,answer,response_hash,receipt,kind='openrouter',failure=None):
    if status not in ('completed','abstained','rejected','failed','unknown') or (status in ('completed','abstained'))!=(answer is not None):raise ValueError('Invalid QA result')
    if answer is not None and len(json.dumps(answer,ensure_ascii=False).encode())>65536:raise ValueError('Answer too large')
    c.execute(text('INSERT INTO agent_qa_results(result_id,run_id,status,answer,response_hash,receipt,execution_kind,failure_code) '
        'VALUES (:id,:run,:status,CAST(:answer AS jsonb),:hash,CAST(:receipt AS jsonb),:kind,:failure) ON CONFLICT(run_id) DO NOTHING'),
        {'id':uuid4(),'run':run_id,'status':status,'answer':json.dumps(answer,ensure_ascii=False) if answer is not None else None,
         'hash':response_hash,'receipt':json.dumps(receipt,ensure_ascii=False),'kind':kind,'failure':failure})
    row=dict(c.execute(text('SELECT * FROM agent_qa_results WHERE run_id=:run'),{'run':run_id}).mappings().one())
    if any(row[k]!=v for k,v in {'status':status,'answer':answer,'response_hash':response_hash,'receipt':receipt,'execution_kind':kind,'failure_code':failure}.items()):raise chat.Conflict('已有问答结果不同，旧回答不能覆盖。')
    return row


def write_result(settings,*args,**kwargs):
    engine=chat.engine_for(settings)
    try:
        with engine.begin() as c:return save_result(c,*args,**kwargs)
    finally:engine.dispose()
