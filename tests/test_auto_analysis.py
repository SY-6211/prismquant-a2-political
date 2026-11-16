"""Synthetic automation cases in a disposable PostgreSQL schema; zero paid calls."""
from contextlib import contextmanager
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID,uuid4
import httpx
from sqlalchemy import text
from app import auto_analysis as worker
from app.agent_analysis import MODEL,PROMPT_VERSION,prepare_context,validate_analysis
from app.agent_tools import ToolSession
from app.annotation_model import digest
from app.event_model import snapshot_from_record
from app.model_trial_live import units
from app.repositories import auto_analysis as repo,agent_analysis as analyses
from app.repositories.agent_tools import SnapshotReader
from app.repositories.events import save_snapshot
import test_agent_qa_db as fixtures
from test_event_model import record

@unittest.skipUnless(os.environ.get('PQ_AUTO_ANALYSIS_DB_TESTS')=='1','Isolated auto analysis DB tests are opt-in')
class AutoAnalysisTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.QADatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(fixtures.QADatabaseTests.drop_schema.__func__)
    def setUp(self):
        fixtures.QADatabaseTests.setUp(self)
        self.approval='synthetic-'+str(uuid4());self.posts=0
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        replacement=patch('app.auto_analysis.service',side_effect=lambda settings,fn,*args:fn(self.c,*args));replacement.start();self.addCleanup(replacement.stop)
        @contextmanager
        def session(settings,people):yield ToolSession(SnapshotReader(self.c),people)
        replacement=patch('app.auto_analysis.open_session',side_effect=session);replacement.start();self.addCleanup(replacement.stop)
        replacement=patch('app.auto_analysis.private_key',return_value='sk-or-synthetic-not-real');replacement.start();self.addCleanup(replacement.stop)
    def grant(self,cap='.1',calls=5):
        self.g=repo.grant(self.c,self.person,self.approval,units(cap),calls,2);return self.g
    def new(self,review='accepted',**changes):
        source=self.c.execute(text('SELECT source_id FROM source_channels c JOIN event_versions v USING(channel_id) WHERE v.version_id=:id'),{'id':self.version['version_id']}).scalar_one()
        data=record(**{'person_id':self.person,'body_text':'Alpha 😀政策 tariffs are discussed.','original_url':'https://truthsocial.com/@qa/'+str(uuid4().int),**changes})
        return save_snapshot(self.c,source,'rss',snapshot_from_record(data,review_status=review),datetime.now(timezone.utc))
    def queued(self):
        self.grant();self.target=self.new();self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],1)
        return self.c.execute(text('SELECT * FROM agent_auto_jobs WHERE grant_id=:id'),{'id':self.g['grant_id']}).mappings().one()
    def ready(self):
        self.job=self.queued();self.assertEqual(worker.prepare_one(None,self.approval)['status'],'prepared');return self.job
    def response(self):
        row=self.c.execute(text('SELECT r.context FROM agent_auto_jobs j JOIN agent_analysis_runs r USING(run_id) WHERE j.job_id=:id'),{'id':self.job['job_id']}).mappings().one()
        ref=row['context']['target_refs'][0]
        analysis={'relevant':True,'topics':['trade_policy'],'importance':'medium','summary_zh':'据合成归档，作者讨论政策。','facts':[{'text_zh':'原文提及政策。','citations':[{'source_ref':ref,'text':'政策 tariffs'}]}],'history_assessment':'not_assessed','comparison_zh':'未比较。','comparison_citations':[],'hypotheses':[],'needs_review':True}
        return {'id':'synthetic-auto','model':MODEL,'provider':'Synthetic','usage':{'cost':.001},'choices':[{'finish_reason':'stop','message':{'content':json.dumps(analysis,ensure_ascii=False)}}]}
    def execute(self,response=None,timeout=False,preflight=False):
        value=self.response() if response is None else response
        def handler(request):
            if request.method=='POST':
                self.posts+=1
                if timeout:raise httpx.ReadTimeout('synthetic timeout',request=request)
                return httpx.Response(200,json=value)
            if request.url.path.endswith('/models'):return httpx.Response(200,json={'data':[] if preflight else [{'id':MODEL,'supported_parameters':['structured_outputs'],'pricing':{'prompt':'.000001','completion':'.000002'}}]})
            return httpx.Response(200,json={'data':{'limit_remaining':10}})
        return worker.execute_one(None,self.approval,client_factory=lambda:httpx.Client(transport=httpx.MockTransport(handler)),receipt_dir=Path(self.tmp.name))
    def test_disabled_default_does_not_enqueue_old_or_new_content(self):
        self.new();self.assertEqual(repo.sync(self.c,'not-authorized')['enqueued'],0)
        self.assertEqual(repo.status(self.c)['enabled_count'],0);self.assertEqual(worker.execute_one(None,'not-authorized')['model_requests'],0)
    def test_activation_starts_after_current_cursor_and_sync_is_deduplicated(self):
        self.new();g=self.grant();self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],0)
        self.new();self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],1);self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],0)
        self.assertEqual(self.c.execute(text('SELECT count(*) FROM agent_auto_jobs WHERE grant_id=:id'),{'id':g['grant_id']}).scalar_one(),1)
    def test_invalid_duplicate_and_overlapping_approvals_rejected(self):
        self.grant()
        for cap,calls,hours in [(0,1,2),(units('11'),1,2),(units('.1'),101,2),(units('.1'),1,25)]:
            with self.assertRaises(ValueError):repo.grant(self.c,self.person,'bad',cap,calls,hours)
        with self.assertRaises(ValueError):repo.grant(self.c,self.person,self.approval,units('.1'),5,2)
        with self.assertRaises(ValueError):repo.grant(self.c,self.person,'overlapping',units('.1'),5,2)
    def test_accepted_new_content_completes_with_exact_evidence_and_one_post(self):
        self.ready();result=self.execute();self.assertEqual(result['status'],'completed');self.assertEqual(self.execute()['status'],'idle');self.assertEqual(self.posts,1)
        saved=analyses.select_detail(self.c,result['run_id']);self.assertTrue(saved['analysis']['facts'][0]['citations'][0]['valid']);self.assertEqual(repo.budget(self.c,self.g['grant_id'])['used_or_reserved_usd'],'0.001')
    def test_completed_analysis_is_reused_without_generation(self):
        job=self.queued()
        context=prepare_context(ToolSession(SnapshotReader(self.c),[self.person]),UUID(self.target['event_id']),UUID(self.target['version_id']))
        run=analyses.prepared(self.c,'synthetic-cache-'+str(uuid4()),context,MODEL,PROMPT_VERSION)
        analysis=validate_analysis(context,{'relevant':False,'topics':[],'importance':'low','summary_zh':'合成缓存样本。','facts':[],'history_assessment':'not_assessed','comparison_zh':'','comparison_citations':[],'hypotheses':[],'needs_review':True})
        analyses.save_result(self.c,run['run_id'],'completed',analysis,digest({'fixture':1}),{'reported_cost_usd':'0'})
        self.assertTrue(worker.prepare_one(None,self.approval)['cached']);self.assertEqual(worker.execute_one(None,self.approval)['model_requests'],0)
        self.assertEqual(repo.budget(self.c,self.g['grant_id'])['remaining_requests'],5)
    def test_expiry_or_close_after_preparation_prevents_request(self):
        self.ready();repo.close(self.c,self.g['grant_id']);self.assertEqual(worker.execute_one(None,self.approval)['model_requests'],0);self.assertEqual(self.posts,0)
    def test_expired_grant_does_not_reserve(self):
        self.ready();self.c.execute(text("UPDATE agent_auto_grants SET expires_at=now()-interval '1 second',created_at=now()-interval '2 hours' WHERE grant_id=:id"),{'id':self.g['grant_id']})
        self.assertEqual(worker.execute_one(None,self.approval)['model_requests'],0)
    def test_insufficient_reservation_pauses_without_sending(self):
        self.ready();self.c.execute(text('UPDATE agent_auto_grants SET cap=1 WHERE grant_id=:id'),{'id':self.g['grant_id']})
        self.assertEqual(worker.execute_one(None,self.approval)['model_requests'],0);self.assertEqual(repo.budget(self.c,self.g['grant_id'])['pause_reason'],'insufficient_reservation')
    def test_unknown_cost_fences_further_automatic_work(self):
        self.ready();value=self.response();del value['usage']['cost'];self.assertEqual(self.execute(value)['status'],'unknown');self.assertEqual(repo.budget(self.c,self.g['grant_id'])['status'],'needs_review');self.assertEqual(self.posts,1)
    def test_timeout_is_never_automatically_retried(self):
        self.ready();self.assertEqual(self.execute(timeout=True)['status'],'unknown');worker.execute_one(None,self.approval);self.assertEqual(self.posts,1)
    def test_bad_citation_is_rejected_but_known_cost_is_recorded(self):
        self.ready();value=self.response();answer=json.loads(value['choices'][0]['message']['content']);answer['facts'][0]['citations'][0]['text']='invented';value['choices'][0]['message']['content']=json.dumps(answer)
        self.assertEqual(self.execute(value)['status'],'rejected');self.assertEqual(repo.budget(self.c,self.g['grant_id'])['used_or_reserved_usd'],'0.001')
    def test_preflight_failure_is_known_unsent_and_zero_cost(self):
        self.ready();self.assertEqual(self.execute(preflight=True)['status'],'failed');self.assertEqual(self.posts,0);self.assertEqual(repo.budget(self.c,self.g['grant_id'])['used_or_reserved_usd'],'0')
    def test_reserved_eval_source_cannot_be_sent(self):
        self.ready()
        with patch('app.repositories.agent_analysis.context_has_reserved',return_value=True):self.assertEqual(worker.execute_one(None,self.approval)['model_requests'],0)
        self.assertEqual(self.posts,0)
    def test_stream_gap_pauses_instead_of_silently_skipping(self):
        self.grant();self.new();self.c.execute(text('UPDATE agent_auto_grants SET epoch=:epoch WHERE grant_id=:id'),{'epoch':uuid4(),'id':self.g['grant_id']})
        self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],0);self.assertEqual(repo.budget(self.c,self.g['grant_id'])['pause_reason'],'content_stream_gap')
    def test_receipt_recovers_storage_failure_without_resending(self):
        self.ready()
        with patch('app.repositories.auto_analysis.finish',side_effect=RuntimeError('synthetic DB failure')):
            with self.assertRaises(RuntimeError):self.execute()
        self.assertEqual(repo.budget(self.c,self.g['grant_id'])['status'],'busy');self.assertEqual(self.posts,1)
        with patch('app.auto_analysis.RUNTIME',Path(self.tmp.name).parent):
            target=Path(self.tmp.name).parent/'receipts';target.mkdir(exist_ok=True)
            file=Path(self.tmp.name)/(str(self.job['job_id'])+'.json');copy=target/file.name;copy.write_bytes(file.read_bytes());self.addCleanup(copy.unlink)
            self.assertEqual(worker.recover(None,self.job['job_id'])['status'],'completed')
        self.assertEqual(self.posts,1)
    def test_two_workers_cannot_claim_the_same_prepared_job(self):
        self.ready();self.tx.commit()
        def start():
            with self.engine.begin() as c:return repo.start(c,self.approval)
        with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(lambda _:start(),range(2)))
        self.assertEqual(sum(row is not None for row in rows),1);row=next(row for row in rows if row)
        value={'run_id':str(row['run_id']),'request_hash':row['request_hash'],'status':'failed','analysis':None,'response_hash':digest({'synthetic_unsent':True}),'receipt':{'model_requests':0,'reported_cost_usd':'0'},'failure_code':'synthetic_unsent'}
        repo.finish(self.c,row['job_id'],value);self.c.commit()

    def test_needs_review_content_never_enters_automatic_queue(self):
        self.grant();self.new(review='needs_review');self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],0)

    def test_superseded_version_is_skipped_before_preparation(self):
        job=self.queued()
        original=self.c.execute(text("SELECT payload->>'original_url' FROM event_versions WHERE version_id=:id"),{'id':self.target['version_id']}).scalar_one()
        self.new(original_url=original,body_text='Revised policy statement with different content.')
        self.assertTrue(worker.prepare_one(None,self.approval)['skipped'])
        self.assertEqual(self.c.execute(text('SELECT failure_code FROM agent_auto_jobs WHERE job_id=:id'),{'id':job['job_id']}).scalar_one(),'superseded_or_unaccepted')

    def test_request_count_exhaustion_stops_further_enqueue_and_send(self):
        self.ready();self.c.execute(text('UPDATE agent_auto_grants SET max_requests=1 WHERE grant_id=:id'),{'id':self.g['grant_id']});self.execute();self.new()
        self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],0);self.assertEqual(worker.execute_one(None,self.approval)['model_requests'],0);self.assertEqual(self.posts,1)

    def test_closing_during_provider_preflight_still_prevents_post(self):
        self.ready()
        def handler(request):
            if request.url.path.endswith('/models'):return httpx.Response(200,json={'data':[{'id':MODEL,'supported_parameters':['structured_outputs'],'pricing':{'prompt':'.000001','completion':'.000002'}}]})
            if request.method=='POST':self.fail('Generation must not be sent after closing')
            repo.close(self.c,self.g['grant_id']);return httpx.Response(200,json={'data':{'limit_remaining':10}})
        result=worker.execute_one(None,self.approval,client_factory=lambda:httpx.Client(transport=httpx.MockTransport(handler)),receipt_dir=Path(self.tmp.name))
        self.assertEqual(result['status'],'failed');self.assertEqual(repo.budget(self.c,self.g['grant_id'])['used_or_reserved_usd'],'0')

    def test_public_status_is_read_only_and_has_no_enable_endpoint(self):
        from fastapi.testclient import TestClient
        from app.core.config import Settings
        from app.main import create_app
        with patch('app.api.auto_analysis.agent_analysis.read',return_value={'enabled_count':0,'jobs':{}}):
            with TestClient(create_app(Settings(_env_file=None))) as client:
                self.assertEqual(client.get('/api/v1/agent-analysis-automation').status_code,200)
                self.assertEqual(client.post('/api/v1/agent-analysis-automation').status_code,405)
                self.assertEqual(client.post('/api/v1/agent-analysis-automation/enable').status_code,404)

    def test_missing_change_rows_pause_without_advancing_cursor(self):
        g=self.grant();self.new();self.c.execute(text('DELETE FROM content_changes WHERE position>:cursor'),{'cursor':g['cursor_position']})
        self.assertEqual(repo.sync(self.c,self.approval)['enqueued'],0)
        value=repo.budget(self.c,g['grant_id']);self.assertEqual(value['pause_reason'],'content_stream_gap');self.assertEqual(value['cursor_position'],g['cursor_position'])
