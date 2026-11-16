import copy
import io
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import database_url
from app.import_batch import main
from app.import_trump_batch import run_import, save_one
from app.import_trump_post import save_post
from app.preview_trump import TRUMP_FEED_URL
from test_import_trump_post import add_fixture, extraction
from test_preview_trump import source_sample
from test_preview_trump_batch import entry, rss, stored
from test_preview_trump_post import note, page, status


class TrumpBatchImportTests(unittest.TestCase):
    def run_batch(self, *, handler=None, saver=None, rows=None, pause=None, limit=3):
        requests, payloads, pauses = [], [], []
        def transport(request):
            requests.append(str(request.url))
            if str(request.url) == TRUMP_FEED_URL:
                return httpx.Response(200, content=rss())
            if handler:
                return handler(request)
            n = int(request.url.path.split('/')[-1])
            return httpx.Response(200, content=page(status('Own full text. ' * 150, identity=n), note(text='Separate note. ' * 200)))
        def save(settings, payload):
            payloads.append(payload)
            return saver(settings, payload) if saver else {
                'action': 'inserted', 'record_id': 'test-id', 'message': 'Saved',
                'body_characters': len(payload['record']['body_text']),
                'media_note_count': len(payload['archive_post']['archive_media_notes'])}
        with patch('app.import_trump_batch.load_context', return_value={**source_sample(), 'source_enabled': False}), \
             patch('app.import_trump_batch.read_existing', return_value=rows or {}), \
             patch('app.import_trump_batch.save_one', side_effect=save), \
             httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False) as client:
            result = run_import(Settings(_env_file=None), limit, client, pause=pause or pauses.append)
        counts = result['summary']
        self.assertEqual(counts['attempted'], sum(counts[k] for k in ('inserted', 'already_exists', 'needs_review', 'skipped_unsupported', 'failed', 'interrupted')))
        self.assertEqual(result['selection']['selected'], counts['attempted'] + counts['not_attempted'])
        self.assertEqual(result['saved'], counts['inserted'] > 0)
        return result, requests, payloads, pauses

    def test_full_sections_go_to_writer_and_terminal_report_is_short(self):
        result, requests, payloads, pauses = self.run_batch()
        self.assertEqual(result['summary']['inserted'], 3)
        self.assertEqual(len(requests), 4)
        self.assertEqual(pauses, [1.0] * 3)
        self.assertEqual(result['provenance_type'], 'third_party_archive')
        for payload in payloads:
            self.assertGreater(len(payload['record']['body_text']), 1200)
            self.assertGreater(len(payload['archive_post']['archive_media_notes'][0]['text']), 1200)
            self.assertNotIn('Separate note', payload['record']['body_text'])
            self.assertEqual(payload['record']['verification_status'], 'archive_only')
        for item in result['items']:
            self.assertNotIn('detail', item)
            self.assertNotIn('rss_item', item)
        self.assertNotIn('Own full text.', json.dumps(result))
        self.assertFalse(result['source_enabled'])

    def test_unsupported_and_unverified_posts_never_call_writer(self):
        def handler(request):
            n = int(request.url.path.split('/')[-1])
            if n == 1:
                post = status('', identity=n, media='<div class="status-attachment--image"><img></div>')
            elif n == 2:
                post = status('Own words.<blockquote>Quoted</blockquote>', identity=n)
            else:
                post = status('Wrong author', identity=n, author='Other')
            return httpx.Response(200, content=page(post, note()))
        result, _, payloads, _ = self.run_batch(handler=handler)
        self.assertEqual(payloads, [])
        self.assertEqual(result['summary']['skipped_unsupported'], 2)
        self.assertEqual(result['summary']['needs_review'], 1)

    def test_existing_and_corrupt_details_are_prechecked_without_page_requests(self):
        rows = {entry(i)['url']: stored() for i in range(1, 7)}
        result, requests, payloads, _ = self.run_batch(rows=rows)
        self.assertEqual(result['status'], 'no_new_candidates')
        self.assertEqual(requests, [TRUMP_FEED_URL])
        self.assertEqual(payloads, [])
        rows[entry(1)['url']]['archive_post'] = None
        result, requests, _, _ = self.run_batch(rows=rows)
        self.assertEqual(result['status'], 'completed_with_issues')
        self.assertEqual(result['selection']['precheck_needs_review'], 1)
        self.assertEqual(requests, [TRUMP_FEED_URL])

    def test_racing_duplicate_and_changed_snapshot_have_distinct_results(self):
        actions = iter(('inserted', 'already_exists', 'needs_review'))
        def save(*_):
            return {'action': next(actions), 'record_id': 'id', 'message': 'Checked', 'body_characters': 100, 'media_note_count': 1}
        result, _, _, _ = self.run_batch(saver=save)
        for key in ('inserted', 'already_exists', 'needs_review'):
            self.assertEqual(result['summary'][key], 1)
        self.assertEqual(result['status'], 'completed_with_issues')

    def test_database_failure_preserves_prior_commit_stops_and_redacts(self):
        calls = []
        def save(*_):
            calls.append(1)
            if len(calls) == 2:
                raise OperationalError('PRIVATE SQL', {}, Exception('SECRET'))
            return {'action': 'inserted', 'record_id': 'id', 'message': 'Saved', 'body_characters': 100, 'media_note_count': 1}
        result, requests, _, _ = self.run_batch(saver=save)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['not_attempted'], 1)
        self.assertTrue(result['items'][1]['write_outcome_unknown'])
        self.assertNotIn('PRIVATE SQL', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len(requests), 3)

    def test_config_recheck_failure_stops_before_next_candidate(self):
        result, requests, _, _ = self.run_batch(saver=lambda *_: (_ for _ in ()).throw(ValueError('SECRET')))
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['summary']['needs_review'], 1)
        self.assertEqual(len(requests), 2)

    def test_http_failures_do_not_expand_candidate_budget(self):
        result, requests, _, _ = self.run_batch(handler=lambda _: httpx.Response(404))
        self.assertEqual(result['summary']['failed'], 3)
        self.assertEqual(len(requests), 4)
        for code in (302, 403, 429, 500):
            result, requests, payloads, _ = self.run_batch(handler=lambda _: httpx.Response(code))
            self.assertEqual(result['status'], 'stopped')
            self.assertEqual(len(requests), 2)
            self.assertEqual(payloads, [])

    def test_timeout_and_interrupt_after_commit_keep_correct_counts(self):
        def timeout(request):
            raise httpx.ReadTimeout('SECRET', request=request)
        result, _, _, _ = self.run_batch(handler=timeout)
        self.assertEqual(result['status'], 'stopped')
        self.assertNotIn('SECRET', json.dumps(result))
        pauses = []
        def pause(_):
            pauses.append(1)
            if len(pauses) == 2:
                raise KeyboardInterrupt
        result, _, _, _ = self.run_batch(pause=pause)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['not_attempted'], 2)

    def test_interrupt_during_write_reports_uncertain_current_result(self):
        def interrupt(*_):
            raise KeyboardInterrupt
        result, _, _, _ = self.run_batch(saver=interrupt)
        self.assertEqual(result['summary']['interrupted'], 1)
        self.assertTrue(result['items'][0]['write_outcome_unknown'])
        self.assertFalse(result['saved'])

    def test_cli_selects_trump_and_invalid_limit_is_rejected_before_io(self):
        with patch('app.import_batch.Settings') as settings, redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(['--source', 'trump', '--limit', '6'])
        settings.assert_not_called()
        with patch('app.import_batch.execute_recorded', side_effect=lambda settings, source, limit, client, runner: runner(settings, limit, client)), patch('app.import_batch.Settings'), patch('app.import_batch.httpx.Client'), \
             patch('app.import_trump_batch.run_import', return_value={'status': 'completed_with_issues'}) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--source', 'trump']), 2)
            self.assertEqual(run.call_args.args[1], 3)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Opt-in PostgreSQL checks')
class TrumpBatchCommitTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.engine = create_engine(database_url(self.settings), hide_parameters=True)
        self.source, self.person = uuid4(), uuid4()
        with self.engine.begin() as connection:
            add_fixture(connection, self.source, self.person)

    def tearDown(self):
        try:
            with self.engine.begin() as connection:
                connection.execute(text('DELETE FROM archive_post_details WHERE record_id IN (SELECT record_id FROM public_records WHERE source_id=:s)'), {'s': self.source})
                connection.execute(text('DELETE FROM public_records WHERE source_id=:s'), {'s': self.source})
                connection.execute(text('DELETE FROM person_sources WHERE source_id=:s'), {'s': self.source})
                connection.execute(text('DELETE FROM sources WHERE source_id=:s'), {'s': self.source})
                connection.execute(text('DELETE FROM people WHERE person_id=:p'), {'p': self.person})
        finally:
            self.engine.dispose()

    def remap(self, connection, payload, seen):
        value = copy.deepcopy(payload)
        value['record'].update(source_id=str(self.source), person_id=str(self.person))
        return save_post(connection, value, seen, source_id=self.source, person_id=self.person)

    def counts(self):
        with self.engine.connect() as connection:
            main = connection.execute(text('SELECT count(*) FROM public_records WHERE source_id=:s'), {'s': self.source}).scalar_one()
            child = connection.execute(text('SELECT count(*) FROM archive_post_details a JOIN public_records r USING(record_id) WHERE r.source_id=:s'), {'s': self.source}).scalar_one()
        return main, child

    def test_first_pair_commits_second_child_failure_rolls_back_pair_and_stops(self):
        from app.import_trump_post import _insert_archive
        calls = []
        def child(connection, identity, archive, digest):
            calls.append(1)
            if len(calls) == 2:
                connection.execute(text('SELECT 1/0'))
            return _insert_archive(connection, identity, archive, digest)
        def transport(request):
            if str(request.url) == TRUMP_FEED_URL:
                return httpx.Response(200, content=rss())
            n = int(request.url.path.split('/')[-1])
            return httpx.Response(200, content=page(status('Synthetic words.', identity=n), note()))
        with patch('app.import_trump_batch.load_context', return_value={**source_sample(), 'source_enabled': False}), \
             patch('app.import_trump_batch.read_existing', return_value={}), \
             patch('app.import_trump_batch.save_post', side_effect=self.remap), \
             patch('app.import_trump_post._insert_archive', side_effect=child), \
             httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result = run_import(self.settings, 3, client, pause=lambda _: None)
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(result['summary']['not_attempted'], 1)
        self.assertEqual(self.counts(), (1, 1))

    def test_concurrent_repeated_and_changed_payloads_preserve_one_pair(self):
        payload = extraction()
        with patch('app.import_trump_batch.save_post', side_effect=self.remap):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: save_one(self.settings, payload), range(3)))
            self.assertEqual(sorted(r['action'] for r in results), ['already_exists', 'already_exists', 'inserted'])
            self.assertEqual(len({r['record_id'] for r in results}), 1)
            self.assertEqual(self.counts(), (1, 1))
            changed = copy.deepcopy(payload)
            changed['archive_post']['archive_media_notes'][0]['text'] = 'Changed note'
            self.assertEqual(save_one(self.settings, changed)['action'], 'needs_review')
            self.assertEqual(save_one(self.settings, payload)['action'], 'already_exists')
            self.assertEqual(self.counts(), (1, 1))
