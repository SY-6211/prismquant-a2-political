"""One durable global web execution fence; user-scoped finite allowances."""
from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import text
from app.agent_qa import MODEL,PROMPT_VERSION,EXECUTABLE_PROMPTS,request_payload
from app.agent_tools import encoded
from app.annotation_model import digest
from app.model_trial_live import units
from app.repositories import chat,qa

def money(n):return str(Decimal(n)/Decimal(10**9))
def lock(c):c.execute(text('SELECT pg_advisory_xact_lock(626184313)'))
def cost_limit(context):
    payload=request_payload(context)
    return units(Decimal(len(encoded(payload))+2048)*Decimal('.000001')+Decimal(payload['max_tokens'])*Decimal('.000002')+Decimal('.0005'))
def grant(c,user_id,approval,cap,max_requests,hours):
    if not 0<cap<=10**10 or not 1<=max_requests<=100 or not 1<=hours<=24 or not approval.strip() or len(approval)>120:raise chat.Invalid('授权范围不正确。')
    if not c.execute(text('SELECT user_id FROM agent_chat_users WHERE user_id=:id AND disabled_at IS NULL'),{'id':user_id}).scalar_one_or_none():raise chat.Missing()
    lock(c)
    if c.execute(text('SELECT 1 FROM agent_web_qa_grants WHERE approval=:value'),{'value':approval}).scalar_one_or_none():raise chat.Conflict('该授权编号已使用，不能重置。')
    return dict(c.execute(text('INSERT INTO agent_web_qa_grants(grant_id,approval,user_id,cap,max_requests,expires_at) VALUES (:id,:approval,:user,:cap,:calls,now()+make_interval(hours=>:hours)) RETURNING grant_id,approval,user_id,cap,max_requests,expires_at'),{'id':uuid4(),'approval':approval,'user':user_id,'cap':cap,'calls':max_requests,'hours':hours}).mappings().one())
def summary(c,user_id):
    global_stop=c.execute(text("SELECT state FROM agent_web_qa_jobs WHERE state IN ('queued','running','unknown') ORDER BY CASE WHEN state='unknown' THEN 0 ELSE 1 END LIMIT 1")).scalar_one_or_none()
    grants=[dict(v) for v in c.execute(text('SELECT * FROM agent_web_qa_grants WHERE user_id=:user ORDER BY created_at DESC'),{'user':user_id}).mappings()]
    selected=next((v for v in grants if v['closed_at'] is None and v['expires_at']>datetime.now(timezone.utc)),None)
    if selected is None:return {'can_generate':False,'reason':'not_enabled','message':'本轮在线回答额度尚未开启或已到期。','remaining_requests':0}
    rows=c.execute(text('SELECT reserved,actual,state FROM agent_web_qa_jobs WHERE grant_id=:id'),{'id':selected['grant_id']}).mappings().all()
    spent=sum(v['actual'] if v['actual'] is not None and v['state']!='unknown' else max(v['reserved'],v['actual'] or 0) for v in rows)
    available=max(0,selected['cap']-spent);count=max(0,selected['max_requests']-len(rows))
    reason='needs_review' if global_stop=='unknown' else 'busy' if global_stop else 'exhausted' if not count or not available else 'ready'
    return {'can_generate':reason=='ready','reason':reason,'message':{'needs_review':'有响应或费用待核对，已停止新生成。','busy':'有回答正在执行或待核对，请先等待或检查原任务。','exhausted':'本轮额度或次数已用尽。','ready':'在线回答可用；每次发送计入本轮授权。'}[reason],
        'grant_id':str(selected['grant_id']),'approval':selected['approval'],'cap_usd':money(selected['cap']),'used_or_reserved_usd':money(spent),'remaining_usd':money(available),'remaining_requests':count,'expires_at':selected['expires_at']}
