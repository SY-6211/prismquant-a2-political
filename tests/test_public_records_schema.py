"""Opt-in schema checks using synthetic data rolled back after each test."""

import hashlib
import os
import unittest
from datetime import date
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.database import database_url


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class PublicRecordsSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(database_url(Settings()), hide_parameters=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.source_id = self.source()
        self.person_id = self.connection.execute(text(
            "INSERT INTO people (name_original) VALUES ('Temporary record test person') RETURNING person_id"
        )).scalar_one()

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()

    def source(self):
        return self.connection.execute(text(
            "INSERT INTO sources (name, publisher_name, provenance_type, feed_url) "
            "VALUES ('Temporary record source', 'Test publisher', 'official', 'https://example.invalid/feed') RETURNING source_id"
        )).scalar_one()

    def record(self, **changes):
        values = {
            'source_id': self.source_id, 'person_id': None, 'title': 'Synthetic record',
            'provenance_type': 'official', 'content_type': 'unconfirmed',
            'original_url': None, 'retrieved_url': 'https://example.invalid/' + str(uuid4()),
            'page_date': None, 'language': None, 'body_text': 'Synthetic body for schema tests.',
            'speaker_evidence': None, 'verification_status': 'needs_review',
            'content_hash': hashlib.sha256(b'Synthetic body for schema tests.').hexdigest(),
        }
        values.update(changes)
        return self.connection.execute(text(
            'INSERT INTO public_records (source_id, person_id, title, provenance_type, content_type, '
            'original_url, retrieved_url, page_date, language, body_text, speaker_evidence, verification_status, content_hash) '
            'VALUES (:source_id, :person_id, :title, :provenance_type, :content_type, :original_url, :retrieved_url, '
            ':page_date, :language, :body_text, :speaker_evidence, :verification_status, :content_hash) RETURNING *'
        ), values).mappings().one()

    def test_eighteen_columns_and_unique_source_url(self):
        inspector = inspect(self.connection)
        columns = {c['name']: c for c in inspector.get_columns('public_records')}
        self.assertEqual(set(columns), {
            'record_id', 'source_id', 'person_id', 'title', 'content_type', 'provenance_type',
            'original_url', 'retrieved_url', 'published_at', 'page_date', 'published_raw',
            'first_seen_at', 'language', 'body_text', 'speaker_evidence', 'verification_status', 'content_hash', 'created_at',
        })
        self.assertEqual(inspector.get_pk_constraint('public_records')['constrained_columns'], ['record_id'])
        self.assertIn(['source_id', 'retrieved_url'], [u['column_names'] for u in inspector.get_unique_constraints('public_records')])
        for name in ('person_id', 'original_url', 'published_at', 'page_date', 'published_raw', 'language', 'speaker_evidence'):
            self.assertTrue(columns[name]['nullable'])

    def test_unknown_times_remain_null_and_creation_is_timezone_aware(self):
        row = self.record(page_date=date(2026, 10, 17))
        self.assertIsNotNone(row['record_id'])
        self.assertIsNone(row['published_at'])
        self.assertIsNone(row['published_raw'])
        self.assertIsNone(row['person_id'])
        self.assertEqual(row['page_date'], date(2026, 10, 17))
        self.assertIsNotNone(row['first_seen_at'].tzinfo)
        self.assertIsNotNone(row['created_at'].tzinfo)

    def test_defaults_do_not_claim_verified_speaker(self):
        row = self.connection.execute(text(
            "INSERT INTO public_records (source_id, title, provenance_type, retrieved_url, body_text, content_hash) "
            "VALUES (:source, 'Synthetic defaults', 'official', 'https://example.invalid/default', 'Synthetic body', :hash) RETURNING *"
        ), {'source': self.source_id, 'hash': hashlib.sha256(b'Synthetic body').hexdigest()}).mappings().one()
        self.assertEqual(row['content_type'], 'unconfirmed')
        self.assertEqual(row['verification_status'], 'needs_review')

    def test_duplicate_url_same_source_rejected_but_cross_source_allowed(self):
        first = self.record()
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.record(retrieved_url=first['retrieved_url'], title='Changed title')
        second = self.record(source_id=self.source(), retrieved_url=first['retrieved_url'])
        self.assertNotEqual(first['record_id'], second['record_id'])

    def test_invalid_fields_and_missing_references_are_rejected(self):
        for changes in (
            {'title': '   '}, {'body_text': ''}, {'language': ''}, {'content_hash': 'bad-hash'},
            {'content_type': 'unknown'}, {'provenance_type': 'unknown'}, {'verification_status': 'certain'},
            {'retrieved_url': 'file:///local'}, {'original_url': 'not-a-url'},
            {'source_id': uuid4()}, {'person_id': uuid4()},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError):
                    with self.connection.begin_nested():
                        self.record(**changes)

    def test_verified_attribution_requires_person_and_evidence(self):
        for changes in ({}, {'person_id': self.person_id}, {'person_id': self.person_id, 'speaker_evidence': '   '}):
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.record(verification_status='verified_page_attribution', **changes)
        row = self.record(person_id=self.person_id, content_type='public_statement',
                          speaker_evidence='Speech by Temporary Person', verification_status='verified_page_attribution')
        self.assertEqual(row['person_id'], self.person_id)

    def test_history_snapshot_and_referenced_deletion_protection(self):
        row = self.record(person_id=self.person_id)
        self.connection.execute(text("UPDATE sources SET provenance_type='third_party_archive' WHERE source_id=:id"), {'id': self.source_id})
        snapshot = self.connection.execute(text('SELECT provenance_type FROM public_records WHERE record_id=:id'), {'id': row['record_id']}).scalar_one()
        self.assertEqual(snapshot, 'official')
        for query, identity in (
            ('DELETE FROM sources WHERE source_id=:id', self.source_id),
            ('DELETE FROM people WHERE person_id=:id', self.person_id),
        ):
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.connection.execute(text(query), {'id': identity})
