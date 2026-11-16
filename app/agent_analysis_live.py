"""Explicit one-shot analysis; separate finite approval, no automatic retry/loop."""
import argparse
from datetime import datetime,timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID

import httpx
from sqlalchemy.exc import SQLAlchemyError
from app.agent_analysis import MODEL,PROMPT_VERSION,request_payload,validate_envelope
from app.agent_tools import encoded
from app.annotation_model import digest
from app.core.config import PROJECT_ROOT,Settings
from app.model_trial import money
from app.model_trial_live import bounded_json,private_key,units,redact
from app.repositories import agent_analysis as repo

APPROVAL='user-2026-11-09-agent-usd005'
CAP=units('.05')
RUNTIME=PROJECT_ROOT/'.runtime/agent-analysis'


class Budget:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=5,isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS analysis_calls (approval TEXT,run_id TEXT,request_hash TEXT,reserved INTEGER,actual INTEGER,status TEXT,PRIMARY KEY(approval,run_id))')

    def reserve(self,run_id,request_hash,amount):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows=self.db.execute('SELECT reserved,actual,status FROM analysis_calls WHERE approval=?',(APPROVAL,)).fetchall()
            if any(r[2]!='settled' for r in rows) or len(rows)>=2:raise ValueError('Approval stopped or call cap reached')
            if sum(r[1] if r[1] is not None else r[0] for r in rows)+amount>CAP:raise ValueError('Approval cap exceeded')
            self.db.execute('INSERT INTO analysis_calls VALUES (?,?,?,?,NULL,?)',(APPROVAL,str(run_id),request_hash,amount,'reserved'))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise

    def settle(self,run_id,cost):
        actual=units(cost) if cost is not None else None
        reserved=self.db.execute('SELECT reserved FROM analysis_calls WHERE approval=? AND run_id=?',(APPROVAL,str(run_id))).fetchone()[0]
        status='settled' if actual is not None and actual<=reserved else 'unknown'
        self.db.execute('UPDATE analysis_calls SET actual=?,status=? WHERE approval=? AND run_id=?',(actual,status,APPROVAL,str(run_id)))
        return status

    def totals(self):
        rows=self.db.execute('SELECT reserved,actual,status FROM analysis_calls WHERE approval=?',(APPROVAL,)).fetchall()
        return {'approval':APPROVAL,'cap_usd':'0.05','calls':len(rows),
                'accounted_or_reserved_usd':str(Decimal(sum(max(r[0],r[1] or 0) if r[2]!='settled' else r[1] for r in rows))/Decimal(10**9)),
                'unsettled':sum(r[2]!='settled' for r in rows)}

    def close(self):self.db.close()


def atomic_receipt(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    with temporary.open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2,default=str);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)


def execute(settings,run_id,approval,*,ledger_path=None,receipt_dir=None,client_factory=None):
    if approval!=APPROVAL:raise ValueError('This finite execution needs its explicit approval')
    row=repo.read(settings,repo.select_detail,run_id)
    if row is None or row['result_id'] is not None:raise ValueError('Run missing or already finalized')
    if row['model_id']!=MODEL or row['prompt_version']!=PROMPT_VERSION or digest(row['context'])!=row['context_hash']:raise ValueError('Prepared context differs')
    if repo.read(settings,repo.context_has_reserved,row['context']):raise ValueError('Context now includes reserved evaluation content')
    payload=request_payload(row['context'])
    reserve=units(Decimal(len(encoded(payload))+2048)*Decimal('.000001')+Decimal(1600)*Decimal('.000002')+Decimal('.0005'))
    if reserve>CAP:raise ValueError('Prepared context exceeds finite budget')
    key=private_key();ledger=Budget(ledger_path or RUNTIME/'budget-usd005.sqlite3')
    factory=client_factory or (lambda:httpx.Client(timeout=httpx.Timeout(60,connect=10),follow_redirects=False,trust_env=False))
    receipt=None
    try:
        with factory() as client:
            models=bounded_json(client,'GET','https://openrouter.ai/api/v1/models')['data']
            selected=next((m for m in models if m['id']==MODEL),None)
            if selected is None or not {'structured_outputs','response_format'}.intersection(selected.get('supported_parameters',[])):raise ValueError('Model capability unavailable')
            for item in ('prompt','completion'):money(selected['pricing'][item])
            remaining=bounded_json(client,'GET','https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+key})['data'].get('limit_remaining')
            if remaining is not None and units(remaining)<reserve:raise ValueError('Insufficient key allowance')
            request_hash=digest(payload);ledger.reserve(run_id,request_hash,reserve)
            started=time.perf_counter();cost=None;analysis=None;status='failed';failure=None;diagnostic={}
            receipt={'approval':APPROVAL,'model_id':MODEL,'request_hash':request_hash,'provider':None,'generation_id':None}
            response_hash=digest({'no_confirmed_response':True,'request_hash':request_hash})
            try:
                envelope=bounded_json(client,'POST','https://openrouter.ai/api/v1/chat/completions',headers={'Authorization':'Bearer '+key},json=payload)
                response_hash=digest(envelope)
                receipt.update(provider=envelope.get('provider'),generation_id=envelope.get('id'))
                if envelope.get('usage',{}).get('cost') is not None:cost=str(money(envelope['usage']['cost']))
                try:analysis=validate_envelope(row['context'],envelope);status='completed'
                except (ValueError,KeyError,TypeError) as error:
                    status='rejected';failure='analysis_contract_or_evidence_rejected'
                    diagnostic={'validation':redact(error,key),'output_preview':redact(envelope.get('choices',[{}])[0].get('message',{}).get('content',''),key)}
            except httpx.HTTPStatusError as error:
                failure='http_'+str(error.response.status_code)
                diagnostic={'provider_error':redact(error.response.text,key)}
            except (httpx.HTTPError,ValueError,KeyError,TypeError):failure='transport_or_response_unknown'
            receipt.update(reported_cost_usd=cost,latency_ms=round((time.perf_counter()-started)*1000,3),
                billing_status=ledger.settle(run_id,cost),finished_at=datetime.now(timezone.utc).isoformat())
            if receipt['billing_status']!='settled':status='unknown';analysis=None;failure='billing_or_response_unknown'
            durable={'run_id':str(run_id),'status':status,'analysis':analysis,'response_hash':response_hash,'receipt':receipt,'failure_code':failure,'private_diagnostic':diagnostic}
            atomic_receipt((receipt_dir or RUNTIME/'receipts')/(str(run_id)+'.json'),durable)
            saved=repo.write_result(settings,run_id,status,analysis,response_hash,receipt,failure_code=failure)
            return {'run_id':str(run_id),'status':status,'result_id':str(saved['result_id']),'receipt':receipt,'budget':ledger.totals()}
    finally:ledger.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-id',required=True,type=UUID)
    p.add_argument('--execute',action='store_true');p.add_argument('--approval',required=True)
    a=p.parse_args(argv)
    if not a.execute:p.error('Explicit --execute and matching user approval required')
    try:
        result=execute(Settings(),a.run_id,a.approval)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0 if result['status']=='completed' else 1
    except (ValueError,TypeError,OSError,sqlite3.Error,SQLAlchemyError,httpx.HTTPError):
        print(json.dumps({'status':'not_started_or_needs_review','message':'检查输入、迁移、审批和本地费用/结果回执；不要重复请求未知结果。'},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
