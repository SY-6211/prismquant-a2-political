import hashlib
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.batch_ecb import PERSON_ID, load_context, prepare_candidates, read_existing, select_unseen
from app.core.config import Settings
from app.core.database import create_read_engine
from app.preview_batch import main, run_preview
from app.preview_ecb import ECB_FEED_URL
from test_preview_ecb import feed
from test_preview_ecb_speech import page


def url(number):
    return f'https://www.ecb.europa.eu/press/key/date/2026/html/ecb.sp2610{number:02d}~test.en.html'


def entry(number, **changes):
    return {
        'url': url(number), 'title': f'Christine Lagarde: Synthetic statement {number}',
        'published_at': f'2026-10-{number:02d}T00:00:00Z',
        'published_raw': f'2026-10-{number:02d}T00:00:00Z', **changes,
    }


def xml(count=5):
    return feed(''.join(f'<item><title>{entry(i)["title"]}</title><link>{url(i)}</link>'
                        f'<pubDate>{entry(i)["published_raw"]}</pubDate></item>' for i in range(1, count + 1)))


def existing():
    body = 'Synthetic complete previously saved speech. ' * 10
    return {
        'record_id': uuid4(), 'person_id': PERSON_ID, 'title': 'Saved title',
        'content_type': 'public_statement', 'provenance_type': 'official',
        'verification_status': 'verified_page_attribution', 'body_text': body,
        'content_hash': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'speaker_evidence': 'Statement by Christine Lagarde, President of the ECB',
    }


def context():
    return {'name': 'Test ECB source', 'source_enabled': False, 'monitoring_enabled': False, 'link_enabled': False}


class BatchSelectionTests(unittest.TestCase):
    def test_person_url_filter_duplicate_normalization_and_conflicts(self):
        items = [entry(1), entry(1, url=url(1).replace('/press/', '//press/') + '#intro'),
                 entry(2), entry(2, title='Other speaker: Contradictory same URL'),
                 entry(3, title='Other speaker'), entry(4, url='https://example.invalid/speech')]
        result = prepare_candidates(items)
        self.assertEqual([item['url'] for item in result['candidates']], [url(1)])
        self.assertEqual(result['selection']['feed_items'], 6)
        self.assertEqual(result['selection']['rss_duplicates'], 2)
        self.assertEqual(result['selection']['precheck_needs_review'], 1)
        self.assertEqual(result['precheck_results'][0]['url'], url(2))

    def test_known_dates_descending_unknown_last_and_url_tie_break(self):
        items = [entry(3, published_at=None), entry(2), entry(4, published_at=entry(2)['published_at']), entry(1)]
        prepared = prepare_candidates(items)
        self.assertEqual([item['url'] for item in prepared['candidates']], [url(2), url(4), url(1), url(3)])
        selected = select_unseen(prepared, {}, 3)
        self.assertEqual(len(selected['candidates']), 3)
        self.assertEqual(selected['selection']['not_selected_due_limit'], 1)

    def test_existing_complete_skipped_unread_and_broken_record_needs_review(self):
        prepared = prepare_candidates([entry(i) for i in range(1, 6)])
        rows = {url(5): existing(), url(4): {**existing(), 'content_hash': 'invalid'}}
        selected = select_unseen(prepared, rows, 2)
        self.assertEqual([item['url'] for item in selected['candidates']], [url(3), url(2)])
        self.assertEqual(selected['selection']['skipped_existing'], 1)
        self.assertEqual(selected['selection']['precheck_needs_review'], 1)
        self.assertIn('未重新读取网页', selected['precheck_results'][0]['reason'])
        self.assertEqual(prepared['selection']['selected'], 0)  # Selection does not mutate the input plan.


