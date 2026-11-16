import io
import os
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
from sqlalchemy import text

from app.collection_failures import recovery_policy, retry_time
from app.collection_runs import start_run
from app.import_batch import run_import
from app.recover_collection import inspect_state, main, recover
from app.scheduler import run_source
from test_preview_batch import context
import test_scheduler as scheduler_tests


class RecoveryTests(unittest.TestCase):
    def test_retry_after_seconds_http_date_invalid_and_past(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.assertEqual(datetime.fromisoformat(retry_time('3600', now)), now + timedelta(hours=1))
        self.assertEqual(datetime.fromisoformat(retry_time('Sun, 18 Oct 2026 02:00:00 GMT', now)), now + timedelta(hours=2))
        self.assertIsNone(retry_time('invalid', now))
        self.assertIsNone(retry_time('9' * 10000, now))
        self.assertEqual(datetime.fromisoformat(retry_time('Sat, 17 Oct 2026 00:00:00 GMT', now)), now)

    def test_backoff_grows_respects_interval_and_retry_after(self):
        now = datetime.now(timezone.utc)
        for count in range(12):
            result = recovery_policy({'failure_kind': 'network'}, count, 60, now)
            self.assertEqual(result[1], now + timedelta(seconds=min(21600, 60 * 2 ** min(count, 9))))
        result = recovery_policy({'failure_kind': 'http', 'http_status': 429, 'retry_not_before': (now + timedelta(days=1)).isoformat()}, 0, 300, now)
        self.assertEqual(result[1], now + timedelta(days=1))

    def test_uncertain_permissions_interrupts_pause_and_healthy_reset(self):
        now = datetime.now(timezone.utc)
        for payload in ({'status': 'stopped', 'http_status': 403}, {'status': 'interrupted'},
                        {'items': [{'write_outcome_unknown': True}]}, {'run_record_status': 'finalization_failed'},
                        {'retry_after_invalid': True}):
            self.assertIsNotNone(recovery_policy(payload, 2, 300, now)[2])
        self.assertEqual(recovery_policy({'status': 'completed_with_issues', 'http_status': 404, 'failure_kind': 'http'}, 4, 300, now), (0, None, None))

    def test_http_response_metadata_reaches_policy_without_raw_header(self):
        with patch('app.import_batch.load_context', return_value=context()), httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={'Retry-After': '3600'}))) as client:
            report = run_import(None, 3, client)
        self.assertEqual(report['http_status'], 429)
        self.assertIsNotNone(report['retry_not_before'])
        self.assertIsNone(recovery_policy(report, 0, 300, datetime.now(timezone.utc))[2])

    def test_recovery_actions_require_review_note_before_io(self):
        with patch('app.recover_collection.Settings') as settings:
            for args in (['--source','ecb','--resume'], ['--source','ecb','--resume','--note',' ']):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(args)
            settings.assert_not_called()


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Opt-in PostgreSQL checks')
class RecoveryDatabaseTests(unittest.TestCase):
    setUp = scheduler_tests.SchedulerDatabaseTests.setUp
    tearDown = scheduler_tests.SchedulerDatabaseTests.tearDown

    def test_backoff_is_persisted_and_blocks_next_attempt_without_client(self):
        factory = lambda: httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={'Retry-After': '3600'})))
        with patch('app.import_batch.load_context', return_value=context()):
            result = run_source(self.settings, 'ecb', client_factory=factory)
        self.assertEqual(result['status'], 'stopped')
        result = run_source(self.settings, 'ecb', client_factory=lambda: self.fail('Must not retry'))
        self.assertEqual(result['status'], 'backoff')
        state = inspect_state(self.settings, 'ecb')['schedule']
        self.assertEqual(state['failure_count'], 1)
        self.assertGreater(state['retry_not_before'], datetime.now(timezone.utc) + timedelta(minutes=55))

    def test_permission_pause_resume_preserves_deadline_and_switches(self):
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO collection_schedule(source_id,source,last_attempt_at,pause_reason,retry_not_before) VALUES (:sid,'ecb',clock_timestamp(),'Review',clock_timestamp()+interval '1 day')"), {'sid': self.sid})
        self.assertEqual(run_source(self.settings, 'ecb')['status'], 'paused')
        result = recover(self.settings, 'ecb', resume=True, note='Reviewed source configuration')
        self.assertTrue(result['resumed'])
        state = inspect_state(self.settings, 'ecb')['schedule']
        self.assertIsNone(state['pause_reason'])
        self.assertIsNotNone(state['retry_not_before'])
        self.assertEqual(run_source(self.settings, 'ecb')['status'], 'backoff')

    def test_review_unfinished_run_retains_unknown_counts_and_records_note(self):
        with self.engine.begin() as connection:
            identity = start_run(connection, 'ecb', 3, 'automatic_batch_import')
        with self.assertRaises(ValueError):
            recover(self.settings, 'ecb', resume=True, note='Reviewed')
        result = recover(self.settings, 'ecb', run_id=identity, note='Reviewed committed content separately')
        self.assertTrue(result['changes_saved'])
        with self.engine.connect() as connection:
            row = connection.execute(text('SELECT status,report_complete,recovery_note,recovered_at FROM collection_runs WHERE run_id=:id'), {'id': identity}).one()
            self.assertEqual(row.status, 'stopped')
            self.assertFalse(row.report_complete)
            self.assertIsNotNone(row.recovered_at)
            self.assertEqual(row.recovery_note, 'Reviewed committed content separately')
