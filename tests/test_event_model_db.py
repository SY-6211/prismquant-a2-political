"""Opt-in PostgreSQL tests. All fixtures live in a unique disposable schema."""
import importlib.util
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app.core.config import PROJECT_ROOT, Settings
from app.core.database import database_url
from app.event_model import snapshot_from_record
from app.main import create_app
from app.project_events import project_records
from app.repositories.events import save_snapshot, select_event, select_versions
from test_event_model import record

NOW = datetime(2026, 11, 7, 10, tzinfo=timezone.utc)


@unittest.skipUnless(os.environ.get('PQ_EVENT_DB_TESTS') == '1', 'Isolated-schema PostgreSQL tests are opt-in')
class EventDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'pq_event_test_' + uuid4().hex
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

    @classmethod
    def drop_schema(cls):
        # The name is generated here, never supplied by a user or read from config.
        assert cls.schema.startswith('pq_event_test_') and len(cls.schema) == 46
        with cls.base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {cls.schema} CASCADE'))
        cls.base.dispose()

    def setUp(self):
        self.conn = self.engine.connect()
        self.tx = self.conn.begin()
        self.addCleanup(self.conn.close)
        self.addCleanup(lambda: self.tx.rollback() if self.tx.is_active else None)
        self.person, self.source = uuid4(), uuid4()
        self.conn.execute(text("INSERT INTO people (person_id, name_original) VALUES (:id, 'Synthetic person')"), {'id': self.person})
        self.conn.execute(text("INSERT INTO sources (source_id, name, publisher_name, provenance_type, feed_url) "
            "VALUES (:id, 'Synthetic archive', 'Test publisher', 'third_party_archive', 'https://example.invalid/rss')"), {'id': self.source})
        self.snap = snapshot_from_record(record(person_id=self.person, original_url=f'https://truthsocial.com/@test/{uuid4().int}'))

    def save(self, snap=None, at=NOW, **kwargs):
        return save_snapshot(self.conn, self.source, 'rss', snap or self.snap, at, **kwargs)

    def test_new_repeat_changed_and_returned_content_preserve_chronology(self):
        a = self.save()
        again = self.save(at=NOW + timedelta(seconds=1))
        changed = self.snap.model_copy(update={'main_text': 'Changed words'})
        b = self.save(changed, NOW + timedelta(seconds=2))
        returned = self.save(at=NOW + timedelta(seconds=3))
        self.assertTrue(a['version_created'])
        self.assertFalse(again['version_created'])
        self.assertEqual(a['version_id'], returned['version_id'])
        self.assertNotEqual(a['version_id'], b['version_id'])
        detail = select_event(self.conn, UUID(a['event_id']))
        self.assertEqual(str(detail['version_id']), a['version_id'])
        self.assertEqual(len(select_versions(self.conn, UUID(a['event_id']))), 2)
        self.assertEqual(self.conn.execute(text('SELECT count(*) FROM event_observations WHERE event_id=:id'),
            {'id': a['event_id']}).scalar_one(), 4)

    def test_two_channels_share_native_event_without_promoting_archive_attribution(self):
        a = self.save()
        b = save_snapshot(self.conn, self.source, 'web_index', self.snap, NOW + timedelta(seconds=1))
        self.assertEqual(a['event_id'], b['event_id'])
        detail = select_event(self.conn, UUID(a['event_id']))
        self.assertEqual(detail['transport'], 'web_index')
        self.assertEqual(detail['verification_status'], 'archive_only')
        self.assertEqual(len(select_versions(self.conn, UUID(a['event_id']))), 2)

    def test_publisher_provenance_mismatch_rolls_back_event_writes(self):
        bad = self.snap.model_copy(update={'provenance_type': 'official', 'verification_status': 'verified_page_attribution'})
        before = self.conn.execute(text('SELECT count(*) FROM monitor_events')).scalar_one()
        with self.assertRaises(ValueError):
            self.save(bad)
        self.assertEqual(self.conn.execute(text('SELECT count(*) FROM monitor_events')).scalar_one(), before)

    def test_versions_are_immutable_even_with_direct_sql(self):
        a = self.save()
        for sql in ('UPDATE event_versions SET version_number=99 WHERE version_id=:id',
                    'DELETE FROM event_versions WHERE version_id=:id'):
            with self.assertRaises(DBAPIError), self.conn.begin_nested():
                self.conn.execute(text(sql), {'id': a['version_id']})

    def test_legacy_projection_is_idempotent_preserves_unknown_times_and_legacy_rows(self):
        rid = uuid4()
        self.conn.execute(text("INSERT INTO public_records (record_id, source_id, person_id, title, "
            "content_type, provenance_type, retrieved_url, body_text, verification_status, content_hash, first_seen_at) "
            "VALUES (:id, :source, :person, 'Legacy', 'public_statement', 'third_party_archive', "
            "'https://example.invalid/legacy', 'Legacy own text', 'archive_only', :hash, :at)"),
            {'id': rid, 'source': self.source, 'person': self.person, 'hash': 'a'*64, 'at': NOW})
        old = dict(self.conn.execute(text('SELECT * FROM public_records WHERE record_id=:id'), {'id': rid}).mappings().one())
        self.assertEqual(project_records(self.conn)['selected'], 1)
        self.assertEqual(self.conn.execute(text('SELECT count(*) FROM event_record_links')).scalar_one(), 0)
        self.assertEqual(project_records(self.conn, apply=True)['selected'], 1)
        self.assertEqual(project_records(self.conn, apply=True)['selected'], 0)
        stored = dict(self.conn.execute(text('SELECT * FROM public_records WHERE record_id=:id'), {'id': rid}).mappings().one())
        self.assertEqual(old, stored)
        detail = self.conn.execute(text('SELECT payload FROM event_versions WHERE event_id IN '
            '(SELECT event_id FROM event_record_links WHERE record_id=:id)'), {'id': rid}).scalar_one()
        self.assertIsNone(detail['published_at'])

    def test_existing_ecb_writer_saves_review_versions_without_overwriting_old_body(self):
        from test_import_ecb_speech import full_detail
        from app.import_ecb_speech import save_speech
        from app.preview_ecb import ECB_FEED_URL
        self.conn.execute(text("UPDATE sources SET provenance_type='official', feed_url=:url WHERE source_id=:id"),
                          {'url': ECB_FEED_URL, 'id': self.source})
        self.conn.execute(text('INSERT INTO person_sources (source_id, person_id) VALUES (:s, :p)'), {'s': self.source, 'p': self.person})
        detail = {**full_detail(), 'person_id': str(self.person)}
        a = save_speech(self.conn, detail, None, NOW, source_id=self.source, person_id=self.person)
        changed = {**detail, 'body_text': detail['body_text'] + ' Revised.', 'body_characters': detail['body_characters'] + 9}
        b = save_speech(self.conn, changed, None, NOW + timedelta(seconds=1), source_id=self.source, person_id=self.person)
        self.assertEqual(b['action'], 'needs_review')
        self.assertEqual(a['event_id'], b['event_id'])
        self.assertEqual(self.conn.execute(text('SELECT body_text FROM public_records WHERE record_id=:id'),
                         {'id': a['record_id']}).scalar_one(), detail['body_text'])
        latest = select_event(self.conn, UUID(a['event_id']))
        self.assertEqual(latest['payload']['main_text'], changed['body_text'])
        self.assertEqual(latest['review_status'], 'needs_review')

    def test_existing_trump_writer_is_atomic_and_media_text_stays_separate(self):
        from test_import_trump_post import add_fixture, extraction
        from app.import_trump_post import save_post
        source, person = uuid4(), uuid4()
        add_fixture(self.conn, source, person)
        payload = extraction(source, person)
        result = save_post(self.conn, payload, NOW, source_id=source, person_id=person)
        detail = select_event(self.conn, UUID(result['event_id']))
        self.assertEqual(detail['payload']['main_text'], payload['record']['body_text'])
        self.assertEqual(len(detail['payload']['media_notes']), 1)
        self.assertNotIn('合成独立', detail['payload']['main_text'])
        self.assertEqual(detail['linked_record_ids'], [UUID(result['record_id'])])

    def test_native_event_concurrent_writers_do_not_duplicate_versions(self):
        # Commit only synthetic fixtures to the disposable schema for other sessions.
        self.tx.commit()
        self.tx = self.conn.begin()
        def writer(_):
            with self.engine.begin() as conn:
                return save_snapshot(conn, self.source, 'rss', self.snap, NOW)
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(writer, range(3)))
        self.assertEqual(len({row['event_id'] for row in results}), 1)
        self.assertEqual(len({row['version_id'] for row in results}), 1)
        self.assertEqual(sum(row['version_created'] for row in results), 1)

    def test_failed_event_write_rolls_back_new_legacy_rows_for_both_writers(self):
        from unittest.mock import patch
        from test_import_trump_post import add_fixture, extraction
        from app.import_trump_post import save_post
        from test_import_ecb_speech import full_detail
        from app.import_ecb_speech import save_speech
        from app.preview_ecb import ECB_FEED_URL
        source, person = uuid4(), uuid4()
        add_fixture(self.conn, source, person)
        with patch('app.repositories.events.save_snapshot', side_effect=ValueError('Synthetic failure')):
            with self.assertRaises(ValueError):
                save_post(self.conn, extraction(source, person), NOW, source_id=source, person_id=person)
            self.conn.execute(text("UPDATE sources SET provenance_type='official', feed_url=:url WHERE source_id=:id"),
                              {'url': ECB_FEED_URL, 'id': source})
            with self.assertRaises(ValueError):
                save_speech(self.conn, {**full_detail(), 'person_id': str(person)}, None, NOW,
                            source_id=source, person_id=person)
        count = self.conn.execute(text('SELECT count(*) FROM public_records WHERE source_id=:id'), {'id': source}).scalar_one()
        self.assertEqual(count, 0)

    def test_old_projection_observation_does_not_replace_more_recent_live_content(self):
        current = self.save()
        old = self.snap.model_copy(update={'main_text': 'Earlier archived text'})
        self.save(old, NOW - timedelta(days=10), observation_kind='legacy_projection')
        latest = select_event(self.conn, UUID(current['event_id']))
        self.assertEqual(str(latest['version_id']), current['version_id'])

    def test_foreign_key_rejects_observation_pointing_at_another_event_version(self):
        a = self.save()
        b = self.save(self.snap.model_copy(update={'original_url': 'https://truthsocial.com/@test/999999'}))
        with self.assertRaises(DBAPIError), self.conn.begin_nested():
            self.conn.execute(text("INSERT INTO event_observations (event_id, version_id, observed_at, observation_kind) "
                "VALUES (:event, :version, :at, 'live_fetch')"), {'event': a['event_id'], 'version': b['version_id'], 'at': NOW})

    def test_real_api_reads_paginated_history_in_disposable_schema(self):
        from unittest.mock import patch
        result = self.save()
        app = create_app(Settings(_env_file=None, environment='test'))
        def reader(settings, selector, *args):
            return selector(self.conn, *args)
        with patch('app.api.events.read_query', side_effect=reader), TestClient(app) as client:
            eid = result['event_id']
            for path in (f'/events/{eid}', f'/events/{eid}/versions', f'/events/{eid}/versions/{result["version_id"]}',
                         f'/events/{eid}/observations', f'/events?person_id={self.person}'):
                response = client.get('/api/v1' + path)
                self.assertEqual(response.status_code, 200, response.text)
