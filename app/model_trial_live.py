"""Explicit finite OpenRouter trial. US$0.20 shared durable local approval cap."""
import argparse
import json
import os
import sqlite3
import time
from datetime import datetime,timezone
from decimal import Decimal,ROUND_CEILING
from pathlib import Path
import httpx
from dotenv import dotenv_values
from app.core.config import PROJECT_ROOT
from app.model_trial import Trial,cases_from,read_file,build_plan,request_payload,validate_response,money
from app.model_trial import quote_request_payload,validate_quote_response,QUOTE_PROMPT_VERSION
from app.annotation_model import digest,confusion

MODELS=['deepseek/deepseek-v3.2','openai/gpt-oss-120b']
APPROVAL='user-2026-11-08-usd020'
CAP=200000000 # integer nanodollars, $0.20 across restarts and all attempts
UNIT=Decimal(1000000000)


def redact(value,key):
    import re
    return re.sub(r'sk-or-[A-Za-z0-9_-]+','[redacted]',str(value).replace(key,'[redacted]'))[:4096]


def units(value):return int((money(value)*UNIT).to_integral_value(rounding=ROUND_CEILING))


class Ledger:
    def __init__(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=5,isolation_level=None)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS calls (approval TEXT NOT NULL,request TEXT NOT NULL, '
            'reserved INTEGER NOT NULL,actual INTEGER,status TEXT NOT NULL,PRIMARY KEY(approval,request))')
        self.db.execute('CREATE TABLE IF NOT EXISTS reconciliations (approval TEXT NOT NULL,request TEXT NOT NULL, '
            'evidence_sha256 TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL)')

    def cover_terminal_rejection(self,call,evidence):
        """Operator-directed diagnostic reconciliation; never auto-refund a failed call.

        A completed 4xx response without a generation remains conservatively charged
        to this experiment at >=$0.01. Timeouts/200 missing cost remain fenced.
        This does NOT assert a zero provider bill.
        """
        if call.get('failure') not in ('http_400','http_401','http_402','http_403','http_404','http_429') or call.get('generation_id'):
            raise ValueError('Only terminal rejected HTTP responses may be covered')
        if not evidence.get('generation_bills_checked') or not evidence.get('key_usage_checked'):
            raise ValueError('Explicit read-only billing evidence required')
        key=call['request_sha256'];self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT reserved,actual,status FROM calls WHERE approval=? AND request=?',(APPROVAL,key)).fetchone()
            if not row or row[1] is not None or row[2]!='unknown':raise ValueError('No unsettled rejection to cover')
            reserve=max(row[0],units('.01'))
            used=self.db.execute('SELECT coalesce(sum(coalesce(actual,reserved)),0) FROM calls WHERE approval=?',(APPROVAL,)).fetchone()[0]
            if used-row[0]+reserve>CAP:raise ValueError('No budget for conservative coverage')
            self.db.execute("UPDATE calls SET reserved=?,status='held_rejection' WHERE approval=? AND request=?",(reserve,APPROVAL,key))
            self.db.execute('INSERT INTO reconciliations VALUES (?,?,?,?,?)',(APPROVAL,key,digest(evidence),
                'Terminal rejection; full conservative reserve retained, actual cost unconfirmed; operator-directed diagnostic continuation',datetime.now(timezone.utc).isoformat()))
            self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise

    def reserve(self,key,amount):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.db.execute("SELECT 1 FROM calls WHERE approval=? AND status IN ('reserved','unknown')",(APPROVAL,)).fetchone():
                raise ValueError('Previous call unresolved; reconcile before any further spend')
            if self.db.execute('SELECT 1 FROM calls WHERE approval=? AND request=?',(APPROVAL,key)).fetchone():raise ValueError('Request already attempted')
            if self.db.execute('SELECT count(*) FROM calls WHERE approval=?',(APPROVAL,)).fetchone()[0]>=12:raise ValueError('Small trial call limit exhausted')
            used=self.db.execute('SELECT coalesce(sum(coalesce(actual,reserved)),0) FROM calls WHERE approval=?',(APPROVAL,)).fetchone()[0]
            if amount<=0 or used+amount>CAP:raise ValueError('Approval budget exhausted')
            self.db.execute("INSERT INTO calls VALUES (?,?,?,NULL,'reserved')",(APPROVAL,key,amount));self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise

    def finish(self,key,cost):
        status='unknown' if cost is None else 'settled'
        actual=units(cost) if cost is not None else None
        row=self.db.execute('SELECT reserved FROM calls WHERE approval=? AND request=?',(APPROVAL,key)).fetchone()
        if row is None:raise ValueError('Missing reservation')
        if actual is not None and actual>row[0]:status='unknown' # unexpected provider billing: stop and reconcile
        self.db.execute('UPDATE calls SET actual=?,status=? WHERE approval=? AND request=?',(actual,status,APPROVAL,key))
        return status

    def totals(self):
        total,unknown,held=self.db.execute("SELECT coalesce(sum(coalesce(actual,reserved)),0),coalesce(sum(status IN ('reserved','unknown')),0),coalesce(sum(status='held_rejection'),0) FROM calls WHERE approval=?",(APPROVAL,)).fetchone()
        return {'approval_id':APPROVAL,'cap_usd':'0.20','accounted_or_reserved_usd':str(Decimal(total)/UNIT),
            'unresolved_calls':unknown,'conservatively_held_rejections':held}

    def close(self):self.db.close()


