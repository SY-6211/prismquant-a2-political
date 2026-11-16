import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.collection_runs import RunCounts
from app.collection_runs import execute_recorded, finish_run, start_run
from app.core.config import Settings
from app.core.database import database_url
from app.import_batch import main
from app.main import create_app
from app.repositories.collection_runs import select_run, select_runs


def report():
    return dict(status='completed_with_issues', saved=True, stop_reason=None,
                source_enabled=False, monitoring_enabled=False, link_enabled=False,
                selection={'selected': 3, 'precheck_needs_review': 1},
                summary=dict(attempted=3, inserted=1, already_exists=0, needs_review=1,
                             failed=1, interrupted=0, not_attempted=0, skipped_unsupported=0),
                precheck_results=[{'url': None, 'status': 'needs_review', 'reason': 'Unknown RSS type'}],
                items=[{'url': 'https://example.invalid/1', 'rss_title': 'Synthetic', 'status': 'inserted', 'record_id': str(uuid4()), 'reason': 'Saved', 'detail': {'body_text': 'NEVER_STORE_BODY'}},
                       {'url': 'https://example.invalid/2', 'status': 'needs_review', 'reason': 'Unknown structure'},
                       {'url': 'https://example.invalid/3', 'status': 'failed', 'reason': 'Database failed', 'write_outcome_unknown': True}])


def header():
    return dict(run_id=uuid4(), source='trump', source_id=uuid4(), person_id=uuid4(),
                mode='manual_batch_import', status='running', batch_limit=3,
                started_at=datetime.now(timezone.utc), finished_at=None, stop_reason=None,
                report_complete=False, source_enabled=None, monitoring_enabled=None,
                link_enabled=None, selection={}, summary={})


class CollectionRunTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None)))
        self.addCleanup(self.client.close)

    def test_start_failure_never_executes_runner_and_redacts_exception(self):
        runner = MagicMock()
        with patch('app.collection_runs.engine_for', side_effect=OperationalError('PRIVATE SQL', {}, Exception('PASSWORD'))):
            result = execute_recorded(Settings(_env_file=None), 'ecb', 3, None, runner)
        runner.assert_not_called()
        self.assertEqual(result['run_record_status'], 'start_failed')
        self.assertNotIn('PASSWORD', json.dumps(result))

    def test_logging_failure_does_not_retry_collection_or_erase_success(self):
        engine = MagicMock()
        runner = MagicMock(return_value=report())
        with patch('app.collection_runs.engine_for', return_value=engine), \
             patch('app.collection_runs.start_run', return_value=uuid4()), \
             patch('app.collection_runs.finish_run', side_effect=OperationalError('PRIVATE', {}, Exception('SECRET'))):
            result = execute_recorded(Settings(_env_file=None), 'trump', 3, None, runner)
        runner.assert_called_once()
        self.assertTrue(result['saved'])
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['run_record_status'], 'finalization_failed')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_unexpected_exception_or_interrupt_never_fabricates_zero_counts(self):
        for exc, expected in ((RuntimeError('SECRET'), 'stopped'), (KeyboardInterrupt(), 'interrupted')):
            with patch('app.collection_runs.engine_for', return_value=MagicMock()), \
                 patch('app.collection_runs.start_run', return_value=uuid4()), \
                 patch('app.collection_runs.finish_run') as finish:
                result = execute_recorded(Settings(_env_file=None), 'ecb', 3, None, MagicMock(side_effect=exc))
            self.assertEqual(result['status'], expected)
            self.assertIsNone(result['saved'])
            self.assertFalse(finish.call_args.kwargs['complete'])
            self.assertNotIn('SECRET', json.dumps(result))

    def test_summary_validation_rejects_inconsistent_and_negative_counts(self):
        for change in ({'inserted': 4}, {'failed': -1}):
            with self.assertRaises(ValueError):
                RunCounts(**{**report()['summary'], **change})

    def test_list_empty_running_unknown_counts_and_utc(self):
        with patch('app.api.collection_runs.read_runs', return_value=[]):
            self.assertEqual(self.client.get('/api/v1/collection-runs').json(), {'items': [], 'next_cursor': None})
        row = header()
        with patch('app.api.collection_runs.read_runs', return_value=[row, header()]):
            result = self.client.get('/api/v1/collection-runs?limit=1&source=trump&status=running').json()
        self.assertEqual(result['next_cursor'], str(row['run_id']))
        self.assertIsNone(result['items'][0]['summary'])
        self.assertTrue(result['items'][0]['started_at'].endswith('Z'))

    def test_detail_returns_phases_and_not_found(self):
        row = header()
        row['items'] = [{'phase': 'detail', 'position': 0, 'url': None, 'title': None, 'record_id': None,
                         'status': 'failed', 'reason': 'Review', 'write_outcome_unknown': True}]
        with patch('app.api.collection_runs.read_run', return_value=row):
            response = self.client.get('/api/v1/collection-runs/' + str(row['run_id']))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['items'][0]['write_outcome_unknown'])
        with patch('app.api.collection_runs.read_run', return_value=None):
            self.assertEqual(self.client.get('/api/v1/collection-runs/' + str(uuid4())).status_code, 404)

    def test_api_validation_errors_and_database_failure_are_safe(self):
        for path in ('/api/v1/collection-runs?source=other', '/api/v1/collection-runs?status=success',
                     '/api/v1/collection-runs?limit=0', '/api/v1/collection-runs?cursor=bad', '/api/v1/collection-runs/bad'):
            self.assertEqual(self.client.get(path).status_code, 422)
        with patch('app.api.collection_runs.read_runs', side_effect=OperationalError('PRIVATE', {}, Exception('SECRET'))):
            result = self.client.get('/api/v1/collection-runs')
            self.assertEqual(result.status_code, 503)
            self.assertNotIn('SECRET', result.text)
        row = {**header(), 'report_complete': True, 'summary': {'inserted': 'SECRET'}}
        with patch('app.api.collection_runs.read_run', return_value=row):
            result = self.client.get('/api/v1/collection-runs/' + str(uuid4()))
            self.assertEqual(result.status_code, 503)
            self.assertNotIn('SECRET', result.text)

    def test_cli_logging_failure_overrides_success_exit_code(self):
        with patch('app.import_batch.Settings'), patch('app.import_batch.httpx.Client'), \
             patch('app.import_batch.execute_recorded', return_value={'status': 'completed', 'run_record_status': 'finalization_failed'}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--source', 'ecb']), 1)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Opt-in PostgreSQL checks')
class CollectionRunDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(database_url(Settings()), hide_parameters=True)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def test_lifecycle_metadata_only_and_immutable_finished_run(self):
        identity = start_run(self.connection, 'trump', 3)
        self.assertEqual(select_run(self.connection, identity)['status'], 'running')
        finish_run(self.connection, identity, report())
        row = select_run(self.connection, identity)
        self.assertTrue(row['report_complete'])
        self.assertEqual(row['summary']['inserted'], 1)
        self.assertEqual(len(row['items']), 4)
        self.assertTrue(row['items'][-1]['write_outcome_unknown'])
        self.assertNotIn('NEVER_STORE_BODY', json.dumps(row, default=str))
        with self.assertRaises(ValueError):
            finish_run(self.connection, identity, report())

    def test_failed_finalization_is_atomic_and_keeps_running_header(self):
        identity = start_run(self.connection, 'ecb', 3)
        payload = report()
        payload['items'][-1]['status'] = 'INVALID'
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                finish_run(self.connection, identity, payload)
        row = select_run(self.connection, identity)
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['items'], [])
        self.assertFalse(row['report_complete'])

    def test_pagination_tie_break_filters_and_missing_cursor(self):
        ids = [start_run(self.connection, 'ecb', 3) for _ in range(3)]
        stamp = datetime(2099, 1, 1, tzinfo=timezone.utc)
        for identity in ids:
            self.connection.execute(text('UPDATE collection_runs SET started_at=:stamp WHERE run_id=:id'), {'stamp': stamp, 'id': identity})
        first = select_runs(self.connection, 1, source='ecb', status='running')
        self.assertEqual(first[0]['run_id'], max(ids))
        rest = select_runs(self.connection, 100, first[0]['run_id'], 'ecb', 'running')
        self.assertEqual([r['run_id'] for r in rest if r['run_id'] in ids], sorted(ids, reverse=True)[1:])
        self.assertEqual(select_runs(self.connection, 1, uuid4()), [])

    def test_constraints_lifecycle_phase_and_cascade(self):
        identity = start_run(self.connection, 'trump', 3)
        for sql in ("UPDATE collection_runs SET status='completed' WHERE run_id=:id",
                    "UPDATE collection_runs SET batch_limit=0 WHERE run_id=:id"):
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.connection.execute(text(sql), {'id': identity})
        finish_run(self.connection, identity, report())
        self.connection.execute(text('DELETE FROM collection_runs WHERE run_id=:id'), {'id': identity})
        self.assertEqual(self.connection.execute(text('SELECT count(*) FROM collection_run_items WHERE run_id=:id'), {'id': identity}).scalar_one(), 0)

    def test_recorded_cli_wrapper_commits_history_even_when_source_stops(self):
        identity = None
        try:
            payload = dict(status='stopped', saved=False, stop_reason='Synthetic RSS failure', selection={},
                           summary=dict(attempted=0, inserted=0, already_exists=0, needs_review=0,
                                        failed=0, interrupted=0, not_attempted=0))
            runner = MagicMock(return_value=payload)
            result = execute_recorded(Settings(), 'ecb', 3, None, runner)
            identity = result['run_id']
            self.assertEqual(result['run_record_status'], 'saved')
            with TestClient(create_app()) as client:
                response = client.get('/api/v1/collection-runs/' + identity)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['status'], 'stopped')
                self.assertEqual(response.json()['summary']['inserted'], 0)
        finally:
            if identity:
                with self.engine.begin() as connection:
                    connection.execute(text('DELETE FROM collection_runs WHERE run_id=CAST(:id AS uuid)'), {'id': identity})

    def test_cli_runs_real_batch_with_mock_feed_and_commits_queryable_history(self):
        import httpx
        from app.preview_ecb import ECB_FEED_URL
        addresses = []
        def transport(request):
            addresses.append(str(request.url))
            return httpx.Response(200, content=b'<rss><channel></channel></rss>')
        identity = None
        client = httpx.Client(transport=httpx.MockTransport(transport))
        try:
            output = io.StringIO()
            with patch('app.import_batch.httpx.Client', return_value=client), redirect_stdout(output):
                self.assertEqual(main(['--source', 'ecb', '--limit', '3']), 0)
            payload = json.loads(output.getvalue())
            identity = payload['run_id']
            self.assertEqual(payload['status'], 'no_candidates')
            self.assertEqual(payload['run_record_status'], 'saved')
            self.assertEqual(addresses, [ECB_FEED_URL])
            with TestClient(create_app()) as api:
                result = api.get('/api/v1/collection-runs/' + identity)
                self.assertEqual(result.status_code, 200)
                self.assertFalse(result.json()['source_enabled'])
                self.assertEqual(result.json()['summary']['inserted'], 0)
                self.assertEqual(result.json()['items'], [])
        finally:
            client.close()
            if identity:
                with self.engine.begin() as connection:
                    connection.execute(text('DELETE FROM collection_runs WHERE run_id=CAST(:id AS uuid)'), {'id': identity})
