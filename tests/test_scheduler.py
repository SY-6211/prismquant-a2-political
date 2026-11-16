import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.collection_execution import SourceLease, execute_exclusive
from app.collection_guard import CollectionBlocked
from app.collection_rules import SUPPORTED
from app.collection_runs import start_run
from app.core.config import Settings
from app.core.database import database_url
from app.import_batch import run_import as run_ecb
from app.import_trump_batch import run_import as run_trump
from app.scheduler import main, run_source, tick
from test_preview_batch import context, xml
from test_preview_trump import source_sample
from test_preview_trump_batch import rss


class SchedulerTests(unittest.TestCase):
    def test_cli_requires_explicit_mode_and_validates_before_io(self):
        with patch('app.scheduler.Settings') as settings:
            for args in ([], ['--once', '--loop'], ['--once', '--limit', '6'], ['--once', '--source', 'other'], ['--loop', '--tick-seconds', '1']):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(args)
            settings.assert_not_called()

    def test_once_and_loop_use_same_tick_and_interrupt_stops_loop(self):
        payload = {'items': [{'status': 'disabled'}]}
        with patch('app.scheduler.Settings'), patch('app.scheduler.tick', return_value=payload) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--once', '--source', 'ecb']), 0)
            self.assertEqual(run.call_args.args[1], ['ecb'])
            with patch('app.scheduler.time.sleep', side_effect=KeyboardInterrupt):
                self.assertEqual(main(['--loop', '--tick-seconds', '10']), 130)
        self.assertEqual(run.call_count, 2)

    def test_tick_runs_sources_sequentially_and_stops_on_interrupt(self):
        with patch('app.scheduler.run_source', side_effect=[{'status': 'disabled'}, {'status': 'not_due'}]) as run:
            self.assertEqual(len(tick(None, ['ecb', 'trump', 'ecb'])['items']), 2)
            self.assertEqual([call.args[1] for call in run.call_args_list], ['ecb', 'trump'])
        with patch('app.scheduler.run_source', return_value={'status': 'interrupted'}) as run:
            tick(None, ['ecb', 'trump'])
            run.assert_called_once()

    def test_busy_manual_batch_never_creates_history_or_requests(self):
        lease = MagicMock(acquired=False)
        with patch('app.collection_execution.SourceLease') as factory, patch('app.collection_execution.execute_recorded') as recorded:
            factory.return_value.__enter__.return_value = lease
            result = execute_exclusive(None, 'ecb', 3, None, MagicMock())
        self.assertEqual(result['status'], 'skipped_busy')
        recorded.assert_not_called()

    def test_guard_checks_before_rss_and_before_detail_without_wrong_counts(self):
        for source, runner, module, content, source_context in (
            ('ecb', run_ecb, 'app.import_batch', xml(), context()),
            ('trump', run_trump, 'app.import_trump_batch', rss(), {**source_sample(), 'source_enabled': False}),
        ):
            addresses = []
            with patch(module + '.load_context', return_value=source_context), patch(module + '.read_existing', return_value={}), \
                 httpx.Client(transport=httpx.MockTransport(lambda request: addresses.append(str(request.url)) or httpx.Response(200, content=content))) as client:
                def blocked():
                    raise CollectionBlocked('Do not expose internal reason')
                result = runner(Settings(_env_file=None), 3, client, before_request=blocked, pause=lambda _: None)
                self.assertEqual(result['status'], 'stopped')
                self.assertEqual(addresses, [])
                guard = MagicMock(side_effect=[None, CollectionBlocked('Disabled')])
                result = runner(Settings(_env_file=None), 3, client, before_request=guard, pause=lambda _: None)
                self.assertEqual(len(addresses), 1)
                self.assertEqual(result['summary']['attempted'], 0)
                self.assertEqual(result['summary']['not_attempted'], 3)

    def test_database_guard_failure_before_request_does_not_count_failed_attempt(self):
        calls = MagicMock(side_effect=[None, OperationalError('PRIVATE', {}, Exception('SECRET'))])
        with patch('app.import_batch.load_context', return_value=context()), patch('app.import_batch.read_existing', return_value={}), \
             httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=xml()))) as client:
            result = run_ecb(Settings(_env_file=None), 3, client, before_request=calls, pause=lambda _: None)
        self.assertEqual(result['summary']['failed'], 0)
        self.assertEqual(result['summary']['attempted'], 0)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_write_permission_revoked_stops_without_retry_or_next_request(self):
        from test_preview_ecb_speech import page
        calls = []
        def transport(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=xml() if len(calls) == 1 else page())
        def blocked(*_):
            raise CollectionBlocked('Changed')
        with patch('app.import_batch.load_context', return_value=context()), patch('app.import_batch.read_existing', return_value={}), \
             httpx.Client(transport=httpx.MockTransport(transport)) as client:
            result = run_ecb(Settings(_env_file=None), 3, client, save=blocked, pause=lambda _: None)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['summary']['needs_review'], 1)
        self.assertEqual(result['summary']['not_attempted'], 2)
        self.assertEqual(len(calls), 2)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Opt-in PostgreSQL checks')
class SchedulerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.engine = create_engine(database_url(self.settings), hide_parameters=True)
        self.sid, self.pid = uuid4(), uuid4()
        previous = SUPPORTED['ecb']
        self.mapping = patch.dict(SUPPORTED, {'ecb': (self.sid, self.pid, previous[2], previous[3])})
        self.mapping.start()
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO people(person_id,name_original,monitoring_enabled) VALUES (:id,'Synthetic scheduler',true)"), {'id': self.pid})
            connection.execute(text("INSERT INTO sources(source_id,name,publisher_name,provenance_type,feed_url,enabled) VALUES (:id,'Synthetic scheduler','Test','official',:url,true)"), {'id': self.sid, 'url': previous[2]})
            connection.execute(text('INSERT INTO person_sources(source_id,person_id,enabled) VALUES (:sid,:pid,true)'), {'sid': self.sid, 'pid': self.pid})

    def tearDown(self):
        try:
            with self.engine.begin() as connection:
                connection.execute(text('DELETE FROM collection_schedule WHERE source_id=:sid'), {'sid': self.sid})
                connection.execute(text('DELETE FROM collection_runs WHERE source_id=:sid'), {'sid': self.sid})
                connection.execute(text('DELETE FROM archive_post_details WHERE record_id IN (SELECT record_id FROM public_records WHERE source_id=:sid)'), {'sid': self.sid})
                connection.execute(text('DELETE FROM public_records WHERE source_id=:sid'), {'sid': self.sid})
                connection.execute(text('DELETE FROM person_sources WHERE source_id=:sid'), {'sid': self.sid})
                connection.execute(text('DELETE FROM sources WHERE source_id=:sid'), {'sid': self.sid})
                connection.execute(text('DELETE FROM people WHERE person_id=:pid'), {'pid': self.pid})
        finally:
            self.mapping.stop()
            self.engine.dispose()

    def test_two_database_sessions_and_manual_entry_share_source_lock(self):
        with SourceLease(self.settings, 'ecb') as first:
            self.assertTrue(first.acquired)
            with first.connection.begin():
                first.require(automatic=True)
            with SourceLease(self.settings, 'ecb') as second:
                self.assertFalse(second.acquired)
            with SourceLease(self.settings, 'trump') as other:
                self.assertTrue(other.acquired)
            runner = MagicMock()
            self.assertEqual(execute_exclusive(self.settings, 'ecb', 3, None, runner)['status'], 'skipped_busy')
            runner.assert_not_called()
            self.assertEqual(run_source(self.settings, 'ecb')['status'], 'skipped_busy')
        with SourceLease(self.settings, 'ecb') as third:
            self.assertTrue(third.acquired)

    def test_released_or_closed_connection_cannot_continue_writing(self):
        with SourceLease(self.settings, 'ecb') as lease:
            lease.connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': lease.key})
            lease.connection.commit()
            with patch('app.import_ecb_speech.save_speech') as writer, self.assertRaises(CollectionBlocked):
                lease.save(True, self.settings, {}, {})
            writer.assert_not_called()
        with SourceLease(self.settings, 'ecb') as lease:
            lease.connection.close()
            with self.assertRaises(CollectionBlocked):
                lease.require()
        with SourceLease(self.settings, 'ecb') as again:
            self.assertTrue(again.acquired)

    def test_switch_off_after_admission_vetoes_content_save(self):
        with SourceLease(self.settings, 'ecb') as lease:
            lease.before_request(True)
            with self.engine.begin() as connection:
                connection.execute(text('UPDATE person_sources SET enabled=false WHERE source_id=:sid'), {'sid': self.sid})
            with patch('app.import_ecb_speech.save_speech') as writer, self.assertRaises(CollectionBlocked):
                lease.save(True, self.settings, {}, {})
            writer.assert_not_called()

    def test_due_state_persists_across_new_scheduler_calls_and_current_interval_applies(self):
        addresses = []
        def client():
            return httpx.Client(transport=httpx.MockTransport(lambda request: addresses.append(str(request.url)) or httpx.Response(200, content=b'<rss><channel/></rss>')))
        with patch('app.import_batch.load_context', return_value={**context(), 'source_enabled': True, 'monitoring_enabled': True, 'link_enabled': True}):
            result = run_source(self.settings, 'ecb', client_factory=client)
        self.assertEqual(result['status'], 'no_candidates')
        self.assertEqual(len(addresses), 1)
        with self.engine.connect() as connection:
            row = connection.execute(text('SELECT mode,status FROM collection_runs WHERE source_id=:sid'), {'sid': self.sid}).one()
            self.assertEqual(tuple(row), ('automatic_batch_import', 'no_candidates'))
        forbidden = MagicMock(side_effect=AssertionError('Must not request before due'))
        self.assertEqual(run_source(self.settings, 'ecb', client_factory=forbidden)['status'], 'not_due')
        forbidden.assert_not_called()
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE collection_schedule SET last_attempt_at=clock_timestamp()-interval '90 seconds',last_finished_at=clock_timestamp()-interval '80 seconds' WHERE source_id=:sid"), {'sid': self.sid})
            connection.execute(text('UPDATE sources SET poll_interval_seconds=60 WHERE source_id=:sid'), {'sid': self.sid})
        with patch('app.import_batch.load_context', return_value={**context(), 'source_enabled': True, 'monitoring_enabled': True, 'link_enabled': True}):
            self.assertEqual(run_source(self.settings, 'ecb', client_factory=client)['status'], 'no_candidates')
        self.assertEqual(len(addresses), 2)

    def test_unfinished_history_pauses_source_without_network_or_schedule(self):
        with self.engine.begin() as connection:
            start_run(connection, 'ecb', 3, 'automatic_batch_import')
        factory = MagicMock(side_effect=AssertionError('Must not fetch'))
        self.assertEqual(run_source(self.settings, 'ecb', client_factory=factory)['status'], 'blocked_unfinished_run')
        factory.assert_not_called()

    def test_disabled_source_does_not_create_history_or_schedule(self):
        with self.engine.begin() as connection:
            connection.execute(text('UPDATE sources SET enabled=false WHERE source_id=:sid'), {'sid': self.sid})
        factory = MagicMock(side_effect=AssertionError('Must not fetch'))
        self.assertEqual(run_source(self.settings, 'ecb', client_factory=factory)['status'], 'disabled')
        factory.assert_not_called()
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text('SELECT count(*) FROM collection_schedule WHERE source_id=:sid'), {'sid': self.sid}).scalar_one(), 0)
            self.assertEqual(connection.execute(text('SELECT count(*) FROM collection_runs WHERE source_id=:sid'), {'sid': self.sid}).scalar_one(), 0)

    def test_automatic_collector_saves_full_body_on_lease_session_and_records_run(self):
        from app.import_ecb_speech import save_speech
        from test_preview_ecb_speech import page
        calls = []
        def client():
            def transport(request):
                calls.append(str(request.url))
                return httpx.Response(200, content=xml(1) if len(calls) == 1 else page(body='Synthetic scheduler full text. ' * 100))
            return httpx.Client(transport=httpx.MockTransport(transport))
        def writer(connection, detail, entry, seen):
            return save_speech(connection, {**detail, 'person_id': str(self.pid)}, entry, seen,
                               source_id=self.sid, person_id=self.pid)
        with patch('app.import_batch.load_context', return_value={**context(), 'source_enabled': True, 'monitoring_enabled': True, 'link_enabled': True}), \
             patch('app.import_batch.read_existing', return_value={}), \
             patch('app.import_ecb_speech.save_speech', side_effect=writer):
            result = run_source(self.settings, 'ecb', client_factory=client)
        self.assertEqual(result['summary']['inserted'], 1)
        self.assertEqual(len(calls), 2)
        with self.engine.connect() as connection:
            row = connection.execute(text('SELECT body_text,person_id FROM public_records WHERE source_id=:sid'), {'sid': self.sid}).one()
            self.assertGreater(len(row.body_text), 1200)
            self.assertEqual(row.person_id, self.pid)
            mode = connection.execute(text('SELECT mode FROM collection_runs WHERE source_id=:sid'), {'sid': self.sid}).scalar_one()
            self.assertEqual(mode, 'automatic_batch_import')

    def test_lost_session_after_collection_preserves_confirmed_result_in_output(self):
        def completed(lease, *_args, **_kwargs):
            lease.connection.close()
            return {'status': 'completed', 'saved': True, 'summary': {'inserted': 2},
                    'run_id': str(uuid4()), 'run_record_status': 'saved'}
        with patch('app.scheduler.execute_under_lease', side_effect=completed):
            result = run_source(self.settings, 'ecb', client_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda _: self.fail('No network'))))
        self.assertEqual(result['status'], 'stopped')
        self.assertTrue(result['saved'])
        self.assertEqual(result['summary']['inserted'], 2)
        self.assertEqual(result['run_record_status'], 'saved')
