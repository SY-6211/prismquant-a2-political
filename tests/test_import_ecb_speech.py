"""Single import checks. Synthetic database records always roll back."""

import hashlib
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.database import database_url
from app.import_ecb_speech import TARGET_URL, find_feed_entry, main, save_speech
from app.preview_ecb import ECB_FEED_URL
from app.preview_ecb_speech import PREVIEW_CHARACTERS, parse_speech
from test_preview_ecb_speech import page

SEEN = datetime(2026, 10, 17, 12, 30, tzinfo=timezone.utc)


def full_detail():
    return parse_speech(page(body='Synthetic full speech about policy. ' * 100), TARGET_URL, include_body=True)


class SingleImportTests(unittest.TestCase):
    def test_full_body_is_separate_from_bounded_preview(self):
        content = page(body='Synthetic paragraph ' * 500)
        preview = parse_speech(content, TARGET_URL)
        detail = parse_speech(content, TARGET_URL, include_body=True)
        self.assertNotIn('body_text', preview)
        self.assertGreater(len(detail['body_text']), PREVIEW_CHARACTERS)
        self.assertEqual(detail['body_characters'], len(detail['body_text']))
        self.assertEqual(detail['body_preview'], detail['body_text'][:PREVIEW_CHARACTERS])

    def test_feed_entry_must_match_normalized_target(self):
        entries = [
            {'url': 'https://example.invalid/other', 'published_raw': 'Wrong date'},
            {'url': TARGET_URL + '?tracking=bad'},
            {'url': TARGET_URL.replace('/press/', '//press/') + '#section', 'published_raw': 'Correct entry'},
        ]
        self.assertEqual(find_feed_entry(entries, TARGET_URL)['published_raw'], 'Correct entry')
        self.assertIsNone(find_feed_entry(entries[:2], TARGET_URL))

    def test_unverified_or_truncated_content_never_touches_database(self):
        for change in (
            {'verification_status': 'needs_review'}, {'body_text': None},
            {'body_text': 'Short'}, {'body_text': 'Long enough but incomplete ' * 5},
            {'person_id': None}, {'speaker_evidence': ''},
        ):
            connection = MagicMock()
            with self.subTest(change=change), self.assertRaises(ValueError):
                save_speech(connection, {**full_detail(), **change}, None, SEEN)
            connection.execute.assert_not_called()

    @patch('app.import_ecb_speech.create_engine')
    @patch('app.import_ecb_speech.fetch_speech', return_value={'verification_status': 'needs_review'})
    @patch('app.import_ecb_speech.fetch_feed_items', return_value=[])
    @patch('app.import_ecb_speech.load_source', return_value={'enabled': False})
    def test_unverified_page_stops_before_write_connection(self, load, feed, detail, engine):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)
        detail.assert_called_once()
        self.assertTrue(detail.call_args.kwargs['include_body'])
        engine.assert_not_called()

    @patch('app.import_ecb_speech.create_engine')
    @patch('app.import_ecb_speech.fetch_feed_items', side_effect=httpx.ConnectError('private details'))
    @patch('app.import_ecb_speech.load_source', return_value={'enabled': False})
    def test_network_failure_has_no_write_or_exception_leak(self, load, feed, engine):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertNotIn('private details', output.getvalue())
        engine.assert_not_called()


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class SingleImportDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(database_url(Settings()), hide_parameters=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.person_id = self.connection.execute(text(
            "INSERT INTO people (name_original) VALUES ('Synthetic import person') RETURNING person_id"
        )).scalar_one()
        self.source_id = self.connection.execute(text(
            "INSERT INTO sources (name, publisher_name, provenance_type, feed_url) "
            "VALUES ('Synthetic import source', 'Test publisher', 'official', :url) RETURNING source_id"
        ), {'url': ECB_FEED_URL}).scalar_one()
        self.connection.execute(text(
            'INSERT INTO person_sources (source_id, person_id) VALUES (:source, :person)'
        ), {'source': self.source_id, 'person': self.person_id})
        self.detail = {**full_detail(), 'person_id': str(self.person_id)}
        self.entry = {'url': TARGET_URL, 'published_raw': 'Sat, 17 Oct 2026 07:00:00 +0200'}

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()

    def save(self, detail=None, entry=None, seen=SEEN):
        return save_speech(self.connection, detail or self.detail, self.entry if entry is None else entry, seen,
                           source_id=self.source_id, person_id=self.person_id)

    def stored(self):
        return dict(self.connection.execute(text(
            'SELECT * FROM public_records WHERE source_id=:source'
        ), {'source': self.source_id}).mappings().one())

    def test_saves_full_body_attribution_metadata_and_keeps_switches_off(self):
        result = self.save()
        row = self.stored()
        self.assertEqual(result['action'], 'inserted')
        self.assertGreater(len(row['body_text']), 1200)
        self.assertEqual(row['body_text'], self.detail['body_text'])
        self.assertEqual(row['content_hash'], hashlib.sha256(row['body_text'].encode('utf-8')).hexdigest())
        self.assertEqual(row['person_id'], self.person_id)
        self.assertEqual(row['original_url'], TARGET_URL)
        self.assertEqual(row['retrieved_url'], TARGET_URL)
        self.assertEqual(row['provenance_type'], 'official')
        self.assertEqual(row['content_type'], 'public_statement')
        self.assertEqual(row['verification_status'], 'verified_page_attribution')
        self.assertEqual(row['speaker_evidence'], self.detail['speaker_evidence'])
        self.assertEqual(row['published_at'].astimezone(timezone.utc).isoformat(), '2026-10-17T05:00:00+00:00')
        self.assertEqual(row['published_raw'], self.entry['published_raw'])
        self.assertEqual(row['page_date'].isoformat(), '2026-10-17')
        self.assertEqual(row['first_seen_at'], SEEN)
        self.assertEqual(row['language'], 'en')
        flags = self.connection.execute(text(
            'SELECT s.enabled, p.monitoring_enabled, ps.enabled FROM sources s '
            'JOIN person_sources ps USING(source_id) JOIN people p USING(person_id) WHERE s.source_id=:source'
        ), {'source': self.source_id}).one()
        self.assertEqual(tuple(flags), (False, False, False))

    def test_repeat_preserves_id_timestamps_and_all_existing_values(self):
        first = self.save()
        before = self.stored()
        normalized = {**self.detail, 'url': TARGET_URL.replace('/press/', '//press/') + '#intro'}
        second = self.save(normalized, seen=datetime.now(timezone.utc))
        self.assertEqual(second['action'], 'already_exists')
        self.assertEqual(first['record_id'], second['record_id'])
        self.assertEqual(self.stored(), before)

    def test_changed_body_or_attribution_requires_review_without_overwrite(self):
        self.save()
        before = self.stored()
        changed = self.detail['body_text'] + '\nNew paragraph'
        for detail in (
            {**self.detail, 'body_text': changed, 'body_characters': len(changed)},
            {**self.detail, 'speaker_evidence': 'Statement by Christine Lagarde, changed evidence'},
        ):
            with self.subTest(detail=detail['speaker_evidence']):
                self.assertEqual(self.save(detail)['action'], 'needs_review')
                self.assertEqual(self.stored(), before)

    def test_unknown_publication_time_stays_null_and_page_date_is_separate(self):
        self.save(entry={**self.entry, 'published_raw': 'unknown'})
        row = self.stored()
        self.assertIsNone(row['published_at'])
        self.assertEqual(row['published_raw'], 'unknown')
        self.assertIsNotNone(row['page_date'])

    def test_absent_feed_entry_does_not_invent_publication_time(self):
        save_speech(self.connection, self.detail, None, SEEN, source_id=self.source_id, person_id=self.person_id)
        row = self.stored()
        self.assertIsNone(row['published_at'])
        self.assertIsNone(row['published_raw'])

    def test_missing_link_or_changed_source_config_prevents_insert(self):
        for update in (
            "UPDATE sources SET provenance_type='third_party_archive' WHERE source_id=:id",
            "UPDATE sources SET feed_url='https://example.invalid/feed' WHERE source_id=:id",
            'DELETE FROM person_sources WHERE source_id=:id',
        ):
            with self.subTest(update=update):
                nested = self.connection.begin_nested()
                try:
                    self.connection.execute(text(update), {'id': self.source_id})
                    with self.assertRaises(ValueError):
                        self.save()
                    count = self.connection.execute(text(
                        'SELECT count(*) FROM public_records WHERE source_id=:id'
                    ), {'id': self.source_id}).scalar_one()
                    self.assertEqual(count, 0)
                finally:
                    nested.rollback()

    def test_mismatched_feed_metadata_is_rejected(self):
        with self.assertRaises(ValueError):
            self.save(entry={'url': TARGET_URL.replace('7015ea3099', 'other'), 'published_raw': self.entry['published_raw']})

    def test_caller_rollback_removes_insert(self):
        nested = self.connection.begin_nested()
        self.save()
        nested.rollback()
        count = self.connection.execute(text(
            'SELECT count(*) FROM public_records WHERE source_id=:id'
        ), {'id': self.source_id}).scalar_one()
        self.assertEqual(count, 0)

    def test_metadata_changes_require_review_and_preserve_original(self):
        self.save()
        before = self.stored()
        for changes in ({'title': 'Changed title'}, {'page_date': '2026-10-16'}):
            self.assertEqual(self.save({**self.detail, **changes})['action'], 'needs_review')
            self.assertEqual(self.stored(), before)
        self.assertEqual(self.save(entry={**self.entry, 'published_raw': '2026-10-16T05:00:00Z'})['action'], 'needs_review')
        self.assertEqual(self.stored(), before)
        for column, value in (('language', 'fr'), ('original_url', 'https://example.invalid/'),
                              ('content_hash', 'a' * 64)):
            with self.connection.begin_nested() as transaction:
                self.connection.execute(text(f'UPDATE public_records SET {column}=:value WHERE source_id=:source'),
                                        {'value': value, 'source': self.source_id})
                changed = self.stored()
                self.assertEqual(self.save()['action'], 'needs_review')
                self.assertEqual(self.stored(), changed)
                transaction.rollback()
