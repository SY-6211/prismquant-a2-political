"""Append-only Agent analyses. Public read views never expose provider credentials."""
import json
from uuid import uuid4
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from app.core.database import database_url,create_read_engine
from app.annotation_model import digest


def write_engine(settings):
    return create_engine(database_url(settings),poolclass=NullPool,hide_parameters=True,
        connect_args={'connect_timeout':5,'options':'-c lock_timeout=5000 -c statement_timeout=5000'})


def prepared(connection,request_id,context,model_id,prompt_version):
    if len(json.dumps(context,ensure_ascii=False).encode())>65536:raise ValueError('Context too large')
    identity=uuid4()
    connection.execute(text('INSERT INTO agent_analysis_runs(run_id,request_id,event_id,version_id,person_id,model_id,prompt_version,context,context_hash) '
        'VALUES (:id,:request,:event,:version,:person,:model,:prompt,CAST(:context AS jsonb),:hash) ON CONFLICT(request_id) DO NOTHING'),
        {'id':identity,'request':request_id,'event':context['event_id'],'version':context['version_id'],'person':context['person_id'],
         'model':model_id,'prompt':prompt_version,'context':json.dumps(context,ensure_ascii=False),'hash':digest(context)})
    row=dict(connection.execute(text('SELECT * FROM agent_analysis_runs WHERE request_id=:id'),{'id':request_id}).mappings().one())
    if any(str(row[k])!=str(context[k]) for k in ('event_id','version_id','person_id')) or row['model_id']!=model_id or row['prompt_version']!=prompt_version:
        raise ValueError('Idempotency key conflict')
    return row


def save_result(connection,run_id,status,analysis,response_hash,receipt,execution_kind='openrouter',failure_code=None):
    if status not in ('completed','rejected','failed','unknown'):raise ValueError('Invalid status')
    if (status=='completed')!=(analysis is not None):raise ValueError('Invalid result body')
    if len(json.dumps(analysis,ensure_ascii=False).encode())>65536:raise ValueError('Analysis too large')
    result_id=uuid4()
    connection.execute(text('INSERT INTO agent_analysis_results(result_id,run_id,status,analysis,response_hash,receipt,execution_kind,failure_code) '
        'VALUES (:id,:run,:status,CAST(:analysis AS jsonb),:hash,CAST(:receipt AS jsonb),:kind,:failure) '
        'ON CONFLICT(run_id) DO NOTHING'),{'id':result_id,'run':run_id,'status':status,
        'analysis':json.dumps(analysis,ensure_ascii=False) if analysis is not None else None,
        'hash':response_hash,'receipt':json.dumps(receipt,ensure_ascii=False),'kind':execution_kind,'failure':failure_code})
    row=dict(connection.execute(text('SELECT * FROM agent_analysis_results WHERE run_id=:id'),{'id':run_id}).mappings().one())
    if row['response_hash']!=response_hash or row['status']!=status or row['execution_kind']!=execution_kind or row['analysis']!=analysis or row['receipt']!=receipt:
        raise ValueError('Existing result differs; create a new analysis run')
    return row


def write_prepared(settings,*args):
    engine=write_engine(settings)
    try:
        with engine.begin() as c:return prepared(c,*args)
    finally:engine.dispose()


def write_result(settings,*args,**kwargs):
    engine=write_engine(settings)
    try:
        with engine.begin() as c:return save_result(c,*args,**kwargs)
    finally:engine.dispose()


def read_by_request(settings,request_id):
    engine=create_read_engine(settings)
    try:
        with engine.connect() as c:
            row=c.execute(text('SELECT * FROM agent_analysis_runs WHERE request_id=:id'),{'id':request_id}).mappings().one_or_none()
            return dict(row) if row else None
    finally:engine.dispose()


def select_detail(c,run_id):
    row=c.execute(text('SELECT r.*,a.result_id,a.status,a.execution_kind,a.analysis,a.failure_code,a.response_hash,'
        'a.receipt,a.created_at AS result_created_at FROM agent_analysis_runs r LEFT JOIN agent_analysis_results a USING(run_id) '
        'WHERE r.run_id=:id'),{'id':run_id}).mappings().one_or_none()
    return dict(row) if row else None


def context_has_reserved(c,context):
    events=list({s['event_id'] for s in context['sources']})
    versions=list({s['version_id'] for s in context['sources']})
    return bool(c.execute(text("SELECT EXISTS (SELECT 1 FROM annotation_samples WHERE split='blind' AND origin='source' "
        "AND (event_key=ANY(CAST(:events AS text[])) OR snapshot->>'event_version_id'=ANY(CAST(:versions AS text[]))))"),
        {'events':events,'versions':versions}).scalar_one())


def select_list(c,person_id=None,event_id=None,limit=20,cursor=None):
    return [dict(r) for r in c.execute(text('SELECT r.run_id,r.event_id,r.version_id,r.person_id,r.model_id,r.prompt_version,r.created_at,'
        "coalesce(a.status,'prepared_or_unfinished') AS status,a.execution_kind,a.analysis->>'summary_zh' AS summary_zh "
        'FROM agent_analysis_runs r LEFT JOIN agent_analysis_results a USING(run_id) '
        'WHERE (CAST(:person AS uuid) IS NULL OR r.person_id=:person) AND (CAST(:event AS uuid) IS NULL OR r.event_id=:event) '
        'AND (CAST(:cursor AS uuid) IS NULL OR (r.created_at,r.run_id)<(SELECT created_at,run_id FROM agent_analysis_runs WHERE run_id=:cursor)) '
        'ORDER BY r.created_at DESC,r.run_id DESC LIMIT :limit'),
        {'person':person_id,'event':event_id,'cursor':cursor,'limit':limit+1}).mappings()]


def read(settings,selector,*args):
    engine=create_read_engine(settings)
    try:
        with engine.connect() as c:return selector(c,*args)
    finally:engine.dispose()
