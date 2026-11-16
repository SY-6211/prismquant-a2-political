"""Finite new-content analysis worker. Imports, API reads and migrations never spend."""
import argparse
from datetime import datetime,timezone
import json,time
from uuid import UUID
import httpx
from app.agent_analysis import MODEL,PROMPT_VERSION,Analysis,request_payload,prepare_context,validate_analysis,validate_envelope
from app.agent_tools import open_session
from app.agent_analysis_live import atomic_receipt
from app.annotation_model import digest
from app.core.config import PROJECT_ROOT,Settings
from app.model_trial import money
from app.model_trial_live import private_key,bounded_json,units
from app.repositories import agent_analysis as analyses,auto_analysis as repo

RUNTIME=PROJECT_ROOT/'.runtime/auto-analysis'

def service(settings,fn,*args):
    engine=analyses.write_engine(settings)
    try:
        with engine.begin() as c:return fn(c,*args)
    finally:engine.dispose()

def prepare_one(settings,approval):
    row=service(settings,repo.claim_prepare,approval)
    if row is None or row.get('skipped') or row.get('cached'):return row
    try:
        with open_session(settings,[row['person_id']]) as session:context=prepare_context(session,row['event_id'],row['version_id'])
        service(settings,repo.prepared,row['job_id'],context)
        return {'job_id':str(row['job_id']),'status':'prepared'}
    except Exception:
        service(settings,repo.skip,row['job_id'],'input_unavailable_or_reserved')
        return {'job_id':str(row['job_id']),'status':'skipped'}

