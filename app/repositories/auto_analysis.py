"""Public analysis automation; finite approvals are separate from private QA."""
from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import text
from app.agent_analysis import MODEL,PROMPT_VERSION,request_payload
from app.agent_tools import encoded
from app.annotation_model import digest
from app.model_trial_live import units
from app.repositories import agent_analysis as analyses

def lock(c):c.execute(text('SELECT pg_advisory_xact_lock(626184314)'))
def money(value):return str(Decimal(value)/Decimal(10**9))
def active(grant):return grant['closed_at'] is None and grant['pause_reason'] is None and grant['expires_at']>datetime.now(timezone.utc)
def amount(context):return units(Decimal(len(encoded(request_payload(context)))+2048)*Decimal('.000001')+Decimal(1600)*Decimal('.000002')+Decimal('.0005'))

def grant(c,person_id,approval,cap,max_requests,hours):
    if not approval.strip() or len(approval)>120 or not 0<cap<=10**10 or not 1<=max_requests<=100 or not 1<=hours<=24:raise ValueError('Invalid finite approval')
    lock(c)
    if c.execute(text('SELECT 1 FROM agent_auto_grants WHERE approval=:approval'),{'approval':approval}).scalar_one_or_none():raise ValueError('Approval already used')
    if not c.execute(text('SELECT 1 FROM people WHERE person_id=:person'),{'person':person_id}).scalar_one_or_none():raise ValueError('Unknown person')
    if c.execute(text('SELECT 1 FROM agent_auto_grants WHERE person_id=:person AND closed_at IS NULL AND expires_at>now()'),{'person':person_id}).scalar_one_or_none():raise ValueError('Close prior policy first')
    clock=c.execute(text('SELECT epoch,position FROM content_stream_clock WHERE singleton')).mappings().one()
    return dict(c.execute(text('INSERT INTO agent_auto_grants(grant_id,approval,person_id,cap,max_requests,epoch,cursor_position,expires_at) VALUES (:id,:approval,:person,:cap,:calls,:epoch,:cursor,now()+make_interval(hours=>:hours)) RETURNING *'),
        {'id':uuid4(),'approval':approval,'person':person_id,'cap':cap,'calls':max_requests,'epoch':clock['epoch'],'cursor':clock['position'],'hours':hours}).mappings().one())

def close(c,grant_id):
    lock(c);c.execute(text('UPDATE agent_auto_grants SET closed_at=coalesce(closed_at,clock_timestamp()) WHERE grant_id=:id'),{'id':grant_id})
    return {'closed':True}

def budget(c,grant_id):
    grant=dict(c.execute(text('SELECT * FROM agent_auto_grants WHERE grant_id=:id'),{'id':grant_id}).mappings().one())
    rows=c.execute(text('SELECT reserved,actual,state FROM agent_auto_jobs WHERE grant_id=:id AND reserved IS NOT NULL'),{'id':grant_id}).mappings().all()
    spent=sum(row['actual'] if row['actual'] is not None and row['state']!='unknown' else max(row['reserved'],row['actual'] or 0) for row in rows)
    pending=c.execute(text("SELECT state FROM agent_auto_jobs WHERE state IN ('running','unknown') ORDER BY CASE WHEN state='unknown' THEN 0 ELSE 1 END LIMIT 1")).scalar_one_or_none()
    reason='disabled' if not active(grant) else 'needs_review' if pending=='unknown' else 'busy' if pending else 'exhausted' if len(rows)>=grant['max_requests'] or spent>=grant['cap'] else 'ready'
    return {**grant,'status':reason,'remaining_requests':max(0,grant['max_requests']-len(rows)),'used_or_reserved_usd':money(spent),'remaining_usd':money(max(0,grant['cap']-spent))}

