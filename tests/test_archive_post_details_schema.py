"""Opt-in PostgreSQL constraints, using only synthetic data in rolled-back transactions."""

import hashlib
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.database import database_url


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class ArchivePostDetailsSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(database_url(Settings()), hide_parameters=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.source_id = self.connection.execute(text(
            "INSERT INTO sources (name, publisher_name, provenance_type, feed_url) "
            "VALUES ('Synthetic archive schema source', 'Test publisher', 'third_party_archive', "
            "'https://example.invalid/feed') RETURNING source_id"
        )).scalar_one()
        self.record_id = self.record()

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()

    def record(self):
        return self.connection.execute(text(
            'INSERT INTO public_records (source_id, title, provenance_type, retrieved_url, body_text, content_hash) '
            "VALUES (:source, 'Synthetic archive title', 'third_party_archive', :url, 'Synthetic own text.', :hash) "
            'RETURNING record_id'
        ), {'source': self.source_id, 'url': 'https://example.invalid/' + str(uuid4()),
            'hash': hashlib.sha256(b'Synthetic own text.').hexdigest()}).scalar_one()

    def detail(self, **changes):
        values = {
            'record_id': self.record_id, 'archive_author': 'Synthetic Author',
            'archive_handle': '@synthetic', 'archive_post_type': 'original',
            'parser_version': 'test_v1', 'snapshot_hash': 'a' * 64,
        }
        values.update(changes)
        # Column names are fixed test fixture keys; all values are bound.
        json_fields = {'inline_quotes', 'quoted_posts', 'archive_media_notes'}
        expressions = [f'CAST(:{key} AS jsonb)' if key in json_fields else ':' + key for key in values]
        bound = {key: json.dumps(value, ensure_ascii=False) if key in json_fields else value
                 for key, value in values.items()}
        return dict(self.connection.execute(text(
            'INSERT INTO archive_post_details (' + ', '.join(values) + ') VALUES (' +
            ', '.join(expressions) + ') RETURNING *'
        ), bound).mappings().one())

    def test_eighteen_columns_types_primary_key_and_reference(self):
        inspector = inspect(self.connection)
        columns = {column['name']: column for column in inspector.get_columns('archive_post_details')}
        self.assertEqual(set(columns), {
            'record_id', 'archive_author', 'archive_handle', 'archive_post_type', 'archive_removed_raw',
            'title_origin', 'page_post_published_at', 'page_post_published_raw', 'archive_captured_at',
            'archive_capture_raw', 'image_count', 'video_count', 'inline_quotes', 'quoted_posts',
            'archive_media_notes', 'parser_version', 'snapshot_hash', 'created_at',
        })
        self.assertIsNone(columns['record_id']['default'])
        self.assertEqual(inspector.get_pk_constraint('archive_post_details')['constrained_columns'], ['record_id'])
        foreign_key = inspector.get_foreign_keys('archive_post_details')[0]
        self.assertEqual(foreign_key['constrained_columns'], ['record_id'])
        self.assertEqual(foreign_key['referred_table'], 'public_records')
        self.assertEqual(foreign_key['referred_columns'], ['record_id'])
        self.assertEqual(foreign_key['options']['ondelete'], 'RESTRICT')
        for name in ('inline_quotes', 'quoted_posts', 'archive_media_notes'):
            self.assertIsInstance(columns[name]['type'], JSONB)
        for name in ('page_post_published_at', 'archive_captured_at', 'created_at'):
            self.assertTrue(columns[name]['type'].timezone)

    def test_defaults_do_not_invent_dates_or_removed_flag(self):
        row = self.detail()
        for name in ('archive_removed_raw', 'page_post_published_at', 'page_post_published_raw',
                     'archive_captured_at', 'archive_capture_raw'):
            self.assertIsNone(row[name])
        self.assertEqual(row['title_origin'], 'archive_rss')
        self.assertEqual((row['image_count'], row['video_count']), (0, 0))
        for name in ('inline_quotes', 'quoted_posts', 'archive_media_notes'):
            self.assertEqual(row[name], [])
        self.assertIsNotNone(row['created_at'].tzinfo)

    def test_full_unicode_arrays_are_preserved_separately_from_main_body(self):
        notes = [{'kind': 'archive_image_description', 'text': '合成图片说明。' * 400,
                  'owner_archive_url': None, 'association_status': 'unresolved'}]
        quotes = [{'archive_author': 'Other synthetic author', 'text': 'Separate quoted words.'}]
        inline = [{'position': 0, 'text': 'Separate inline quote.', 'attribution': None}]
        before = dict(self.connection.execute(text(
            'SELECT * FROM public_records WHERE record_id=:id'
        ), {'id': self.record_id}).mappings().one())
        row = self.detail(inline_quotes=inline, quoted_posts=quotes, archive_media_notes=notes, image_count=1)
        self.assertEqual(row['inline_quotes'], inline)
        self.assertEqual(row['quoted_posts'], quotes)
        self.assertEqual(row['archive_media_notes'], notes)
        after = dict(self.connection.execute(text(
            'SELECT * FROM public_records WHERE record_id=:id'
        ), {'id': self.record_id}).mappings().one())
        self.assertEqual(before, after)
        self.assertNotIn(notes[0]['text'], after['body_text'])

    def test_required_fields_enums_counts_and_hash_format(self):
        for changes in (
            {'archive_author': ''}, {'archive_handle': '   '}, {'parser_version': ''},
            {'archive_author': None}, {'archive_handle': None}, {'parser_version': None},
            {'archive_post_type': None}, {'archive_post_type': 'unknown'},
            {'title_origin': 'official'}, {'title_origin': None},
            {'image_count': -1}, {'video_count': -1}, {'image_count': None}, {'video_count': None},
            {'snapshot_hash': None}, {'snapshot_hash': 'bad'}, {'snapshot_hash': 'A' * 64},
            {'created_at': None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError):
                    with self.connection.begin_nested():
                        self.detail(**changes)
        # The table supports the designed enum; the first writer will be stricter.
        for kind in ('original', 'quote', 'retruth'):
            nested = self.connection.begin_nested()
            try:
                self.assertEqual(self.detail(archive_post_type=kind)['archive_post_type'], kind)
            finally:
                nested.rollback()

    def test_all_json_fields_require_arrays_and_reject_sql_null(self):
        for name in ('inline_quotes', 'quoted_posts', 'archive_media_notes'):
            for value in ({}, 'not an array', 1, None):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(IntegrityError):
                        with self.connection.begin_nested():
                            self.detail(**{name: value})
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.connection.execute(text(
                        'INSERT INTO archive_post_details (record_id, archive_author, archive_handle, '
                        'archive_post_type, parser_version, snapshot_hash, ' + name + ') '
                        "VALUES (:id, 'Test', '@test', 'original', 'test_v1', :hash, NULL)"
                    ), {'id': self.record_id, 'hash': 'a' * 64})

    def test_one_to_one_missing_record_and_parent_deletion_protection(self):
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.detail(record_id=uuid4())
        self.detail()
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.detail()
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.connection.execute(text('DELETE FROM public_records WHERE record_id=:id'), {'id': self.record_id})
        self.assertIsNotNone(self.detail(record_id=self.record())['record_id'])

    def test_page_publication_and_capture_time_are_independent(self):
        published = datetime(2026, 10, 17, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        captured = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)
        row = self.detail(page_post_published_at=published, archive_captured_at=captured,
                          page_post_published_raw='2026-10-17T08:00:00+08:00',
                          archive_capture_raw='2026-10-17T13:00:00Z', archive_removed_raw='false')
        self.assertEqual(row['page_post_published_at'], published)
        self.assertEqual(row['archive_captured_at'], captured)
        self.assertNotEqual(row['page_post_published_at'], row['archive_captured_at'])
        self.assertEqual(row['page_post_published_raw'], '2026-10-17T08:00:00+08:00')
        self.assertEqual(row['archive_removed_raw'], 'false')