def enqueue(c,user_id,conversation_id,run_id):
    lock(c);row=qa.detail(c,user_id,conversation_id,run_id)
    existing=c.execute(text('SELECT * FROM agent_web_qa_jobs WHERE run_id=:id'),{'id':run_id}).mappings().one_or_none()
    if existing:return {'created':False,'run_id':str(run_id),'state':existing['state']}
    if row['status']!='prepared':raise chat.Conflict('此回答已结束或执行待核对，不能重复生成。')
    if row['model_id']!=MODEL or row['prompt_version'] not in EXECUTABLE_PROMPTS or row['context'].get('prompt_version')!=row['prompt_version'] or digest(row['context'])!=row['context_hash']:raise chat.Invalid('冻结输入不匹配或提示词较早，请准备新版本。')
    allowance=summary(c,user_id)
    if not allowance['can_generate']:raise chat.Conflict(allowance['message'])
    amount=cost_limit(row['context'])
    if units(allowance['remaining_usd'])<amount:raise chat.Conflict('剩余额度不足以预留本次回答，请缩小资料范围或另行设置预算。')
    fingerprint=digest(request_payload(row['context']));qa.claim(c,user_id,conversation_id,run_id,fingerprint)
    c.execute(text('INSERT INTO agent_web_qa_jobs(run_id,grant_id,request_hash,reserved) VALUES (:run,:grant,:hash,:reserved)'),{'run':run_id,'grant':allowance['grant_id'],'hash':fingerprint,'reserved':amount})
    return {'created':True,'run_id':str(run_id),'state':'queued'}
def start(c,token,conversation_id,run_id):
    identity=chat.authenticate(c,token);row=qa.detail(c,identity['user_id'],conversation_id,run_id)
    changed=c.execute(text("UPDATE agent_web_qa_jobs SET state='running',started_at=clock_timestamp() WHERE run_id=:id AND state='queued' RETURNING *"),{'id':run_id}).mappings().one_or_none()
    return {**row,**dict(changed)} if changed else None
def mark_sent(c,token,conversation_id,run_id):
    identity=chat.authenticate(c,token);conv=chat.conversation(c,identity['user_id'],conversation_id)
    if conv['archived']:raise chat.Conflict('会话已归档。')
    value=c.execute(text("UPDATE agent_web_qa_jobs j SET sent_at=clock_timestamp() FROM agent_web_qa_grants g WHERE j.run_id=:id AND j.state='running' AND j.sent_at IS NULL AND j.grant_id=g.grant_id AND g.closed_at IS NULL AND g.expires_at>now() RETURNING j.run_id"),{'id':run_id}).scalar_one_or_none()
    if value is None:raise chat.Conflict('授权已停止或任务已执行。')
def finish(c,run_id,result):
    row=c.execute(text('SELECT * FROM agent_web_qa_jobs WHERE run_id=:id FOR UPDATE'),{'id':run_id}).mappings().one()
    if result['request_hash']!=row['request_hash']:raise ValueError('Receipt mismatch')
    qa.save_result(c,run_id,result['status'],result['answer'],result['response_hash'],result['receipt'],failure=result['failure_code'])
    actual=units(result['receipt']['reported_cost_usd']) if result['receipt']['reported_cost_usd'] is not None else None
    c.execute(text('UPDATE agent_web_qa_jobs SET state=:state,actual=:actual,finished_at=clock_timestamp() WHERE run_id=:id'),{'state':result['status'],'actual':actual,'id':run_id})
    return {'run_id':str(run_id),'status':result['status']}
def close(c,grant_id):
    lock(c);c.execute(text('UPDATE agent_web_qa_grants SET closed_at=coalesce(closed_at,clock_timestamp()) WHERE grant_id=:id'),{'id':grant_id});return {'closed':True}
def cancel_queued(c,run_id):
    lock(c);row=c.execute(text('SELECT * FROM agent_web_qa_jobs WHERE run_id=:id FOR UPDATE'),{'id':run_id}).mappings().one()
    if row['state']!='queued' or row['started_at'] is not None or row['sent_at'] is not None:raise chat.Conflict('任务已开始，不能推断尚未发送。')
    value={'request_hash':row['request_hash'],'status':'failed','answer':None,'response_hash':digest({'not_sent':row['request_hash']}),'receipt':{'model_requests':0,'reported_cost_usd':'0','billing_status':'settled'},'failure_code':'queued_task_cancelled_before_start'}
    return finish(c,run_id,value)