def sync(c,approval):
    lock(c);grants=c.execute(text('SELECT * FROM agent_auto_grants WHERE approval=:approval FOR UPDATE'),{'approval':approval}).mappings().all()
    total=0
    for g in grants:
        if not active(g) or budget(c,g['grant_id'])['status']!='ready':continue
        clock=c.execute(text('SELECT epoch,position FROM content_stream_clock WHERE singleton')).mappings().one()
        floor=c.execute(text('SELECT min(position) FROM content_changes')).scalar_one()
        if clock['epoch']!=g['epoch'] or g['cursor_position']>clock['position'] or (floor is not None and g['cursor_position']<floor-1):
            c.execute(text("UPDATE agent_auto_grants SET pause_reason='content_stream_gap' WHERE grant_id=:id"),{'id':g['grant_id']});continue
        changes=c.execute(text('SELECT * FROM content_changes WHERE position>:cursor AND position<=:head ORDER BY position LIMIT 100'),{'cursor':g['cursor_position'],'head':clock['position']}).mappings().all()
        if (not changes and clock['position']>g['cursor_position']) or any(row['position']!=g['cursor_position']+index+1 for index,row in enumerate(changes)):
            c.execute(text("UPDATE agent_auto_grants SET pause_reason='content_stream_gap' WHERE grant_id=:id"),{'id':g['grant_id']});continue
        for row in changes:
            if row['person_id']!=g['person_id'] or row['review_status']!='accepted':continue
            result=c.execute(text('INSERT INTO agent_auto_jobs(job_id,grant_id,event_id,version_id,person_id,model_id,prompt_version) VALUES (:id,:grant,:event,:version,:person,:model,:prompt) ON CONFLICT(version_id,model_id,prompt_version) DO NOTHING'),
                {'id':uuid4(),'grant':g['grant_id'],'event':row['event_id'],'version':row['version_id'],'person':row['person_id'],'model':MODEL,'prompt':PROMPT_VERSION})
            total+=result.rowcount
        cursor=changes[-1]['position'] if changes else clock['position']
        c.execute(text('UPDATE agent_auto_grants SET cursor_position=:cursor WHERE grant_id=:id'),{'cursor':cursor,'id':g['grant_id']})
    return {'enqueued':total,'model_requests':0}

def eligible(c,row):
    current=c.execute(text('SELECT v.review_status,v.person_id FROM event_versions v WHERE v.event_id=:event AND v.version_id=:version'),{'event':row['event_id'],'version':row['version_id']}).mappings().one()
    latest=c.execute(text('SELECT version_id FROM event_observations WHERE event_id=:event ORDER BY observed_at DESC,observation_id DESC LIMIT 1'),{'event':row['event_id']}).scalar_one()
    return current['review_status']=='accepted' and current['person_id']==row['person_id'] and latest==row['version_id']

def skip(c,job_id,code):
    c.execute(text("UPDATE agent_auto_jobs SET state='skipped',failure_code=:code,finished_at=clock_timestamp() WHERE job_id=:id AND state IN ('queued','preparing','prepared')"),{'id':job_id,'code':code})

def claim_prepare(c,approval):
    lock(c)
    rows=c.execute(text("SELECT j.* FROM agent_auto_jobs j JOIN agent_auto_grants g USING(grant_id) WHERE g.approval=:approval AND j.state='queued' ORDER BY j.created_at,j.job_id LIMIT 1 FOR UPDATE OF j"),{'approval':approval}).mappings().all()
    if not rows:return None
    row=dict(rows[0]);g=budget(c,row['grant_id'])
    if g['status']!='ready':return None
    if not eligible(c,row):skip(c,row['job_id'],'superseded_or_unaccepted');return {'skipped':True}
    cached=c.execute(text("SELECT r.run_id FROM agent_analysis_runs r JOIN agent_analysis_results a USING(run_id) WHERE r.version_id=:version AND r.model_id=:model AND r.prompt_version=:prompt AND a.status='completed' AND a.execution_kind='openrouter' ORDER BY a.created_at DESC LIMIT 1"),{'version':row['version_id'],'model':MODEL,'prompt':PROMPT_VERSION}).scalar_one_or_none()
    if cached:
        c.execute(text("UPDATE agent_auto_jobs SET state='cached',run_id=:run,finished_at=clock_timestamp() WHERE job_id=:id"),{'id':row['job_id'],'run':cached});return {'cached':True}
    c.execute(text("UPDATE agent_auto_jobs SET state='preparing' WHERE job_id=:id"),{'id':row['job_id']});return row

def prepared(c,job_id,context):
    lock(c);row=c.execute(text('SELECT * FROM agent_auto_jobs WHERE job_id=:id FOR UPDATE'),{'id':job_id}).mappings().one()
    if row['state']!='preparing' or not active(budget(c,row['grant_id'])):raise ValueError('Preparation no longer active')
    if any(str(row[k])!=str(context[k]) for k in ('event_id','version_id','person_id')) or context['prompt_version']!=row['prompt_version']:raise ValueError('Wrong prepared scope')
    if analyses.context_has_reserved(c,context):skip(c,job_id,'reserved_evaluation_source');return
    run=analyses.prepared(c,'auto:'+str(job_id),context,MODEL,PROMPT_VERSION)
    c.execute(text("UPDATE agent_auto_jobs SET state='prepared',run_id=:run,request_hash=:hash WHERE job_id=:id"),{'run':run['run_id'],'hash':digest(request_payload(run['context'])),'id':job_id})

