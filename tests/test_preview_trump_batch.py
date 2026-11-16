import copy
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.batch_trump import bounded, existing_complete, load_context, prepare_candidates, read_existing, select_unseen
from app.core.config import Settings
from app.core.database import create_read_engine
from app.import_trump_post import ArchiveDraft, RecordDraft, snapshot_hash
from app.preview_batch import main
from app.preview_trump import TRUMP_FEED_URL
from app.preview_trump_batch import run_preview
from test_import_trump_post import extraction
from test_preview_trump import feed, source_sample
from test_preview_trump_post import candidate, note, page, status


def entry(number=1, **changes):
    return candidate(**{'url': f'https://www.trumpstruth.org/statuses/{number}',
                        'published_raw': '2026-10-17T04:00:00Z', **changes})


def stored():
    payload = extraction()
    record, archive = payload['record'], payload['archive_post']
    archive = {**archive, 'snapshot_hash': snapshot_hash(RecordDraft.model_validate(record), ArchiveDraft.model_validate(archive))}
    return {**record, 'record_id': uuid4(), 'archive_post': archive}


def rss(count=6):
    return feed(''.join(
        f'<item><title>Synthetic {i}</title><link>{entry(i)["url"]}</link>'
        '<pubDate>2026-10-17T04:00:00Z</pubDate><dc:creator>Donald J. Trump</dc:creator>'
        '<truth:originalUrl>https://truthsocial.com/@realDonaldTrump/123</truth:originalUrl>'
        '<archive:postType>original</archive:postType><archive:removed>false</archive:removed></item>'
        for i in range(1, count + 1)))


