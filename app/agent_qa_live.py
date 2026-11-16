"""Explicit finite QA execution; no HTTP generation endpoint or background spending."""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
from uuid import UUID
import httpx
from sqlalchemy.exc import SQLAlchemyError
from app.agent_qa import MODEL,PROMPT_VERSION,EXECUTABLE_PROMPTS,request_payload,validate_envelope
from app.agent_tools import encoded
from app.annotation_model import digest
from app.agent_analysis_live import atomic_receipt
from app.core.config import Settings,PROJECT_ROOT
from app.model_trial import money
from app.model_trial_live import bounded_json,private_key,units,redact
from app.repositories import chat,qa,agent_analysis

APPROVAL='user-2026-11-12-qa-usd005'
CAP=units('.05')
RUNTIME=PROJECT_ROOT/'.runtime/agent-qa'


class Budget:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=5,isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS qa_calls (approval TEXT,run_id TEXT,request_hash TEXT,reserved INTEGER,actual INTEGER,status TEXT,PRIMARY KEY(approval,run_id))')

    def reserve(self,run_id,request_hash,amount):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows=self.db.execute('SELECT reserved,actual,status FROM qa_calls WHERE approval=?',(APPROVAL,)).fetchall()
            if any(r[2]!='settled' for r in rows) or len(rows)>=2 or not 0<amount<=CAP:raise ValueError('Finite QA approval is fenced')
            if sum(r[1] for r in rows)+amount>CAP:raise ValueError('Finite QA cap exceeded')
            self.db.execute('INSERT INTO qa_calls VALUES (?,?,?,?,NULL,?)',(APPROVAL,str(run_id),request_hash,amount,'reserved'))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise

    def settle(self,run_id,cost):
        actual=units(cost) if cost is not None else None
        reserved=self.db.execute('SELECT reserved FROM qa_calls WHERE approval=? AND run_id=?',(APPROVAL,str(run_id))).fetchone()[0]
        status='settled' if actual is not None and actual<=reserved else 'unknown'
        self.db.execute('UPDATE qa_calls SET actual=?,status=? WHERE approval=? AND run_id=?',(actual,status,APPROVAL,str(run_id)))
        return status

    def totals(self):
        rows=self.db.execute('SELECT reserved,actual,status FROM qa_calls WHERE approval=?',(APPROVAL,)).fetchall()
        return {'approval':APPROVAL,'cap_usd':'0.05','calls':len(rows),'unsettled':sum(r[2]!='settled' for r in rows),
            'accounted_or_reserved_usd':str(Decimal(sum(r[1] if r[2]=='settled' else max(r[0],r[1] or 0) for r in rows))/Decimal(10**9))}

    def close(self):self.db.close()


def read_credential(path):
    root=(PROJECT_ROOT/'.runtime/chat-credentials').resolve();target=Path(path).resolve()
    if not target.is_relative_to(root):raise ValueError('Use a private local credential file')
    value=json.loads(target.read_text(encoding='utf-8'));chat.token_digest(value['access_token'])
    return value['access_token']