def start(c,approval):
    lock(c)
    rows=c.execute(text("SELECT j.* FROM agent_auto_jobs j JOIN agent_auto_grants g USING(grant_id) WHERE g.approval=:approval AND j.state='prepared' ORDER BY j.created_at,j.job_id LIMIT 1 FOR UPDATE OF j"),{'approval':approval}).mappings().all()
    if not rows:return None
    row=dict(rows[0]);g=budget(c,row['grant_id'])
    if g['status']!='ready':return None
    if not eligible(c,row):skip(c,row['job_id'],'superseded_or_unaccepted');return None
    run=analyses.select_detail(c,row['run_id'])
    if run['status'] is not None:raise ValueError('Already has result')
    if run['model_id']!=MODEL or run['prompt_version']!=PROMPT_VERSION or digest(run['context'])!=run['context_hash'] or digest(request_payload(run['context']))!=row['request_hash']:raise ValueError('Frozen input differs')
    if analyses.context_has_reserved(c,run['context']):skip(c,row['job_id'],'reserved_evaluation_source');return None
    reserved=amount(run['context'])
    if units(g['remaining_usd'])<reserved:
        c.execute(text("UPDATE agent_auto_grants SET pause_reason='insufficient_reservation' WHERE grant_id=:id"),{'id':row['grant_id']});return None
    c.execute(text("UPDATE agent_auto_jobs SET state='running',reserved=:amount,started_at=clock_timestamp() WHERE job_id=:id"),{'id':row['job_id'],'amount':reserved})
    return {**run,**row,'reserved':reserved}

def mark_sent(c,job_id):
    lock(c);row=c.execute(text('SELECT * FROM agent_auto_jobs WHERE job_id=:id FOR UPDATE'),{'id':job_id}).mappings().one()
    if row['state']!='running' or row['sent_at'] is not None or not active(budget(c,row['grant_id'])) or not eligible(c,row):raise ValueError('Sending no longer allowed')
    if analyses.context_has_reserved(c,analyses.select_detail(c,row['run_id'])['context']):raise ValueError('Reserved source')
    c.execute(text('UPDATE agent_auto_jobs SET sent_at=clock_timestamp() WHERE job_id=:id'),{'id':job_id})

def finish(c,job_id,value):
    lock(c);row=c.execute(text('SELECT * FROM agent_auto_jobs WHERE job_id=:id FOR UPDATE'),{'id':job_id}).mappings().one()
    if str(row['run_id'])!=value['run_id'] or row['request_hash']!=value['request_hash']:raise ValueError('Receipt mismatch')
    analyses.save_result(c,row['run_id'],value['status'],value['analysis'],value['response_hash'],value['receipt'],failure_code=value['failure_code'])
    actual=units(value['receipt']['reported_cost_usd']) if value['receipt']['reported_cost_usd'] is not None else None
    c.execute(text('UPDATE agent_auto_jobs SET state=:state,actual=:actual,failure_code=:failure,finished_at=clock_timestamp() WHERE job_id=:id'),{'id':job_id,'state':value['status'],'actual':actual,'failure':value['failure_code']})
    if value['failure_code']=='not_sent_preflight_failed':
        c.execute(text("UPDATE agent_auto_grants SET pause_reason='preflight_failed' WHERE grant_id=:id"),{'id':row['grant_id']})
    return {'job_id':str(job_id),'run_id':value['run_id'],'status':value['status']}

def status(c):
    policies=[]
    for g in c.execute(text('SELECT grant_id FROM agent_auto_grants ORDER BY created_at DESC LIMIT 50')).scalars():
        value=budget(c,g);policies.append({k:value[k] for k in ('grant_id','person_id','status','remaining_requests','remaining_usd','used_or_reserved_usd','expires_at','pause_reason','cursor_position')})
    counts=dict(c.execute(text('SELECT state,count(*) FROM agent_auto_jobs GROUP BY state')).all())
    return {'enabled_count':sum(p['status'] in ('ready','busy') for p in policies),'policies':policies,'jobs':counts,'model':'deepseek/deepseek-v3.2','read_only':True,'worker_state':'not_measured','message':'自动分析未启用；不会消费模型。' if not policies else '这是授权与任务状态；工作进程是否持续运行需另行核对。'}