def execute_one(settings,approval,*,client_factory=None,receipt_dir=None):
    row=service(settings,repo.start,approval)
    if row is None:return {'status':'idle','model_requests':0}
    payload=request_payload(row['context']);sent=False;cost='0';analysis=None;state='failed';failure='not_sent_preflight_failed';started=time.perf_counter()
    receipt={'model_requests':0,'model_id':MODEL,'provider':None,'generation_id':None,'request_hash':row['request_hash'],'mode':'finite_automatic_analysis'}
    response_hash=digest({'not_sent':row['request_hash']})
    factory=client_factory or (lambda:httpx.Client(timeout=httpx.Timeout(60,connect=10),trust_env=False,follow_redirects=False))
    try:
        key=private_key()
        with factory() as client:
            models=bounded_json(client,'GET','https://openrouter.ai/api/v1/models')['data'];model=next((m for m in models if m['id']==MODEL),None)
            if model is None or not {'structured_outputs','response_format'}.intersection(model.get('supported_parameters',[])):raise ValueError('Model unavailable')
            for name in ('prompt','completion'):money(model['pricing'][name])
            remaining=bounded_json(client,'GET','https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+key})['data'].get('limit_remaining')
            if remaining is not None and units(remaining)<row['reserved']:raise ValueError('Provider allowance insufficient')
            service(settings,repo.mark_sent,row['job_id']);sent=True;cost=None;receipt['model_requests']=1
            value=bounded_json(client,'POST','https://openrouter.ai/api/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
            response_hash=digest(value);receipt.update(provider=value.get('provider'),generation_id=value.get('id'))
            if value.get('usage',{}).get('cost') is not None:cost=str(money(value['usage']['cost']))
            try:analysis=validate_envelope(row['context'],value);state='completed';failure=None
            except (ValueError,TypeError,KeyError):state='rejected';failure='analysis_contract_or_evidence_rejected'
    except Exception:
        state='unknown' if sent else 'failed';analysis=None;failure='response_or_cost_unknown' if sent else 'not_sent_preflight_failed'
    if sent and (cost is None or units(cost)>row['reserved']):state='unknown';analysis=None;failure='response_or_cost_unknown'
    receipt.update(reported_cost_usd=cost,billing_status='unknown' if state=='unknown' else 'settled',latency_ms=round((time.perf_counter()-started)*1000,3),finished_at=datetime.now(timezone.utc).isoformat())
    result={'run_id':str(row['run_id']),'request_hash':row['request_hash'],'status':state,'analysis':analysis,'response_hash':response_hash,'receipt':receipt,'failure_code':failure}
    atomic_receipt((receipt_dir or RUNTIME/'receipts')/(str(row['job_id'])+'.json'),result)
    return service(settings,repo.finish,row['job_id'],result)

def recover(settings,job_id):
    file=RUNTIME/'receipts'/(str(job_id)+'.json')
    if file.stat().st_size>131072:raise ValueError('Receipt too large')
    value=json.loads(file.read_text(encoding='utf8'))
    def apply(c):
        from sqlalchemy import text
        job=c.execute(text('SELECT * FROM agent_auto_jobs WHERE job_id=:id'),{'id':job_id}).mappings().one()
        if str(job['run_id'])!=value['run_id'] or job['request_hash']!=value['request_hash']:raise ValueError('Receipt mismatch')
        run=analyses.select_detail(c,job['run_id'])
        if value['analysis'] is not None:
            answer=value['analysis'];raw={name:answer[name] for name in Analysis.model_fields}
            raw=json.loads(json.dumps(raw))
            for point in raw['facts']+raw['hypotheses']:
                point['citations']=[{'source_ref':q['source_ref'],'text':q['text']} for q in point['citations']]
            raw['comparison_citations']=[{'source_ref':q['source_ref'],'text':q['text']} for q in raw['comparison_citations']]
            if digest(validate_analysis(run['context'],raw))!=digest(answer):raise ValueError('Evidence differs')
        cost=value['receipt']['reported_cost_usd']
        if value['status']!='unknown' and (cost is None or units(cost)>job['reserved']):raise ValueError('Cost differs')
        return repo.finish(c,job_id,value)
    return service(settings,apply)

def tick(settings,approval,*,execute=False):
    queued=service(settings,repo.sync,approval)
    prepared=prepare_one(settings,approval)
    result=execute_one(settings,approval) if execute else {'status':'execution_not_authorized','model_requests':0}
    return {'queue':queued,'preparation':prepared,'execution':result}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('status')
    enable=sub.add_parser('enable');enable.add_argument('--person-id',type=UUID,required=True);enable.add_argument('--approval',required=True);enable.add_argument('--cap-usd',required=True);enable.add_argument('--max-requests',type=int,required=True);enable.add_argument('--hours',type=int,required=True);enable.add_argument('--approved',action='store_true',required=True)
    disable=sub.add_parser('disable');disable.add_argument('--grant-id',type=UUID,required=True)
    repair=sub.add_parser('recover');repair.add_argument('--job-id',type=UUID,required=True)
    run=sub.add_parser('run');run.add_argument('--approval',required=True);run.add_argument('--execute',action='store_true');run.add_argument('--loop',action='store_true');run.add_argument('--tick-seconds',type=int,default=10)
    args=parser.parse_args(argv)
    if args.action=='run' and (not 5<=args.tick_seconds<=60 or args.loop and not args.execute):parser.error('Loop requires explicit execution; tick must be 5-60 seconds')
    try:
        settings=Settings()
        if args.action=='enable':result=service(settings,repo.grant,args.person_id,args.approval,units(args.cap_usd),args.max_requests,args.hours)
        elif args.action=='disable':result=service(settings,repo.close,args.grant_id)
        elif args.action=='status':result=analyses.read(settings,repo.status)
        elif args.action=='recover':result=recover(settings,args.job_id)
        else:
            while True:
                result=tick(settings,args.approval,execute=args.execute)
                print(json.dumps(result,ensure_ascii=False,default=str),flush=True)
                if not args.loop:return 0
                def decision(c):
                    from sqlalchemy import text
                    grant_id=c.execute(text('SELECT grant_id FROM agent_auto_grants WHERE approval=:approval'),{'approval':args.approval}).scalar_one_or_none()
                    return repo.budget(c,grant_id)['status'] if grant_id else 'disabled'
                state=service(settings,decision)
                if state!='ready':print(json.dumps({'stopped':state}));return 0
                time.sleep(args.tick_seconds)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0
    except KeyboardInterrupt:print('已停止等待。已发送结果请按原任务回执核对，不会自动重发。');return 0
    except Exception:print('自动分析操作未完成；请核对迁移、有限授权及原任务回执。未显示密钥，不会自动重试。');return 1

if __name__=='__main__':raise SystemExit(main())