def execute(settings,token,conversation_id,run_id,approval,*,ledger_path=None,receipt_dir=None,client_factory=None):
    if approval!=APPROVAL:raise ValueError('A separate finite QA approval is required')
    row=chat.execute(settings,token,qa.detail,conversation_id,run_id)
    if row['status']!='prepared':raise chat.Conflict('问答已结束或执行结果待核对，不能重复请求模型。')
    if row['model_id']!=MODEL or row['prompt_version'] not in EXECUTABLE_PROMPTS or row['context'].get('prompt_version')!=row['prompt_version'] or digest(row['context'])!=row['context_hash']:raise ValueError('Frozen QA contract differs')
    if agent_analysis.read(settings,agent_analysis.context_has_reserved,row['context']):raise ValueError('Reserved evaluation source')
    payload=request_payload(row['context']);request_hash=digest(payload)
    reserve=units(Decimal(len(encoded(payload))+2048)*Decimal('.000001')+Decimal(payload['max_tokens'])*Decimal('.000002')+Decimal('.0005'))
    if reserve>CAP:raise ValueError('QA context exceeds finite cap')
    key=private_key();ledger=Budget(ledger_path or RUNTIME/'budget-usd005.sqlite3')
    factory=client_factory or (lambda:httpx.Client(timeout=httpx.Timeout(60,connect=10),follow_redirects=False,trust_env=False))
    try:
        with factory() as client:
            models=bounded_json(client,'GET','https://openrouter.ai/api/v1/models')['data']
            model=next((m for m in models if m['id']==MODEL),None)
            if model is None or not {'structured_outputs','response_format'}.intersection(model.get('supported_parameters',[])):raise ValueError('Model capability unavailable')
            for item in ('prompt','completion'):money(model['pricing'][item])
            remaining=bounded_json(client,'GET','https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+key})['data'].get('limit_remaining')
            if remaining is not None and units(remaining)<reserve:raise ValueError('Insufficient allowance')
            chat.execute(settings,token,qa.claim,conversation_id,run_id,request_hash)
            try:ledger.reserve(run_id,request_hash,reserve)
            except Exception:
                qa.write_result(settings,run_id,'failed',None,digest({'not_sent':request_hash}),
                    {'model_requests':0,'billing_status':'not_started','reported_cost_usd':'0'},failure='local_budget_fenced')
                raise
            started=time.perf_counter();cost=None;answer=None;status='failed';failure=None;diagnostic={}
            receipt={'approval':APPROVAL,'model_id':MODEL,'request_hash':request_hash,'provider':None,'generation_id':None}
            response_hash=digest({'unconfirmed':request_hash})
            try:
                envelope=bounded_json(client,'POST','https://openrouter.ai/api/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
                response_hash=digest(envelope);receipt.update(provider=envelope.get('provider'),generation_id=envelope.get('id'))
                if envelope.get('usage',{}).get('cost') is not None:cost=str(money(envelope['usage']['cost']))
                try:
                    answer=validate_envelope(row['context'],envelope)
                    status='completed' if answer['outcome']=='answered' else 'abstained'
                except (ValueError,KeyError,TypeError) as error:
                    status='rejected';failure='qa_contract_or_citation_rejected';diagnostic={'validation':redact(error,key)}
            except httpx.HTTPStatusError as error:failure='http_'+str(error.response.status_code)
            except (httpx.HTTPError,ValueError,KeyError,TypeError):failure='transport_or_response_unknown'
            receipt.update(reported_cost_usd=cost,billing_status=ledger.settle(run_id,cost),
                finished_at=datetime.now(timezone.utc).isoformat(),latency_ms=round((time.perf_counter()-started)*1000,3))
            if receipt['billing_status']!='settled':status='unknown';answer=None;failure='billing_or_response_unknown'
            durable={'run_id':str(run_id),'status':status,'answer':answer,'response_hash':response_hash,
                'receipt':receipt,'failure_code':failure,'private_diagnostic':diagnostic}
            atomic_receipt((receipt_dir or RUNTIME/'receipts')/(str(run_id)+'.json'),durable)
            saved=qa.write_result(settings,run_id,status,answer,response_hash,receipt,failure=failure)
            return {'run_id':str(run_id),'status':status,'result_id':str(saved['result_id']),'receipt':receipt,'budget':ledger.totals()}
    finally:ledger.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--conversation-id',required=True,type=UUID);p.add_argument('--run-id',required=True,type=UUID)
    p.add_argument('--credential-file',type=Path,default=PROJECT_ROOT/'.runtime/chat-credentials/local-user.json')
    p.add_argument('--approval',required=True);p.add_argument('--execute',action='store_true')
    args=p.parse_args(argv)
    if not args.execute:p.error('Explicit execution and this round approval required')
    try:
        result=execute(Settings(),read_credential(args.credential_file),args.conversation_id,args.run_id,args.approval)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['status'] in ('completed','abstained') else 1
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error,SQLAlchemyError,httpx.HTTPError,chat.Missing,chat.Conflict,chat.Unauthorized):
        print(json.dumps({'status':'not_started_or_needs_review','message':'检查私有身份、问答范围、审批及费用回执；不要重试结果未知的模型调用。'},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