class TrumpBatchTests(unittest.TestCase):
    def run_batch(self, *, handler=None, rows=None, pause=None, limit=3):
        requests, pauses = [], []
        def transport(request):
            requests.append(str(request.url))
            if str(request.url) == TRUMP_FEED_URL:
                return httpx.Response(200, content=rss())
            if handler:
                return handler(request)
            number = int(request.url.path.split('/')[-1])
            return httpx.Response(200, content=page(status('Own words. ' * 150, identity=number), note(text='Media note. ' * 200)))
        context = {**source_sample(), 'source_enabled': False}
        with patch('app.preview_trump_batch.load_context', return_value=context), \
             patch('app.preview_trump_batch.read_existing', return_value=rows or {}), \
             httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False) as client:
            result = run_preview(Settings(_env_file=None), limit, client, pause=pause or pauses.append)
        counts = result['summary']
        self.assertEqual(counts['attempted'], sum(counts[k] for k in ('ready_for_import', 'skipped_unsupported', 'needs_review', 'failed', 'interrupted')))
        self.assertEqual(result['selection']['selected'], counts['attempted'] + counts['not_attempted'])
        self.assertFalse(result['saved'])
        self.assertEqual(counts['inserted'], 0)
        return result, requests, pauses

    def test_filter_metadata_conflicts_missing_type_wrong_account_and_known_quotes(self):
        items = [entry(1), entry(1, url=entry(1)['url'] + '#section'),
                 entry(2, archive_post_type='quote'), entry(3, archive_post_type='retruth'),
                 entry(4, archive_post_type=None), entry(5, archive_author='Other'),
                 entry(6, original_url='https://truthsocial.com/@other/123'),
                 entry(7), entry(7, archive_removed_raw='true'), entry(8, url=None)]
        result = prepare_candidates(items)
        self.assertEqual([item['url'] for item in result['candidates']], [entry(1)['url']])
        self.assertEqual(result['selection']['out_of_scope'], 2)
        self.assertEqual(result['selection']['precheck_needs_review'], 5)
        self.assertEqual(result['selection']['rss_duplicates'], 2)

    def test_order_unknown_last_ties_url_and_unseen_limit(self):
        prepared = prepare_candidates([entry(3, published_at=None), entry(2), entry(1)])
        self.assertEqual([i['url'] for i in prepared['candidates']], [entry(1)['url'], entry(2)['url'], entry(3)['url']])
        self.assertEqual(len(select_unseen(prepared, {}, 1)['candidates']), 1)

    def test_snapshot_integrity_missing_details_and_hash_corruption(self):
        row = stored()
        self.assertTrue(existing_complete(row))
        for changes in ({'archive_post': None}, {'body_text': 'Changed'}, {'person_id': str(uuid4())}):
            self.assertFalse(existing_complete({**row, **changes}))
        changed = copy.deepcopy(row)
        changed['archive_post']['archive_media_notes'][0]['text'] = 'Changed note'
        self.assertFalse(existing_complete(changed))
        plan = select_unseen(prepare_candidates([entry(1), entry(2)]), {entry(1)['url']: row, entry(2)['url']: {'archive_post': None}}, 3)
        self.assertEqual(plan['selection']['skipped_existing'], 1)
        self.assertEqual(plan['selection']['precheck_needs_review'], 1)
        self.assertEqual(plan['candidates'], [])

    def test_default_three_full_extraction_bounded_separate_output_and_spacing(self):
        result, requests, pauses = self.run_batch()
        self.assertEqual(result['summary']['ready_for_import'], 3)
        self.assertEqual(requests, [TRUMP_FEED_URL, entry(1)['url'], entry(2)['url'], entry(3)['url']])
        self.assertEqual(pauses, [1.0] * 3)
        detail = result['items'][0]['detail']
        self.assertEqual(detail['verification_status'], 'archive_only')
        self.assertEqual(result['provenance_type'], 'third_party_archive')
        self.assertEqual(len(detail['post']['post_text']['text_preview']), 1200)
        self.assertNotIn('Media note', detail['post']['post_text']['text_preview'])
        self.assertNotIn('text', detail['archive_media_notes'][0])
        self.assertTrue(all('truthsocial.com' not in address for address in requests))
        result, requests, _ = self.run_batch(limit=5)
        self.assertEqual(len(requests), 6)

    def test_media_only_inline_quotes_and_wrong_author_classification(self):
        def handler(request):
            n = int(request.url.path.split('/')[-1])
            if n == 1:
                post = status('', identity=n, media='<div class="status-attachment--image"><img></div>')
            elif n == 2:
                post = status('Own words.<blockquote>Quoted words</blockquote>', identity=n)
            else:
                post = status('Wrong author', identity=n, author='Other')
            return httpx.Response(200, content=page(post, note()))
        result, _, _ = self.run_batch(handler=handler)
        self.assertEqual(result['summary']['skipped_unsupported'], 2)
        self.assertEqual(result['summary']['needs_review'], 1)
        self.assertEqual(result['summary']['ready_for_import'], 0)

    def test_preview_omission_counts_do_not_claim_incomplete_extraction(self):
        result = bounded({'quoted_posts': [{'post_text': {'text': 'Long' * 500}}] * 8, 'quoted_posts_omitted': 0})
        self.assertEqual(len(result['quoted_posts']), 5)
        self.assertEqual(result['quoted_posts_preview_omitted'], 3)
        self.assertEqual(result['quoted_posts_omitted'], 0)

    def test_http_404_budget_and_source_errors_stop(self):
        result, requests, _ = self.run_batch(handler=lambda request: httpx.Response(404))
        self.assertEqual(result['summary']['failed'], 3)
        self.assertEqual(len(requests), 4)
        for code in (302, 401, 403, 429, 500):
            result, requests, _ = self.run_batch(handler=lambda _: httpx.Response(code))
            self.assertEqual(result['status'], 'stopped')
            self.assertEqual(len(requests), 2)
            self.assertEqual(result['summary']['not_attempted'], 2)

    def test_network_timeout_and_interrupt_counts(self):
        def timeout(request):
            raise httpx.ReadTimeout('PRIVATE', request=request)
        result, _, _ = self.run_batch(handler=timeout)
        self.assertEqual(result['status'], 'stopped')
        self.assertNotIn('PRIVATE', json.dumps(result))
        def interrupt(_):
            raise KeyboardInterrupt
        result, _, _ = self.run_batch(pause=interrupt)
        self.assertEqual(result['summary']['attempted'], 0)
        self.assertEqual(result['summary']['not_attempted'], 3)
        result, _, _ = self.run_batch(handler=interrupt)
        self.assertEqual(result['summary']['interrupted'], 1)

    def test_all_existing_skips_without_requests_and_corrupt_precheck_is_issue(self):
        result, requests, _ = self.run_batch(rows={entry(i)['url']: stored() for i in range(1, 7)})
        self.assertEqual(result['status'], 'no_new_candidates')
        self.assertEqual(requests, [TRUMP_FEED_URL])
        result, requests, _ = self.run_batch(rows={entry(i)['url']: {} for i in range(1, 7)})
        self.assertEqual(result['status'], 'completed_with_issues')
        self.assertEqual(result['selection']['precheck_needs_review'], 6)
        self.assertEqual(requests, [TRUMP_FEED_URL])

    def test_db_bootstrap_failure_is_redacted_and_never_fetches(self):
        with patch('app.preview_trump_batch.load_context', side_effect=OperationalError('PRIVATE', {}, Exception('SECRET'))), \
             httpx.Client(transport=httpx.MockTransport(lambda _: self.fail('Must not fetch'))) as client:
            result = run_preview(Settings(_env_file=None), 3, client)
        self.assertEqual(result['status'], 'stopped')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_cli_dispatches_trump_and_preserves_limit(self):
        with patch('app.preview_batch.Settings'), patch('app.preview_batch.httpx.Client'), \
             patch('app.preview_trump_batch.run_preview', return_value={'status': 'completed'}) as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--source', 'trump']), 0)
            self.assertEqual(run.call_args.args[1], 3)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Opt-in PostgreSQL checks')
class TrumpBatchDatabaseTests(unittest.TestCase):
    def test_readonly_context_existing_snapshot_and_no_changes(self):
        settings = Settings()
        engine = create_read_engine(settings)
        try:
            with engine.connect() as connection:
                before = [dict(row) for row in connection.execute(text('SELECT * FROM public_records ORDER BY record_id')).mappings()]
                archive_before = [dict(row) for row in connection.execute(text('SELECT * FROM archive_post_details ORDER BY record_id')).mappings()]
            context = load_context(settings)
            self.assertEqual((context['source_enabled'], context['monitoring_enabled'], context['link_enabled']), (False, False, False))
            target = 'https://www.trumpstruth.org/statuses/42396'
            rows = read_existing(settings, [target])
            self.assertTrue(existing_complete(rows[target]))
            self.assertEqual(select_unseen(prepare_candidates([entry(url=target)]), rows, 3)['selection']['skipped_existing'], 1)
            with engine.connect() as connection:
                self.assertEqual(before, [dict(row) for row in connection.execute(text('SELECT * FROM public_records ORDER BY record_id')).mappings()])
                self.assertEqual(archive_before, [dict(row) for row in connection.execute(text('SELECT * FROM archive_post_details ORDER BY record_id')).mappings()])
        finally:
            engine.dispose()