def bounded_json(client,method,url,**kwargs):
    with client.stream(method,url,**kwargs) as response:
        body=bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body)>4194304:raise ValueError('Response exceeds 4 MiB')
        # Keep bounded error content readable after the streaming context closes.
        buffered=httpx.Response(response.status_code,content=bytes(body),request=response.request)
        buffered.raise_for_status()
        from app.model_trial import load_json
        return load_json(body.decode('utf-8'))


def private_key():
    values=dotenv_values(PROJECT_ROOT/'.env')
    key=os.environ.get('PQ_OPENROUTER_API_KEY') or values.get('PQ_OPENROUTER_API_KEY')
    if not key or not key.startswith('sk-or-') or any(c.isspace() for c in key):raise ValueError('Configure PQ_OPENROUTER_API_KEY privately in .env')
    return key


def run(cases_path,out,ledger_path,*,only_model=None):
    key=private_key();cases=cases_from(read_file(cases_path))
    if only_model is not None and (only_model not in MODELS or len(cases)!=1):raise ValueError('Manual follow-up requires one case and an existing candidate')
    if out.exists():raise ValueError('Output directory exists; do not repeat an existing experiment')
    out.mkdir(parents=True);ledger=Ledger(ledger_path);rows=[];stop=None
    report={'date':'2026-11-08','mode':'real_api_synthetic_cases','models':MODELS,'approved_budget_usd':'0.20',
        'primary_model':None,'backup_model':None,'human_accuracy_verified':False,'cases_origin':'synthetic',
        'prompt_version':QUOTE_PROMPT_VERSION,'openai_candidate_kind':'open_weight_third_party_hosted',
        'price_ceiling_per_million_usd':{'prompt':'1','completion':'2'}}
    if only_model:report.update(mode='single_case_development_followup',only_model=only_model)
    try:
        with httpx.Client(timeout=httpx.Timeout(45,connect=10),follow_redirects=False,trust_env=False) as client:
            catalog=bounded_json(client,'GET','https://openrouter.ai/api/v1/models')
            selected={'data':[v for v in catalog['data'] if v['id'] in MODELS]}
            # Budget at the enforced provider price ceiling, not the catalog floor.
            ceiling_catalog={'data':[{**v,'pricing':{**v['pricing'],'prompt':'0.000001','completion':'0.000002'}} for v in selected['data']]}
            plan=build_plan({'model_ids':MODELS,'budget_usd':'.20','max_output_tokens':1024},[c.model_dump() for c in cases],ceiling_catalog,
                payload_factory=quote_request_payload,prompt_version=QUOTE_PROMPT_VERSION)
            (out/'catalog.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2),encoding='utf-8')
            (out/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
            headers={'Authorization':'Bearer '+key}
            key_info=bounded_json(client,'GET','https://openrouter.ai/api/v1/key',headers=headers)['data']
            report['key_preflight']={k:key_info.get(k) for k in ('limit_remaining','usage')}
            prices={v['id']:v['pricing'] for v in selected['data']}
            estimates={(v['model_id'],v['case_id']):money(v['estimated_usd']) for v in plan['requests']}
            required=sum(estimates.values())+Decimal('.0005')*len(estimates)
            if required>Decimal('.20'):raise ValueError('Reservation estimate exceeds .20')
            if key_info.get('limit_remaining') is not None and money(key_info['limit_remaining'])<required:raise ValueError('Key allowance insufficient')
            for case in cases: # alternate models for less systematic time-of-day bias
                for model in MODELS:
                    if only_model and model!=only_model:continue
                    payload=quote_request_payload(model,case,1024)
                    price=prices[model]
                    payload['provider']['max_price']={'prompt':1,'completion':2}
                    if model.startswith('deepseek/'):
                        payload['reasoning']={'enabled':False};payload['include_reasoning']=False
                    elif model.startswith('openai/gpt-oss-'):
                        payload['reasoning']={'effort':'low','exclude':True};payload['include_reasoning']=False
                    identity=digest({'payload':payload,'approval':APPROVAL})
                    reserve=units(estimates[(model,case.case_id)]+Decimal('.0005'))
                    ledger.reserve(identity,reserve)
                    start=time.perf_counter();cost=None
                    row={'model_id':model,'case_id':case.case_id,'request_sha256':identity,'valid':False}
                    try:
                        response=bounded_json(client,'POST','https://openrouter.ai/api/v1/chat/completions',headers=headers,json=payload)
                        raw_cost=response.get('usage',{}).get('cost')
                        cost=str(money(raw_cost)) if raw_cost is not None else None
                        row.update({'generation_id':response.get('id'),'returned_model':response.get('model'),
                            'provider':response.get('provider'),'reported_cost_usd':cost,
                            'usage':{k:response.get('usage',{}).get(k) for k in ('prompt_tokens','completion_tokens','total_tokens')}})
                        try:row.update(validate_quote_response(response,case,model));row['valid']=True
                        except (ValueError,KeyError,TypeError) as error:
                            row['failure']='response_contract_rejected'
                            row['validation_diagnostic']=redact(error,key)
                            choices=response.get('choices') or []
                            if choices and isinstance(choices[0].get('message',{}).get('content'),str):
                                row['output_preview']=redact(choices[0]['message']['content'],key)
                            row['finish_reason']=response.get('choices',[{}])[0].get('finish_reason') if response.get('choices') else None
                    except httpx.HTTPStatusError as error:
                        row['failure']='http_'+str(error.response.status_code)
                        try:
                            detail=error.response.read().decode('utf-8')[:4096]
                            row['http_diagnostic']=redact(detail,key)
                        except (httpx.HTTPError,UnicodeError):row['http_diagnostic']='Error detail unavailable'
                    except (httpx.HTTPError,ValueError,KeyError,TypeError):row['failure']='transport_or_response_unknown'
                    finally:
                        row['latency_ms']=round((time.perf_counter()-start)*1000,1)
                        state=ledger.finish(identity,cost);row['billing_status']=state
                        rows.append(row)
                        with (out/'calls.jsonl').open('a',encoding='utf-8') as file:
                            file.write(json.dumps(row,ensure_ascii=False)+'\n');file.flush();os.fsync(file.fileno())
                    if state=='unknown':stop='Unknown or unexpected billing; stop without retry and reconcile';break
                if stop:break
    except (ValueError,KeyError,TypeError,httpx.HTTPError):stop='Preflight/budget/network check stopped; no automatic retry'
    finally:
        report.update({'calls':rows,'stop_reason':stop,'ledger':ledger.totals(),'completed':len(rows)==len(cases)*(1 if only_model else 2) and stop is None})
        report['candidate_results']={}
        for model in MODELS:
            entries=[r for r in rows if r['model_id']==model];lookup={r['case_id']:r for r in entries};pairs=[]
            for case in cases:
                if case.expected_relevant is not None:
                    value=lookup.get(case.case_id);pairs.append((case.expected_relevant,value['answer']['relevant'] if value and value['valid'] else None))
            report['candidate_results'][model]={'attempted':len(entries),'valid':sum(r['valid'] for r in entries),
                'reported_cost_usd':str(sum(money(r['reported_cost_usd']) for r in entries if r.get('reported_cost_usd') is not None)),
                'latencies_ms':[r['latency_ms'] for r in entries],'synthetic_case_relevance':confusion(pairs)}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');ledger.close()
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute',action='store_true');p.add_argument('--cases',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    if not args.execute:p.error('Explicit --execute required; user approval applies only to the .20 trial')
    try:
        result=run(args.cases,args.out,PROJECT_ROOT/'.runtime/model-trial-usd020.sqlite3')
        print(json.dumps({'completed':result['completed'],'calls':len(result['calls']),'ledger':result['ledger'],'report':str(args.out/'report.json')},ensure_ascii=False))
        return 0 if result['completed'] else 1
    except (ValueError,OSError,sqlite3.Error):
        print(json.dumps({'status':'not_started_or_stopped','message':'检查本机私有凭证、输出目录和持久费用台账；不要把 Key 发到聊天。'},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
