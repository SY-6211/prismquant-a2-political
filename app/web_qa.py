"""One explicit web request, durable receipt, no automatic generation retries."""
from datetime import datetime,timezone
from decimal import Decimal
import json,time
import httpx
from app.core.config import PROJECT_ROOT
from app.agent_qa import MODEL,PROMPT_VERSION,EXECUTABLE_PROMPTS,request_payload,validate_envelope,validate_answer,raw_answer,OutputRejected
from app.agent_analysis_live import atomic_receipt
from app.annotation_model import digest
from app.model_trial import money
from app.model_trial_live import private_key,bounded_json,units
from app.repositories import chat,qa,agent_analysis,web_qa as repo

RUNTIME=PROJECT_ROOT/'.runtime/web-qa'
def service(settings,fn,*args):
    engine=chat.engine_for(settings)
    try:
        with engine.begin() as c:return fn(c,*args)
    finally:engine.dispose()

def execute_worker(settings,token,conversation_id,run_id,*,client_factory=None,receipt_dir=None):
    try:row=service(settings,repo.start,token,conversation_id,run_id)
    except (chat.Unauthorized,chat.Missing):return service(settings,repo.cancel_queued,run_id)
    if row is None:return {'status':'not_started'}
    try:payload=request_payload(row['context'])
    except (ValueError,TypeError,KeyError):
        # A changed/missing frozen prompt must finish as not-sent instead of
        # stranding a running job or allowing an unrecorded provider request.
        result={'run_id':str(run_id),'request_hash':row['request_hash'],'status':'failed','answer':None,
          'response_hash':digest({'not_sent':row['request_hash']}),'failure_code':'not_sent_prompt_contract_unavailable',
          'receipt':{'model_requests':0,'model_id':row['model_id'],'reported_cost_usd':'0','billing_status':'settled',
                     'request_hash':row['request_hash'],'finished_at':datetime.now(timezone.utc).isoformat()}}
        atomic_receipt((receipt_dir or RUNTIME/'receipts')/(str(run_id)+'.json'),result)
        return service(settings,repo.finish,run_id,result)
    started=time.perf_counter();sent=False;key=None;cost='0';answer=None;status='failed';failure='preflight_failed'
    receipt={'model_requests':0,'model_id':MODEL,'provider':None,'generation_id':None,'request_hash':row['request_hash']}
    response_hash=digest({'not_sent':row['request_hash']})
    factory=client_factory or (lambda:httpx.Client(timeout=httpx.Timeout(60,connect=10),trust_env=False,follow_redirects=False))
    try:
        if row['model_id']!=MODEL or row['prompt_version'] not in EXECUTABLE_PROMPTS or row['context'].get('prompt_version')!=row['prompt_version'] or digest(row['context'])!=row['context_hash'] or digest(payload)!=row['request_hash']:raise ValueError('Frozen contract differs')
        if agent_analysis.read(settings,agent_analysis.context_has_reserved,row['context']):raise ValueError('Reserved evaluation source')
        key=private_key()
        with factory() as client:
            models=bounded_json(client,'GET','https://openrouter.ai/api/v1/models')['data'];model=next((m for m in models if m['id']==MODEL),None)
            if model is None or not {'structured_outputs','response_format'}.intersection(model.get('supported_parameters',[])):raise ValueError('Model unavailable')
            for item in ('prompt','completion'):money(model['pricing'][item])
            allowance=bounded_json(client,'GET','https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+key})['data'].get('limit_remaining')
            if allowance is not None and units(allowance)<row['reserved']:raise ValueError('Provider allowance insufficient')
            service(settings,repo.mark_sent,token,conversation_id,run_id)
            sent=True;cost=None;receipt['model_requests']=1
            envelope=bounded_json(client,'POST','https://openrouter.ai/api/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
            response_hash=digest(envelope);receipt.update(provider=envelope.get('provider'),generation_id=envelope.get('id'))
            if envelope.get('usage',{}).get('cost') is not None:cost=str(money(envelope['usage']['cost']))
            try:
                answer=validate_envelope(row['context'],envelope);status='completed' if answer['outcome']=='answered' else 'abstained';failure=None
            except (ValueError,KeyError,TypeError) as error:
                status='rejected';failure=error.code if isinstance(error,OutputRejected) else 'qa_contract_or_citation_rejected'
                # Small safe diagnostics: never persist exception text, source text, prompts or model output here.
                receipt['output_check']={'code':failure,'output_token_limit':payload['max_tokens']}
                if isinstance(error,OutputRejected) and error.details:receipt['output_check']['details']=error.details
                # Private diagnostic artifact for rejected replies only. Never included in API results or source releases.
                if len(json.dumps(envelope,ensure_ascii=False).encode())<=131072:
                    try:atomic_receipt((receipt_dir or RUNTIME/'receipts')/'rejected-responses'/(str(run_id)+'.json'),envelope)
                    except OSError:receipt['output_check']['private_diagnostic_saved']=False
                choices=envelope.get('choices')
                if isinstance(choices,list) and choices:
                    finish=choices[0].get('finish_reason')
                    receipt['output_check']['finish_reason']=finish if finish in ('stop','length','tool_calls','error','content_filter') else 'unknown'
    except Exception:
        status='unknown' if sent else 'failed';failure='response_or_cost_unknown' if sent else 'not_sent_preflight_failed';answer=None
    if sent and (cost is None or units(cost)>row['reserved']):status='unknown';answer=None;failure='response_or_cost_unknown'
    receipt.update(reported_cost_usd=cost,billing_status='unknown' if status=='unknown' else 'settled',latency_ms=round((time.perf_counter()-started)*1000,3),finished_at=datetime.now(timezone.utc).isoformat())
    result={'run_id':str(run_id),'request_hash':row['request_hash'],'status':status,'answer':answer,'response_hash':response_hash,'receipt':receipt,'failure_code':failure}
    # If saving fails, leave a recoverable file and a fenced execution; never re-send.
    atomic_receipt((receipt_dir or RUNTIME/'receipts')/(str(run_id)+'.json'),result)
    return service(settings,repo.finish,run_id,result)

def recover(settings,run_id):
    path=RUNTIME/'receipts'/(str(run_id)+'.json')
    if path.stat().st_size>131072:raise ValueError('Receipt too large')
    value=json.loads(path.read_text(encoding='utf8'))
    if value['run_id']!=str(run_id):raise ValueError('Receipt/run mismatch')
    def reconcile(c):
        from sqlalchemy import text
        row=c.execute(text('SELECT r.context,j.request_hash,j.reserved FROM agent_qa_runs r JOIN agent_web_qa_jobs j USING(run_id) WHERE r.run_id=:id'),{'id':run_id}).mappings().one()
        if value['request_hash']!=row['request_hash']:raise ValueError('Wrong receipt')
        if value['answer']:
            answer=value['answer'];raw=raw_answer(row['context'],answer)
            if digest(validate_answer(row['context'],raw))!=digest(answer):raise ValueError('Receipt evidence differs')
        cost=value['receipt']['reported_cost_usd']
        if value['status']!='unknown' and (cost is None or units(cost)>row['reserved']):raise ValueError('Cost outside reservation')
        return repo.finish(c,run_id,value)
    return service(settings,reconcile)