class BatchPreviewTests(unittest.TestCase):
    def run_batch(self, *, count=5, rows=None, handler=None, pause=None, limit=3):
        requests = []
        pauses = []
        def transport(request):
            requests.append(str(request.url))
            if str(request.url) == ECB_FEED_URL:
                return httpx.Response(200, content=xml(count))
            if handler:
                return handler(request)
            return httpx.Response(200, content=page(body='Synthetic full paragraph. ' * 100))
        with patch('app.preview_batch.load_context', return_value=context()), \
             patch('app.preview_batch.read_existing', return_value=rows or {}):
            with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False) as client:
                result = run_preview(Settings(_env_file=None), limit, client,
                                     pause=pause if pause is not None else pauses.append)
        return result, requests, pauses

    def assert_counts(self, result):
        counts = result['summary']
        self.assertEqual(counts['attempted'], sum(counts[key] for key in ('ready_for_import', 'needs_review', 'failed', 'interrupted')))
        self.assertEqual(result['selection'].get('selected', 0), counts['attempted'] + counts['not_attempted'])
        self.assertEqual(counts['inserted'], 0)
        self.assertFalse(result['saved'])

    def test_default_three_detail_budget_full_extraction_bounded_output_and_spacing(self):
        result, requests, pauses = self.run_batch()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(requests, [ECB_FEED_URL, url(5), url(4), url(3)])
        self.assertEqual(pauses, [1.0, 1.0, 1.0])
        self.assertEqual(result['summary']['ready_for_import'], 3)
        for item in result['items']:
            self.assertNotIn('body_text', item['detail'])
            self.assertGreater(item['detail']['body_characters'], 1200)
            self.assertEqual(len(item['detail']['body_preview']), 1200)
        self.assert_counts(result)

    def test_existing_urls_do_not_get_requested_and_no_new_candidate_is_clear(self):
        result, requests, _ = self.run_batch(rows={url(5): existing()})
        self.assertNotIn(url(5), requests)
        self.assertEqual(requests, [ECB_FEED_URL, url(4), url(3), url(2)])
        self.assertEqual(result['selection']['skipped_existing'], 1)
        all_rows = {url(i): existing() for i in range(1, 6)}
        result, requests, pauses = self.run_batch(rows=all_rows)
        self.assertEqual(result['status'], 'no_new_candidates')
        self.assertEqual(requests, [ECB_FEED_URL])
        self.assertEqual(pauses, [])
        self.assert_counts(result)

    def test_404_continues_but_never_replaces_failed_attempt_with_extra_candidate(self):
        result, requests, _ = self.run_batch(handler=lambda request: httpx.Response(
            404 if str(request.url) == url(5) else 200, content=page()))
        self.assertEqual(result['status'], 'completed_with_issues')
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(result['summary']['ready_for_import'], 2)
        self.assertEqual(requests, [ECB_FEED_URL, url(5), url(4), url(3)])
        self.assert_counts(result)

    def test_auth_rate_limit_redirect_and_server_errors_stop_without_retry(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status):
                result, requests, _ = self.run_batch(handler=lambda request: httpx.Response(
                    status, headers={'Location': 'https://example.invalid/', 'Retry-After': '100'}))
                self.assertEqual(result['status'], 'stopped')
                self.assertEqual(requests, [ECB_FEED_URL, url(5)])
                self.assertEqual(result['summary']['not_attempted'], 2)
                self.assert_counts(result)

    def test_attribution_failure_is_review_and_network_timeout_stops(self):
        result, requests, _ = self.run_batch(handler=lambda request: httpx.Response(
            200, content=page(subtitle='Speech by Another Person')))
        self.assertEqual(result['status'], 'completed_with_issues')
        self.assertEqual(result['summary']['needs_review'], 3)
        self.assert_counts(result)
        def timeout(request):
            raise httpx.ReadTimeout('DO_NOT_EXPOSE', request=request)
        result, requests, _ = self.run_batch(handler=timeout)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(len(requests), 2)
        self.assertNotIn('DO_NOT_EXPOSE', json.dumps(result))
        self.assert_counts(result)

    def test_interrupt_during_pause_or_request_reports_unattempted_items(self):
        def interrupt(_):
            raise KeyboardInterrupt
        result, requests, _ = self.run_batch(pause=interrupt)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['summary']['attempted'], 0)
        self.assertEqual(result['summary']['not_attempted'], 3)
        self.assert_counts(result)
        result, requests, _ = self.run_batch(handler=interrupt)
        self.assertEqual(result['summary']['attempted'], 1)
        self.assertEqual(result['summary']['interrupted'], 1)
        self.assertEqual(result['summary']['not_attempted'], 2)
        self.assert_counts(result)

    def test_empty_feed_and_precheck_problem_do_not_claim_successful_import(self):
        result, requests, _ = self.run_batch(count=0)
        self.assertEqual(result['status'], 'no_candidates')
        self.assert_counts(result)
        result, requests, _ = self.run_batch(count=1, rows={url(1): {**existing(), 'body_text': ''}})
        self.assertEqual(result['status'], 'completed_with_issues')
        self.assertEqual(result['selection']['precheck_needs_review'], 1)
        self.assertEqual(requests, [ECB_FEED_URL])
        self.assert_counts(result)

    def test_bootstrap_failures_are_redacted_and_do_not_fetch_details(self):
        requests = []
        with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, content=xml()))) as client:
            with patch('app.preview_batch.load_context', side_effect=ValueError('PRIVATE')):
                result = run_preview(Settings(_env_file=None), 3, client, pause=lambda _: None)
                self.assertEqual(result['status'], 'stopped')
                self.assertEqual(requests, [])
            with patch('app.preview_batch.load_context', return_value=context()), \
                 patch('app.preview_batch.read_existing', side_effect=OperationalError('private SQL', {}, Exception('PRIVATE'))):
                result = run_preview(Settings(_env_file=None), 3, client, pause=lambda _: None)
                self.assertEqual(result['status'], 'stopped')
                self.assertEqual(len(requests), 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertNotIn('private SQL', json.dumps(result))

    @patch('app.preview_batch.Settings')
    @patch('app.preview_batch.httpx.Client')
    def test_cli_rejects_invalid_arguments_before_any_io(self, client, settings):
        for args in ([], ['--source', 'unknown'], ['--source', 'ecb', '--limit', '0'],
                     ['--source', 'ecb', '--limit', '6'], ['--source', 'ecb', '--limit', 'abc']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)
        settings.assert_not_called()
        client.assert_not_called()

    @patch('app.preview_batch.Settings')
    @patch('app.preview_batch.httpx.Client')
    @patch('app.preview_batch.run_preview')
    def test_cli_default_limit_no_redirects_and_exit_codes(self, run, client, settings):
        for status, code in (('completed', 0), ('no_new_candidates', 0), ('completed_with_issues', 2),
                             ('stopped', 1), ('interrupted', 130)):
            run.return_value = {'status': status}
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--source', 'ecb']), code)
            self.assertEqual(run.call_args.args[1], 3)
            self.assertFalse(client.call_args.kwargs['follow_redirects'])


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class BatchPreviewDatabaseTests(unittest.TestCase):
    def test_context_and_existing_checks_use_read_only_connection_without_changes(self):
        settings = Settings()
        engine = create_read_engine(settings)
        try:
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SHOW transaction_read_only')).scalar_one(), 'on')
                before = [dict(row) for row in connection.execute(text('SELECT * FROM public_records ORDER BY record_id')).mappings()]
            source = load_context(settings)
            self.assertFalse(source['source_enabled'])
            self.assertFalse(source['monitoring_enabled'])
            self.assertFalse(source['link_enabled'])
            speech = next(row for row in before if row['title'] == 'IMFC Statement')
            rows = read_existing(settings, [speech['retrieved_url'], url(1)])
            self.assertEqual(set(rows), {speech['retrieved_url']})
            prepared = prepare_candidates([entry(17, url=speech['retrieved_url'])])
            self.assertEqual(select_unseen(prepared, rows, 3)['selection']['skipped_existing'], 1)
            with engine.connect() as connection:
                after = [dict(row) for row in connection.execute(text('SELECT * FROM public_records ORDER BY record_id')).mappings()]
            self.assertEqual(before, after)
        finally:
            engine.dispose()
