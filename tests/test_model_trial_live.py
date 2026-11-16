import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from app.model_trial_live import Ledger,units,run,MODELS,redact
from test_model_trial import catalog,cases,envelope


class LiveTrialTests(unittest.TestCase):
    def test_manual_rejection_coverage_keeps_budget_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'ledger.db');ledger.reserve('failed',units('.003'));ledger.finish('failed',None)
            with self.assertRaises(ValueError):ledger.cover_terminal_rejection({'request_sha256':'failed','failure':'transport_or_response_unknown'},{})
            with self.assertRaises(ValueError):ledger.cover_terminal_rejection({'request_sha256':'failed','failure':'http_403'},{})
            ledger.cover_terminal_rejection({'request_sha256':'failed','failure':'http_403'},{'generation_bills_checked':True,'key_usage_checked':True})
            self.assertEqual(ledger.totals()['accounted_or_reserved_usd'],'0.01')
            self.assertEqual(ledger.totals()['conservatively_held_rejections'],1)
            ledger.reserve('new-probe',units('.001'));ledger.finish('new-probe','.0001')
            self.assertEqual(ledger.db.execute('SELECT count(*) FROM reconciliations').fetchone()[0],1);ledger.close()
    def test_diagnostics_redact_credentials_and_are_bounded(self):
        self.assertNotIn('sk-or-test-secret',redact('Error sk-or-test-secret','sk-or-test-secret'))
        self.assertNotIn('private-value',redact('private-value','private-value'))
        self.assertLessEqual(len(redact('x'*10000,'secret')),4096)
    def test_durable_cap_duplicate_and_unknown_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.db';ledger=Ledger(path)
            ledger.reserve('one',units('.15'));ledger.finish('one','.15');ledger.close()
            ledger=Ledger(path)
            with self.assertRaises(ValueError):ledger.reserve('two',units('.06'))
            with self.assertRaises(ValueError):ledger.reserve('one',1)
            ledger.reserve('three',units('.01'));ledger.finish('three',None)
            with self.assertRaises(ValueError):ledger.reserve('four',1)
            self.assertEqual(ledger.totals()['unresolved_calls'],1);ledger.close()

    def test_crash_reservation_and_unexpected_cost_stay_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'ledger.db';ledger=Ledger(path);ledger.reserve('one',units('.01'));ledger.close()
            ledger=Ledger(path)
            with self.assertRaises(ValueError):ledger.reserve('two',1)
            self.assertEqual(ledger.finish('one','.02'),'unknown');ledger.close()

    def fake_run(self,failure=None,only_model=None):
        calls=[];real_client=httpx.Client
        rows=catalog()['data']
        for row,model in zip(rows,MODELS):row['id']=model
        def transport(request):
            if request.url.path.endswith('/models'):return httpx.Response(200,json={'data':rows})
            if request.url.path.endswith('/key'):return httpx.Response(200,json={'data':{'limit_remaining':47,'usage':3}})
            payload=json.loads(request.content);calls.append(payload)
            if failure=='http':return httpx.Response(403,stream=httpx.ByteStream(b'{"error":"forbidden sk-or-test-only"}'))
            if failure=='timeout':raise httpx.ReadTimeout('test')
            response=envelope(payload['model'],len(calls)<=2)
            answer=json.loads(response['choices'][0]['message']['content'])
            answer['evidence']=[{'text':v['text']} for v in answer['evidence']]
            response['choices'][0]['message']['content']=json.dumps(answer)
            if failure=='unknown_cost':response.pop('usage')
            if failure=='invalid':response['choices'][0]['finish_reason']='length'
            return httpx.Response(200,json=response)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'cases.json';source.write_text(json.dumps(cases()),encoding='utf-8')
            with patch('app.model_trial_live.private_key',return_value='sk-or-test-only'),patch('app.model_trial_live.httpx.Client',side_effect=lambda **kwargs:real_client(transport=httpx.MockTransport(transport),**kwargs)):
                if only_model:source.write_text(json.dumps(cases()[:1]),encoding='utf-8')
                result=run(source,root/'output',root/'ledger.db',only_model=only_model)
            self.assertNotIn('sk-or-test-only',(root/'output/report.json').read_text())
            return result,calls

    def test_finite_success_alternates_models_and_logs_actual_cost(self):
        result,calls=self.fake_run()
        self.assertEqual(len(calls),4);self.assertTrue(result['completed'])
        self.assertEqual([c['model'] for c in calls],MODELS*2)
        self.assertIn('max_price',calls[0]['provider']);self.assertFalse(calls[0]['provider']['allow_fallbacks'])
        self.assertEqual(result['ledger']['accounted_or_reserved_usd'],'0.00004')

    def test_http_timeout_and_missing_cost_stop_without_retry(self):
        for failure in ('http','timeout','unknown_cost'):
            result,calls=self.fake_run(failure)
            self.assertEqual(len(calls),1);self.assertFalse(result['completed']);self.assertEqual(result['ledger']['unresolved_calls'],1)
            if failure=='http':self.assertIn('forbidden',result['calls'][0]['http_diagnostic'])

    def test_invalid_but_billed_response_counted_as_failure(self):
        result,calls=self.fake_run('invalid')
        self.assertEqual(len(calls),4);self.assertEqual(sum(row['valid'] for row in result['calls']),0)
        self.assertEqual(result['ledger']['unresolved_calls'],0)

    def test_manual_followup_never_calls_the_other_candidate(self):
        result,calls=self.fake_run(only_model=MODELS[1])
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['model'],MODELS[1])
        self.assertTrue(result['completed']);self.assertEqual(result['mode'],'single_case_development_followup')
