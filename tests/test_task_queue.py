import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.main import create_app
from app.task_queue import main, work
from app.task_types import Outcome, normalized_payload, outcome_for_report, payload_key


def history_payload(number=1):
    return {'source': 'ecb', 'since': '2026-10-01', 'until': '2026-11-07', 'entry': {
        'url': f'https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp261101~abc{number}.en.html',
        'title': 'Christine Lagarde: Synthetic speech', 'published_at': '2026-11-01T08:00:00Z',
        'published_raw': '2026-11-01T08:00:00Z', 'metadata_origin': 'ecb_history_index'}}


class TaskContractTests(unittest.TestCase):
    def test_kind_fixes_lane_and_priority_without_accepting_arbitrary_handlers(self):
        raw, lane, priority = normalized_payload('collect_source', {'source': 'ecb', 'limit': 3})
        self.assertEqual((lane, priority), ('raw', 100))
        self.assertEqual(normalized_payload('history_item', history_payload())[1:], ('history', 10))
        for kind, value in (('ai_analyze', {}), ('media_parse', {}), ('exec', {}),
                            ('collect_source', {'source': 'x'}), ('collect_source', {'source': 'ecb', 'limit': 99}),
                            ('collect_source', {'source': 'ecb', 'limit': True}),
                            ('collect_source', {'source': 'ecb', 'url': 'https://evil.invalid/'})):
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                normalized_payload(kind, value)

    def test_history_rejects_wrong_host_date_origin_and_unsupported_scope(self):
        for field, value in (('url', 'https://evil.invalid/a'), ('metadata_origin', 'rss'), ('published_at', '2020-01-01T00:00:00Z')):
            payload = history_payload()
            payload['entry'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                normalized_payload('history_item', payload)

    def test_history_entry_drops_unknown_options_and_identity_hash_is_stable(self):
        value = history_payload()
        value['entry']['headers'] = {'Authorization': 'DO_NOT_STORE'}
        normalized, _, _ = normalized_payload('history_item', value)
        self.assertNotIn('headers', normalized['entry'])
        key = payload_key('history_item', normalized, 'request-1')
        self.assertEqual(key, payload_key('history_item', dict(reversed(list(normalized.items()))), 'request-1'))
        self.assertNotEqual(key, payload_key('history_item', normalized, 'request-2'))

    def test_only_transient_failures_retry_and_unknown_commits_require_review(self):
        for report in ({'status': 'stopped', 'failure_kind': 'network'},
                       {'status': 'stopped', 'failure_kind': 'http', 'http_status': 429},
                       {'status': 'stopped', 'failure_kind': 'http', 'http_status': 503}):
            self.assertEqual(outcome_for_report(report).disposition, 'retry')
        for report in ({'status': 'stopped', 'failure_kind': 'http', 'http_status': 403},
                       {'status': 'completed', 'write_outcome_unknown': True},
                       {'status': 'completed', 'items': [{'write_outcome_unknown': True}]},
                       {'status': 'completed', 'run_record_status': 'finalization_failed'},
                       {'status': 'paused'}, {'status': 'blocked_unfinished_run'},
                       {'status': 'stopped', 'failure_kind': 'http', 'http_status': 429, 'retry_after_invalid': True}):
            self.assertEqual(outcome_for_report(report).disposition, 'needs_review')

    def test_disabled_cancelled_busy_deferred_and_content_review_does_not_fake_failure(self):
        self.assertEqual(outcome_for_report({'status': 'disabled'}).disposition, 'cancelled')
        for status in ('skipped_busy', 'not_due', 'backoff'):
            self.assertEqual(outcome_for_report({'status': status}).disposition, 'deferred')
        self.assertEqual(outcome_for_report({'status': 'completed_with_issues', 'summary': {'needs_review': 1}}).disposition, 'succeeded')
        self.assertEqual(outcome_for_report({'status': 'completed_with_issues', 'summary': {'failed': 1}}).disposition, 'needs_review')

    def test_retry_after_is_retained_and_naive_timestamps_require_review(self):
        value = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        outcome = outcome_for_report({'status': 'backoff', 'retry_not_before': value})
        self.assertEqual(outcome.not_before.isoformat(), value)
        self.assertEqual(outcome_for_report({'status': 'backoff', 'retry_not_before': '2026-11-07T12:00:00'}).disposition, 'needs_review')

    @patch('app.task_queue.claim', return_value=None)
    def test_idle_worker_and_bounded_parameters_do_not_call_handler(self, claim):
        handler = MagicMock()
        result = work(None, engine=MagicMock(), handler=handler)
        self.assertTrue(result['idle'])
        handler.assert_not_called()
        for number in (0, 101, True):
            with self.assertRaises(ValueError):
                work(None, max_tasks=number, engine=MagicMock())

    @patch('app.task_queue.claim')
    def test_keyboard_interrupt_closes_session_without_claiming_success(self, claim):
        lease = MagicMock()
        claim.return_value = lease
        with self.assertRaises(KeyboardInterrupt):
            work(None, engine=MagicMock(), handler=MagicMock(side_effect=KeyboardInterrupt))
        lease.close.assert_called_once()
        lease.finish.assert_not_called()


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment='test')))
        self.addCleanup(self.client.close)

    @patch('app.api.tasks.read_query', return_value=[])
    def test_empty_queue_is_readonly_and_filters_are_validated(self, query):
        self.assertEqual(self.client.get('/api/v1/task-queue').json(), {'items': []})
        self.assertEqual(self.client.get('/api/v1/collection-tasks').json(), {'items': [], 'next_cursor': None})
        query.reset_mock()
        for tail in ('?limit=101', '?limit=0', '?lane=bad', '?status=bad', '?cursor=bad'):
            self.assertEqual(self.client.get('/api/v1/collection-tasks' + tail).status_code, 422)
        query.assert_not_called()
        self.assertEqual(self.client.post('/api/v1/collection-tasks', json={}).status_code, 405)

    @patch('app.api.tasks.read_query', return_value=None)
    def test_missing_task_and_attempts_are_404(self, query):
        for suffix in ('', '/attempts'):
            self.assertEqual(self.client.get(f'/api/v1/collection-tasks/{uuid4()}' + suffix).status_code, 404)

    @patch('app.api.tasks.read_query', side_effect=OperationalError('private', {}, Exception('SECRET')))
    def test_database_errors_are_redacted(self, query):
        response = self.client.get('/api/v1/task-queue')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('SECRET', response.text)
