import io
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import database_url
from app.import_batch import main, run_import, save_one
from app.import_ecb_speech import save_speech
from app.preview_ecb import ECB_FEED_URL
from test_preview_batch import context, existing, page, url, xml


class BatchImportTests(unittest.TestCase):
    def run_batch(self, *, handler=None, saver=None, rows=None, pause=None):
        requests, bodies = [], []
        def transport(request):
            address = str(request.url)
            requests.append(address)
            if address == ECB_FEED_URL:
                return httpx.Response(200, content=xml())
            return handler(request) if handler else httpx.Response(200, content=page(body='Full synthetic paragraph. ' * 100))
        def save(settings, detail, entry):
            bodies.append(detail['body_text'])
            return saver(settings, detail, entry) if saver else {
                'action': 'inserted', 'record_id': 'synthetic', 'message': 'Saved'}
        with patch('app.import_batch.load_context', return_value=context()), \
             patch('app.import_batch.read_existing', return_value=rows or {}), \
             patch('app.import_batch.save_one', side_effect=save), \
             httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False) as client:
            result = run_import(Settings(_env_file=None), 3, client, pause=pause or (lambda _: None))
        counts = result['summary']
        self.assertEqual(counts['attempted'], sum(counts[k] for k in (
            'inserted', 'already_exists', 'needs_review', 'failed', 'interrupted')))
        self.assertEqual(result['selection']['selected'], counts['attempted'] + counts['not_attempted'])
        self.assertEqual(result['saved'], counts['inserted'] > 0)
        return result, requests, bodies

    def test_full_bodies_saved_only_after_attribution_checks(self):
        result, requests, bodies = self.run_batch()
        self.assertEqual(result['summary']['inserted'], 3)
        self.assertEqual(requests, [ECB_FEED_URL, url(5), url(4), url(3)])
        self.assertTrue(all(len(body) > 1200 for body in bodies))
        self.assertTrue(all('body_text' not in item['detail'] for item in result['items']))
        self.assertFalse(result['source_enabled'])
        result, _, bodies = self.run_batch(handler=lambda _: httpx.Response(200, content=page(subtitle='Speech by Other Person')))
        self.assertEqual(result['summary']['needs_review'], 3)
        self.assertEqual(bodies, [])

    def test_existing_skips_no_new_and_concurrent_duplicate_review_counts(self):
        result, requests, _ = self.run_batch(rows={url(i): existing() for i in range(1, 6)})
        self.assertEqual(result['status'], 'no_new_candidates')
        self.assertEqual(requests, [ECB_FEED_URL])
        actions = iter(('inserted', 'already_exists', 'needs_review'))
        result, _, _ = self.run_batch(saver=lambda *_: {'action': next(actions), 'record_id': 'id', 'message': 'Checked'})
        self.assertEqual(result['status'], 'completed_with_issues')
        for key in ('inserted', 'already_exists', 'needs_review'):
            self.assertEqual(result['summary'][key], 1)

    def test_partial_success_then_database_failure_stops_and_redacts(self):
        calls = []
        def save(*_):
            calls.append(1)
            if len(calls) == 2:
                raise OperationalError('PRIVATE SQL', {}, Exception('PASSWORD'))
            return {'action': 'inserted', 'record_id': 'committed', 'message': 'Saved'}
        result, requests, _ = self.run_batch(saver=save)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(result['summary']['not_attempted'], 1)
        self.assertTrue(result['items'][1]['write_outcome_unknown'])
        self.assertNotIn('PASSWORD', json.dumps(result))
        self.assertNotIn('PRIVATE SQL', json.dumps(result))
        self.assertEqual(len(requests), 3)

    def test_http_failure_budget_and_source_stop(self):
        result, requests, _ = self.run_batch(handler=lambda request: httpx.Response(
            404 if str(request.url) == url(5) else 200, content=page()))
        self.assertEqual(result['summary']['inserted'], 2)
        self.assertEqual(len(requests), 4)
        for status in (302, 403, 429, 500):
            result, requests, bodies = self.run_batch(handler=lambda _: httpx.Response(status))
            self.assertEqual(result['status'], 'stopped')
            self.assertEqual(len(requests), 2)
            self.assertEqual(bodies, [])

    def test_interrupt_after_commit_preserves_report(self):
        pauses = []
        def pause(_):
            pauses.append(1)
            if len(pauses) == 2:
                raise KeyboardInterrupt
        result, requests, _ = self.run_batch(pause=pause)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['not_attempted'], 2)
        self.assertEqual(len(requests), 2)

    def test_config_recheck_failure_stops_and_retains_prior_commit(self):
        calls = []
        def save(*_):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('PRIVATE')
            return {'action': 'inserted', 'record_id': 'id', 'message': 'Saved'}
        result, _, _ = self.run_batch(saver=save)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['needs_review'], 1)

    @patch('app.import_batch.Settings')
    def test_cli_arguments_before_io_and_exit_codes(self, settings):
        for args in ([], ['--source', 'unknown'], ['--source', 'ecb', '--limit', '6']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)
        settings.assert_not_called()
        with patch('app.import_batch.httpx.Client'), patch('app.import_batch.execute_recorded', side_effect=lambda settings, source, limit, client, runner: runner(settings, limit, client)), patch('app.import_batch.run_import') as run:
            for status, code in (('completed', 0), ('no_new_candidates', 0), ('completed_with_issues', 2), ('stopped', 1), ('interrupted', 130)):
                run.return_value = {'status': status}
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(['--source', 'ecb']), code)
                self.assertEqual(run.call_args.args[1], 3)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Opt-in PostgreSQL checks')
class BatchCommitTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.engine = create_engine(database_url(self.settings), hide_parameters=True)
        with self.engine.begin() as connection:
            self.person = connection.execute(text("INSERT INTO people(name_original) VALUES ('Synthetic batch test') RETURNING person_id")).scalar_one()
            self.source = connection.execute(text("INSERT INTO sources(name,publisher_name,provenance_type,feed_url) VALUES ('Synthetic batch test','Test','official',:url) RETURNING source_id"), {'url': ECB_FEED_URL}).scalar_one()
            connection.execute(text('INSERT INTO person_sources(source_id,person_id) VALUES (:s,:p)'), {'s': self.source, 'p': self.person})
        self.original_save = save_speech

    def tearDown(self):
        try:
            with self.engine.begin() as connection:
                connection.execute(text('DELETE FROM public_records WHERE source_id=:s'), {'s': self.source})
                connection.execute(text('DELETE FROM person_sources WHERE source_id=:s'), {'s': self.source})
                connection.execute(text('DELETE FROM sources WHERE source_id=:s'), {'s': self.source})
                connection.execute(text('DELETE FROM people WHERE person_id=:p'), {'p': self.person})
        finally:
            self.engine.dispose()

    def remap(self, connection, detail, entry, seen):
        return self.original_save(connection, {**detail, 'person_id': str(self.person)}, entry, seen,
                                  source_id=self.source, person_id=self.person)

    def test_each_article_commits_and_failed_second_insert_rolls_back_only_itself(self):
        calls = []
        def writer(connection, detail, entry, seen):
            result = self.remap(connection, detail, entry, seen)
            calls.append(1)
            if len(calls) == 2:
                connection.execute(text('SELECT 1/0'))
            return result
        def transport(request):
            return httpx.Response(200, content=xml() if str(request.url) == ECB_FEED_URL else page())
        with patch('app.import_batch.load_context', return_value=context()), \
             patch('app.import_batch.read_existing', return_value={}), \
             patch('app.import_batch.save_speech', side_effect=writer), \
             httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result = run_import(self.settings, 3, client, pause=lambda _: None)
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(result['summary']['failed'], 1)
        with self.engine.connect() as connection:
            rows = connection.execute(text('SELECT retrieved_url FROM public_records WHERE source_id=:s'), {'s': self.source}).scalars().all()
        self.assertEqual(rows, [url(5)])

    def test_concurrent_writers_commit_one_record_and_preserve_identity(self):
        from app.preview_ecb_speech import parse_speech
        from test_preview_batch import entry
        detail = parse_speech(page(), url(5), include_body=True)
        with patch('app.import_batch.save_speech', side_effect=self.remap):
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda _: save_one(self.settings, detail, entry(5)), range(3)))
        self.assertEqual(sorted(r['action'] for r in results), ['already_exists', 'already_exists', 'inserted'])
        self.assertEqual(len({r['record_id'] for r in results}), 1)
