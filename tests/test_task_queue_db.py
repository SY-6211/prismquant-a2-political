"""Opt-in queue tests, including races and a real subprocess crash, in an isolated schema."""
import importlib.util
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.collection_rules import SUPPORTED
from app.core.config import PROJECT_ROOT, Settings
from app.core.database import database_url
from app.main import create_app
from app.repositories.tasks import TaskLeaseLost, claim, enqueue, retry_reviewed, select_task, select_attempts
from app.task_jobs import enqueue_tick
from app.task_queue import enqueue_history, work
from app.task_types import Outcome
from test_task_queue import history_payload


@unittest.skipUnless(os.environ.get('PQ_TASK_DB_TESTS') == '1', 'Isolated-schema queue database tests are opt-in')
class TaskDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'pq_task_test_' + uuid4().hex
        cls.base = create_engine(database_url(Settings()), poolclass=NullPool, hide_parameters=True)
        with cls.base.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {cls.schema}'))
        cls.addClassCleanup(cls.drop_schema)
        cls.engine = create_engine(database_url(Settings()), poolclass=NullPool, hide_parameters=True,
            connect_args={'options': f'-c search_path={cls.schema},pg_catalog -c lock_timeout=5000 -c statement_timeout=15000'})
        cls.addClassCleanup(cls.engine.dispose)
        with cls.engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                for path in sorted((PROJECT_ROOT / 'migrations' / 'versions').glob('[0-9]*.py')):
                    spec = importlib.util.spec_from_file_location(path.stem, path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    module.upgrade()
            for name, (sid, pid, url, provenance) in SUPPORTED.items():
                conn.execute(text("INSERT INTO people (person_id,name_original,monitoring_enabled) VALUES (:id,:name,true)"),
                             {'id': pid, 'name': 'Synthetic ' + name})
                conn.execute(text('INSERT INTO sources (source_id,name,publisher_name,provenance_type,feed_url,enabled) '
                    "VALUES (:id,:name,'Synthetic publisher',:provenance,:url,true)"), {'id': sid, 'name': name, 'provenance': provenance, 'url': url})
                conn.execute(text('INSERT INTO person_sources (source_id,person_id,enabled) VALUES (:s,:p,true)'), {'s': sid, 'p': pid})

    @classmethod
    def drop_schema(cls):
        assert cls.schema.startswith('pq_task_test_') and len(cls.schema) == 45
        with cls.base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {cls.schema} CASCADE'))
        cls.base.dispose()

    def setUp(self):
        with self.engine.begin() as conn:
            self.assertEqual(conn.execute(text('SELECT current_schema()')).scalar_one(), self.schema)
            conn.execute(text('TRUNCATE agent_quality_reviews,agent_quality_cases,agent_answer_feedback,speed_receipts,speed_deliveries,speed_candidates,speed_rounds,web_document_links,web_documents,web_pilot_runs,kb_chunks,kb_documents,reminder_feedback,reminder_actions,reminders,reminder_channels,agent_auto_jobs,agent_auto_grants,agent_web_qa_jobs,agent_web_qa_grants,agent_qa_results,agent_qa_executions,agent_qa_runs,agent_chat_messages,agent_chat_conversations,agent_chat_tokens,agent_chat_users,agent_analysis_results,agent_analysis_runs,annotation_runs,annotation_labels,annotation_samples,annotation_datasets,keyword_publications,keyword_previews,keyword_versions,keyword_policies,content_changes,ecb_discovery_candidates,ecb_http_cache,ecb_discovery_state,task_reviews,task_attempts,collection_tasks,event_record_links,event_observations,event_versions, '
                'monitor_events,source_channels,archive_post_details,public_records,collection_run_items,collection_runs,collection_schedule RESTART IDENTITY'))
            conn.execute(text('UPDATE sources SET enabled=true'))

    def submit(self, source='ecb', *, request='test', kind='collect_source', payload=None, max_failures=3):
        with self.engine.begin() as conn:
            return enqueue(conn, kind, payload or {'source': source, 'limit': 1}, request, max_failures=max_failures)

    def get(self, identity):
        with self.engine.connect() as conn:
            return select_task(conn, UUID(str(identity)))

    def expire(self, identity):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE collection_tasks SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE task_id=:id"), {'id': identity})

    def ready(self, identity):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE collection_tasks SET available_at=clock_timestamp()-interval '1 second' WHERE task_id=:id"), {'id': identity})

    def test_duplicate_enqueue_and_active_poll_coalescing(self):
        a = self.submit()
        b = self.submit()
        c = self.submit(request='another-scheduler-tick')
        self.assertTrue(a['created'])
        self.assertEqual(a['task_id'], b['task_id'])
        self.assertEqual(a['task_id'], c['task_id'])
        job = claim(self.engine)
        job.finish(Outcome('succeeded', 'completed'))
        job.close()
        self.assertFalse(self.submit()['created'])
        self.assertTrue(self.submit(request='new-poll')['created'])

    def test_raw_wins_priority_and_raw_worker_never_claims_history(self):
        history = self.submit(kind='history_item', payload=history_payload())
        raw = self.submit()
        job = claim(self.engine, ('raw','history'))
        self.assertEqual(str(job.task['task_id']), raw['task_id'])
        job.finish(Outcome('succeeded', 'completed'))
        job.close()
        self.assertIsNone(claim(self.engine, ('raw',)))
        job = claim(self.engine, ('history',))
        self.assertEqual(str(job.task['task_id']), history['task_id'])
        job.close()

    def test_concurrent_workers_claim_one_task_once(self):
        self.submit()
        with ThreadPoolExecutor(max_workers=3) as pool:
            leases = list(pool.map(lambda _: claim(self.engine), range(3)))
        self.assertEqual(sum(job is not None for job in leases), 1)
        for lease in leases:
            if lease:
                lease.close()

    def test_source_resource_lock_and_other_source_are_independent(self):
        self.submit()
        self.submit(kind='history_item', payload=history_payload())
        ecb = claim(self.engine)
        self.addCleanup(ecb.close)
        self.assertIsNone(claim(self.engine, ('history',)))
        trump = self.submit('trump')
        other = claim(self.engine)
        self.assertEqual(str(other.task['task_id']), trump['task_id'])
        other.close()

    def test_live_session_is_not_stolen_after_lease_timeout(self):
        task = self.submit()
        old = claim(self.engine)
        self.expire(task['task_id'])
        self.assertIsNone(claim(self.engine))
        old.checkpoint()
        old.finish(Outcome('succeeded', 'completed'))
        old.close()
        self.assertEqual(self.get(task['task_id'])['attempt_count'], 1)

    def test_crashed_process_recovers_and_preserves_attempt_history(self):
        task = self.submit()
        code = """import os
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from app.core.config import Settings
from app.core.database import database_url
from app.repositories.tasks import claim
engine=create_engine(database_url(Settings()),poolclass=NullPool,connect_args={'options':'-c search_path='+os.environ['PQ_TEST_SCHEMA']+',pg_catalog'})
lease=claim(engine)
assert lease is not None
os._exit(29)
"""
        env = {**os.environ, 'PQ_TEST_SCHEMA': self.schema}
        process = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=PROJECT_ROOT, env=env, capture_output=True, timeout=20,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(process.returncode, 29, process.stderr.decode('utf-8', errors='replace'))
        self.expire(task['task_id'])
        recovered = claim(self.engine)
        self.assertEqual(recovered.task['attempt_count'], 2)
        recovered.finish(Outcome('succeeded', 'completed'))
        recovered.close()
        with self.engine.connect() as conn:
            attempts = select_attempts(conn, UUID(task['task_id']))
        self.assertEqual([a['status'] for a in attempts], ['succeeded','abandoned'])

    def test_expired_max_failures_becomes_dead_and_old_lease_cannot_ack(self):
        task = self.submit(max_failures=1)
        old = claim(self.engine)
        old.close()
        self.expire(task['task_id'])
        self.assertIsNone(claim(self.engine))
        self.assertEqual(self.get(task['task_id'])['status'], 'dead')
        with self.assertRaises(TaskLeaseLost):
            old.finish(Outcome('succeeded', 'completed'))

    def test_retry_backoff_retry_after_dead_letter_and_reviewed_retry(self):
        task = self.submit(max_failures=2)
        first = claim(self.engine)
        deadline = datetime.now(timezone.utc) + timedelta(hours=2)
        first.finish(Outcome('retry', 'network_timeout', not_before=deadline))
        first.close()
        stored = self.get(task['task_id'])
        self.assertGreaterEqual(stored['available_at'], deadline)
        self.assertIsNone(claim(self.engine))
        self.ready(task['task_id'])
        second = claim(self.engine)
        second.finish(Outcome('retry', 'network_timeout'))
        second.close()
        self.assertEqual(self.get(task['task_id'])['status'], 'dead')
        with self.engine.begin() as conn:
            retry_reviewed(conn, UUID(task['task_id']), 'Synthetic review: source is available again')
        self.assertEqual(self.get(task['task_id'])['failure_count'], 0)
        third = claim(self.engine)
        self.assertEqual(third.task['attempt_count'], 3)
        third.finish(Outcome('succeeded', 'completed'))
        third.close()

    def test_deferral_does_not_exhaust_failure_budget(self):
        task = self.submit(max_failures=1)
        lease = claim(self.engine)
        lease.finish(Outcome('deferred', 'source_busy'))
        lease.close()
        stored = self.get(task['task_id'])
        self.assertEqual((stored['status'], stored['failure_count']), ('retry', 0))

    def test_history_submission_resumes_across_small_batches_without_readding_old_jobs(self):
        plan = {'version': 1, 'source': 'ecb', 'since': '2026-10-01', 'until': '2026-11-07',
                'candidates': [history_payload(n)['entry'] for n in (1,2,3)]}
        with self.engine.begin() as conn:
            preview = enqueue_history(conn, plan, limit=1)
            self.assertEqual(preview['selected'], 1)
            self.assertEqual(conn.execute(text('SELECT count(*) FROM collection_tasks')).scalar_one(), 0)
            for expected in (2,1,0):
                result = enqueue_history(conn, plan, limit=1, apply=True)
                self.assertEqual(result['selected'], 1)
                self.assertEqual(result['pending_after_batch'], expected)
            self.assertEqual(enqueue_history(conn, plan, apply=True)['selected'], 0)

    def test_queue_admission_is_database_only_and_disabled_sources_are_not_enqueued(self):
        with patch('app.task_jobs.engine_for', return_value=self.engine):
            preview = enqueue_tick(None, ['ecb'], apply=False)
            self.assertEqual(preview['items'][0]['status'], 'would_queue')
            with self.engine.begin() as conn:
                conn.execute(text('UPDATE sources SET enabled=false'))
            result = enqueue_tick(None, ['ecb'], apply=True)
            self.assertEqual(result['items'][0]['status'], 'disabled')
            self.assertFalse(result['network_requested'])

    def synthetic_runner(self, config, limit, http, *, before_request, save, **kwargs):
        from test_import_ecb_speech import full_detail
        before_request()
        result = save(config, full_detail(), None)
        return {'status': 'completed', 'saved': True, 'source_enabled': True, 'monitoring_enabled': True, 'link_enabled': True,
                'selection': {'selected': 1}, 'summary': {'attempted': 1, 'inserted': int(result['action']=='inserted'),
                'already_exists': int(result['action']=='already_exists'), 'failed': 0, 'needs_review': 0, 'interrupted': 0, 'not_attempted': 0},
                'items': [{'url': result['retrieved_url'], 'status': result['action'], 'record_id': result['record_id']}]}

    def test_real_raw_handler_uses_existing_writer_and_records_linked_run(self):
        task = self.submit()
        with patch('app.collection_execution.engine_for', return_value=self.engine), \
             patch('app.collection_runs.engine_for', return_value=self.engine), \
             patch('app.scheduler.runner_for', return_value=self.synthetic_runner):
            result = work(Settings(), engine=self.engine)
        self.assertEqual(result['items'][0]['status'], 'succeeded')
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT count(*) FROM public_records')).scalar_one(), 1)
            self.assertIsNotNone(select_attempts(conn, UUID(task['task_id']))[0]['run_id'])

    def test_real_history_handler_uses_a_single_candidate_and_manual_history_run(self):
        self.submit(kind='history_item', payload=history_payload())
        from app import batch_ecb
        with patch('app.collection_execution.engine_for', return_value=self.engine), \
             patch('app.collection_runs.engine_for', return_value=self.engine), \
             patch('app.backfill.source_tools', return_value=(batch_ecb, SimpleNamespace(run_import=self.synthetic_runner))):
            result = work(Settings(), lanes=('history',), engine=self.engine)
        self.assertEqual(result['items'][0]['status'], 'succeeded')
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT mode FROM collection_runs')).scalar_one(), 'manual_history_import')

    def test_commit_before_ack_can_replay_without_duplicate_content(self):
        from app.import_ecb_speech import save_speech
        from test_import_ecb_speech import full_detail
        self.submit()
        first = claim(self.engine)
        def write_once():
            with self.engine.begin() as conn:
                return save_speech(conn, full_detail(), None, datetime.now(timezone.utc))
        a = write_once()
        first.close()  # Simulate loss between content commit and task acknowledgement.
        self.expire(first.task['task_id'])
        next_lease = claim(self.engine)
        b = write_once()
        next_lease.finish(Outcome('succeeded', 'completed'))
        next_lease.close()
        self.assertEqual(a['record_id'], b['record_id'])
        self.assertEqual(b['action'], 'already_exists')
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT count(*) FROM public_records')).scalar_one(), 1)

    def test_unknown_handler_exception_stops_for_review_and_does_not_leak_error(self):
        task = self.submit()
        result = work(None, engine=self.engine, handler=lambda *_: (_ for _ in ()).throw(RuntimeError('SECRET')))
        self.assertEqual(result['items'][0]['status'], 'needs_review')
        self.assertNotIn('SECRET', str(self.get(task['task_id'])))

    def test_lane_capacity_backpressure_still_allows_idempotent_retries(self):
        with patch('app.repositories.tasks.MAX_ACTIVE_PER_LANE', 1):
            self.submit(kind='history_item', payload=history_payload())
            self.assertFalse(self.submit(kind='history_item', payload=history_payload())['created'])
            with self.assertRaises(ValueError):
                self.submit(kind='history_item', payload=history_payload(2))

    def test_worker_rechecks_source_switch_before_network_or_run_creation(self):
        self.submit()
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE sources SET enabled=false'))
        with patch('app.collection_execution.engine_for', return_value=self.engine):
            result = work(Settings(), engine=self.engine)
        self.assertEqual(result['items'][0]['status'], 'cancelled')
        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text('SELECT count(*) FROM collection_runs')).scalar_one(), 0)

    def test_stale_token_cannot_complete_a_task(self):
        task = self.submit()
        job = claim(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE collection_tasks SET lease_token=:token WHERE task_id=:id'),
                         {'id': task['task_id'], 'token': uuid4()})
        with self.assertRaises(TaskLeaseLost):
            job.finish(Outcome('succeeded', 'completed'))
        job.close()
        self.assertEqual(self.get(task['task_id'])['status'], 'running')

    def test_real_task_api_is_bounded_and_does_not_modify_queue(self):
        task = self.submit()
        first = claim(self.engine)
        first.finish(Outcome('deferred', 'source_busy'))
        first.close()
        self.ready(task['task_id'])
        second = claim(self.engine)
        second.finish(Outcome('succeeded', 'completed'))
        second.close()
        def read(settings, selector, *args):
            with self.engine.connect() as conn:
                return selector(conn, *args)
        with patch('app.api.tasks.read_query', side_effect=read), TestClient(create_app(Settings(_env_file=None))) as client:
            for path in ('/task-queue','/collection-tasks?limit=1',f'/collection-tasks/{task["task_id"]}',
                         f'/collection-tasks/{task["task_id"]}/attempts'):
                response = client.get('/api/v1' + path)
                self.assertEqual(response.status_code, 200, response.text)
            page = client.get(f'/api/v1/collection-tasks/{task["task_id"]}/attempts?limit=1').json()
            self.assertEqual(page['next_before'], 2)
            self.assertNotIn('lease_token', page['items'][0])
            self.assertTrue(page['items'][0]['started_at'].endswith('Z'))
            last = client.get(f'/api/v1/collection-tasks/{task["task_id"]}/attempts?limit=1&before=2').json()
            self.assertEqual(last['items'][0]['attempt_number'], 1)
            self.assertIsNone(last['next_before'])
        self.assertEqual(self.get(task['task_id'])['attempt_count'], 2)
