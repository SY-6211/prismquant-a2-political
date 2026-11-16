"""ECB production-path tests in disposable schemas with synthetic HTTP only."""
import hashlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import text
from fastapi.testclient import TestClient

from app.batch_ecb import SOURCE_ID
from app.core.config import Settings
from app.ecb_fast import run_ecb_fast
from app.ecb_fast_http import ECBReader,ROBOTS,RSS,VERSIONS
from app.repositories.ecb_discovery import snapshot,state
from app.repositories.tasks import enqueue
from app.task_jobs import enqueue_tick
from app.task_queue import work
from app.main import create_app
from test_ecb_fast import metadata
from test_ecb_observer import OLD,NEW,OTHER,chunk,rss,speech
import test_task_queue_db as queue_fixtures


@unittest.skipUnless(os.environ.get('PQ_ECB_DB_TESTS')=='1','Isolated ECB database tests are opt-in')
class ECBFastDatabaseTests(unittest.TestCase):
    # Reuse fixture construction only, without inheriting unrelated test methods.
    setUpClass=classmethod(queue_fixtures.TaskDatabaseTests.setUpClass.__func__)
    drop_schema=classmethod(queue_fixtures.TaskDatabaseTests.drop_schema.__func__)

    def setUp(self):
        queue_fixtures.TaskDatabaseTests.setUp(self)
        self.urls=[OLD]
        self.rss_urls=[OLD]
        self.version=1
        self.calls=[]
        self.custom=None
        self.patches=[patch('app.collection_execution.engine_for',return_value=self.engine),
                      patch('app.collection_runs.engine_for',return_value=self.engine),
                      patch('app.ecb_http_pacing.pace',side_effect=self.fake_pace)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def fake_pace(self,conn,require,**kwargs):
        with conn.begin(): require()

    def transport(self,request):
        self.calls.append(request)
        if self.custom:
            response=self.custom(request)
            if response is not None:return response
        url=str(request.url)
        if url==ROBOTS:
            body=b'User-agent: *\nDisallow:\nCrawl-delay: 5'
        elif url==VERSIONS:
            body=json.dumps([{'version':str(self.version),'hash':'abc'}]).encode()
        elif url.endswith('metadata.json'):
            body=json.dumps(metadata(total_records=len(self.urls))).encode()
        elif url.endswith('chunk_0.json'):
            body=json.dumps(chunk(self.urls)).encode()
        elif url==RSS:
            body=rss(self.rss_urls).encode()
        else:
            body=speech().encode()
        etag='"'+hashlib.sha256(body).hexdigest()+'"'
        headers={'ETag':etag,'Cache-Control':'max-age=60','Set-Cookie':'SHOULD_NOT_PROPAGATE=value'}
        if request.headers.get('if-none-match')==etag:
            return httpx.Response(304,headers=headers)
        return httpx.Response(200,content=body,headers=headers)

    def factory(self):
        return httpx.Client(transport=httpx.MockTransport(self.transport),follow_redirects=False)

    def run_round(self,commit=True,limit=3):
        return run_ecb_fast(Settings(),limit,commit=commit,client_factory=self.factory)

    def expire(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE ecb_discovery_state SET last_scan_at=clock_timestamp()-interval '61 seconds',last_host_request_at=NULL"))
            conn.execute(text("UPDATE ecb_http_cache SET fresh_until=clock_timestamp()-interval '1 second'"))

    def count(self,table):
        with self.engine.connect() as conn:
            return conn.execute(text('SELECT count(*) FROM '+table)).scalar_one()

    def bootstrap_new(self):
        result=self.run_round()
        self.assertEqual(result['status'],'no_new_candidates',result)
        self.version=2;self.urls=[NEW,OLD];self.rss_urls=[NEW,OLD];self.expire()

    def test_first_success_is_baseline_not_new_or_historical_ingestion(self):
        result=self.run_round()
        self.assertEqual(result['status'],'no_new_candidates',result)
        self.assertTrue(result['discovery']['index']['baseline_established'])
        self.assertEqual(self.count('public_records'),0)
        with self.engine.connect() as conn:
            row=conn.execute(text('SELECT * FROM ecb_discovery_candidates')).mappings().one()
        self.assertEqual(row['status'],'baseline')
        self.assertEqual(set(row['channel_metadata']),{'web_index','rss'})

    def test_new_index_candidate_verified_saved_once_and_correct_channel_retained(self):
        self.bootstrap_new()
        result=self.run_round()
        self.assertEqual(result['summary']['inserted'],1,result)
        self.assertEqual(self.count('public_records'),1)
        with self.engine.connect() as conn:
            channel=conn.execute(text('SELECT transport FROM source_channels')).scalar_one()
            item=conn.execute(text('SELECT * FROM ecb_discovery_candidates WHERE url=:url'),{'url':NEW}).mappings().one()
            observed=conn.execute(text('SELECT metadata FROM event_observations')).scalar_one()
        self.assertEqual(channel,'web_index')
        self.assertEqual(item['status'],'saved')
        self.assertIn('discovered_at',observed)
        self.expire();self.run_round()
        self.assertEqual(self.count('public_records'),1)

    def test_attribution_mismatch_never_writes_target_speech(self):
        self.bootstrap_new()
        self.custom=lambda req:httpx.Response(200,text=speech().replace('Speech by Christine Lagarde','Speech by Other Person')) if str(req.url)==NEW else None
        result=self.run_round()
        self.assertEqual(result['summary']['needs_review'],1,result)
        self.assertEqual(self.count('public_records'),0)

    def test_pending_survives_a_moving_window_and_detail_limits(self):
        self.bootstrap_new()
        self.urls=[NEW,OTHER,OLD];self.rss_urls=[NEW,OTHER,OLD]
        result=self.run_round(limit=1)
        self.assertEqual(result['summary']['inserted'],1,result)
        self.version=3;self.urls=[OLD];self.rss_urls=[OLD];self.expire()
        result=self.run_round(limit=1)
        self.assertEqual(result['summary']['inserted'],1,result)
        self.assertEqual(self.count('public_records'),2)

    def test_unchanged_version_uses_persistent_window_and_304_validators(self):
        self.run_round();self.calls=[];self.expire()
        result=self.run_round()
        paths=[str(req.url) for req in self.calls]
        self.assertNotIn('metadata.json',' '.join(paths))
        self.assertNotIn('chunk_0.json',' '.join(paths))
        self.assertTrue(result['discovery']['index_window_reused'])
        self.assertTrue(any(r['state']=='not_modified' for r in result['http_observations']))
        self.assertTrue(all('cookie' not in req.headers for req in self.calls))

    def test_first_304_without_body_stops_and_pauses_only_new_channel(self):
        self.custom=lambda req:httpx.Response(304) if str(req.url)==VERSIONS else None
        result=self.run_round()
        self.assertEqual(result['status'],'stopped')
        with self.engine.connect() as conn:
            self.assertIsNotNone(state(conn)['pause_reason'])
        self.assertEqual(self.count('collection_schedule'),0)

    def test_index_success_and_rss_failure_keep_separate_baseline_state(self):
        self.custom=lambda req:httpx.Response(503) if str(req.url)==RSS else None
        result=self.run_round()
        self.assertEqual(result['http_status'],503)
        with self.engine.connect() as conn:
            value=state(conn)
        self.assertTrue(value['index_ready'])
        self.assertFalse(value['rss_ready'])
        self.assertIsNotNone(value['retry_not_before'])
        self.assertEqual(self.count('collection_schedule'),0)

    def test_429_retry_after_prevents_another_round_without_new_network(self):
        self.custom=lambda req:httpx.Response(429,headers={'Retry-After':'600'}) if str(req.url)==VERSIONS else None
        result=self.run_round()
        self.assertEqual(result['http_status'],429)
        calls=len(self.calls)
        self.assertEqual(self.run_round()['status'],'backoff')
        self.assertEqual(len(self.calls),calls)

    def test_robots_redirects_and_disallowed_paths_stop_before_origin_fetch(self):
        for content in ('<html>Not robots</html>','User-agent: *\nDisallow: /foedb/'):
            with self.engine.begin() as conn:
                conn.execute(text('TRUNCATE ecb_http_cache,ecb_discovery_candidates,ecb_discovery_state'))
            self.calls=[]
            self.custom=lambda req:httpx.Response(200,text=content) if str(req.url)==ROBOTS else None
            result=self.run_round()
            self.assertEqual(result['status'],'stopped')
            self.assertEqual(len(self.calls),1)

    def test_detail_and_candidate_save_roll_back_together(self):
        self.bootstrap_new()
        with patch('app.ecb_fast.mark',side_effect=ValueError('Synthetic post-save failure')):
            result=self.run_round()
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(self.count('public_records'),0)
        self.assertEqual(self.count('event_versions'),0)

    def test_scan_interval_and_shared_source_lock_prevent_duplicate_requests(self):
        self.run_round();before=len(self.calls)
        self.assertEqual(self.run_round()['status'],'not_due')
        self.assertEqual(len(self.calls),before)
        from app.collection_execution import SourceLease
        with SourceLease(Settings(),'ecb'):
            self.assertEqual(self.run_round()['status'],'skipped_busy')

    def test_discovery_only_mode_keeps_content_pending_for_later_worker(self):
        self.bootstrap_new()
        result=self.run_round(commit=False)
        self.assertEqual(self.count('public_records'),0)
        self.expire()
        result=self.run_round(commit=True)
        self.assertEqual(result['summary']['inserted'],1,result)

    def test_queue_mode_runs_the_actual_index_handler(self):
        with self.engine.begin() as conn:
            enqueue(conn,'collect_source',{'source':'ecb','limit':1,'discovery':'ecb_index'},'fast-test')
        original=run_ecb_fast
        def run(config,limit,**kwargs):
            return original(config,limit,client_factory=self.factory,**kwargs)
        with patch('app.ecb_fast.run_ecb_fast',side_effect=run):
            result=work(Settings(),engine=self.engine)
        self.assertEqual(result['items'][0]['status'],'succeeded',result)
        self.assertEqual(self.count('public_records'),0)

    def test_discovery_status_and_candidates_api_never_trigger_fetch(self):
        self.run_round();before=len(self.calls)
        def reader(settings,selector,*args):
            with self.engine.connect() as conn:return selector(conn,*args)
        with patch('app.api.ecb_discovery.read_query',side_effect=reader),TestClient(create_app(Settings(_env_file=None))) as client:
            for path in ('/api/v1/ecb-discovery','/api/v1/ecb-discovery/candidates?limit=1'):
                response=client.get(path)
                self.assertEqual(response.status_code,200,response.text)
                self.assertNotIn('"body"',response.text)
        self.assertEqual(len(self.calls),before)

    def test_scheduler_uses_channel_interval_and_legacy_payloads_still_coalesce(self):
        self.run_round()
        with patch('app.task_jobs.engine_for',return_value=self.engine):
            result=enqueue_tick(None,['ecb'],apply=False,ecb_index=True)
            self.assertEqual(result['items'][0]['status'],'not_due')

    def test_shared_pacer_waits_and_rechecks_ownership(self):
        self.patches[2].stop()
        from app.ecb_http_pacing import pace
        from app.repositories.ecb_discovery import ensure_state
        sleeps=[];checks=[]
        with self.engine.connect() as conn:
            with conn.begin():
                ensure_state(conn)
                conn.execute(text('UPDATE ecb_discovery_state SET last_host_request_at=clock_timestamp()'))
            def sleep(seconds):
                sleeps.append(seconds)
                with conn.begin():conn.execute(text("UPDATE ecb_discovery_state SET last_host_request_at=clock_timestamp()-interval '6 seconds'"))
            pace(conn,lambda:checks.append(True),sleep=sleep)
        self.assertTrue(sleeps)
        self.assertTrue(all(0<value<=1 for value in sleeps))
        self.assertGreaterEqual(len(checks),2)

    def test_no_store_response_body_is_not_retained_in_http_cache(self):
        self.custom=lambda req:httpx.Response(200,text=rss([OLD]),headers={'Cache-Control':'no-store'}) if str(req.url)==RSS else None
        result=self.run_round()
        self.assertEqual(result['status'],'no_new_candidates',result)
        with self.engine.connect() as conn:
            count=conn.execute(text('SELECT count(*) FROM ecb_http_cache WHERE url=:url'),{'url':RSS}).scalar_one()
        self.assertEqual(count,0)

    def test_redirect_and_off_host_reference_are_not_followed(self):
        self.custom=lambda req:httpx.Response(302,headers={'Location':'https://example.invalid/forbidden'}) if str(req.url)==VERSIONS else None
        result=self.run_round()
        self.assertEqual(result['status'],'stopped')
        self.assertEqual(result['http_status'],302)
        self.assertTrue(all(req.url.host=='www.ecb.europa.eu' for req in self.calls))

    def test_disabled_source_and_request_budget_stop_before_extra_http(self):
        with self.engine.begin() as conn:conn.execute(text('UPDATE sources SET enabled=false'))
        self.assertEqual(self.run_round()['status'],'disabled')
        self.assertEqual(self.calls,[])
        with self.engine.begin() as conn:conn.execute(text('UPDATE sources SET enabled=true'))
        from app.collection_execution import SourceLease
        from app.repositories.ecb_discovery import ensure_state
        with SourceLease(Settings(),'ecb') as lease,self.factory() as client:
            with lease.connection.begin():ensure_state(lease.connection)
            reader=ECBReader(client,lease,max_requests=1)
            with self.assertRaises(ValueError):reader.discover()
            self.assertEqual(len(self.calls),1)
