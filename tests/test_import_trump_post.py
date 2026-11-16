import copy
import hashlib
import io
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import Settings
from app.core.database import database_url
from app.extract_trump_post import prepare_extraction
from app.import_trump_post import ArchiveDraft, RecordDraft, _insert_archive, main, save_post, snapshot_hash
from app.preview_trump import PERSON_ID, SOURCE_ID, TRUMP_FEED_URL
from app.preview_trump_post import parse_post
from test_preview_trump import source_sample
from test_preview_trump_post import candidate, note, page, status

SEEN = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)


def extraction(source=SOURCE_ID, person=PERSON_ID):
    entry = candidate(published_raw='2026-10-17T04:00:00Z')
    payload = prepare_extraction(parse_post(
        page(status('Short own text.', media='<div class="status-attachment--image"></div>'),
             note(text='合成独立图片说明。' * 200)), entry, include_full=True,
    ), entry)
    payload['record'].update(source_id=str(source), person_id=str(person))
    return payload


def add_fixture(connection, source, person):
    connection.execute(text("INSERT INTO people (person_id, name_original) VALUES (:id, 'Synthetic archive import person')"), {'id': person})
    connection.execute(text(
        'INSERT INTO sources (source_id, name, publisher_name, provenance_type, feed_url) '
        "VALUES (:id, 'Synthetic archive import source', 'Test publisher', 'third_party_archive', :url)"
    ), {'id': source, 'url': TRUMP_FEED_URL})
    connection.execute(text('INSERT INTO person_sources (source_id, person_id) VALUES (:source, :person)'),
                       {'source': source, 'person': person})


class TrumpImportTests(unittest.TestCase):
    def test_incomplete_unsupported_or_untyped_data_never_reaches_database(self):
        original = extraction()
        variants = []
        for key in ('extraction_complete', 'ingestion_eligible'):
            value = copy.deepcopy(original)
            value[key] = False
            variants.append(value)
        for field, value in (('body_text', ''), ('content_hash', 'a' * 64),
                             ('provenance_type', 'official'), ('verification_status', 'verified_page_attribution'),
                             ('published_at', '2026-10-17T04:00:00'), ('published_at', '2026-10-17T05:00:00Z')):
            payload = copy.deepcopy(original)
            payload['record'][field] = value
            variants.append(payload)
        for field, value in (('archive_post_type', 'quote'), ('quoted_posts', [{'text': 'Quoted'}]),
                             ('inline_quotes', [{'text': 'Inline'}]), ('image_count', -1), ('video_count', True)):
            payload = copy.deepcopy(original)
            payload['archive_post'][field] = value
            variants.append(payload)
        for payload in variants:
            connection = MagicMock()
            with self.subTest(payload=payload['record']['body_text']), self.assertRaises(ValueError):
                save_post(connection, payload, SEEN)
            connection.execute.assert_not_called()

    def test_media_note_schema_rejects_preview_text_invalid_order_and_attribution(self):
        for change in ({'position': 2}, {'position': True}, {'text_preview': 'Truncated'},
                       {'text': None}, {'kind': 'other_archive_note'}, {'kind': 'unavailable'},
                       {'owner_archive_url': 'https://www.trumpstruth.org/statuses/1'},
                       {'association_status': 'guessed'}):
            payload = extraction()
            payload['archive_post']['archive_media_notes'][0].update(change)
            connection = MagicMock()
            with self.subTest(change=change), self.assertRaises(ValueError):
                save_post(connection, payload, SEEN)
            connection.execute.assert_not_called()

    def test_snapshot_hash_is_stable_and_includes_media_and_metadata(self):
        payload = extraction()
        record = RecordDraft.model_validate(payload['record'])
        archive = ArchiveDraft.model_validate(payload['archive_post'])
        digest = snapshot_hash(record, archive)
        equivalent = {**payload['record'], 'published_at': datetime(2026, 10, 17, 12, tzinfo=timezone(timedelta(hours=8)))}
        self.assertEqual(snapshot_hash(RecordDraft.model_validate(equivalent), archive), digest)
        payload['archive_post']['archive_media_notes'][0]['text'] += 'Changed description.'
        self.assertNotEqual(snapshot_hash(record, ArchiveDraft.model_validate(payload['archive_post'])), digest)
        self.assertEqual(record.content_hash, hashlib.sha256(record.body_text.encode('utf-8')).hexdigest())

    @patch('app.import_trump_post.load_source', return_value=source_sample())
    @patch('app.import_trump_post.create_engine')
    @patch('app.import_trump_post.fetch_post')
    def test_missing_target_or_network_failure_never_opens_writer(self, detail, engine, load):
        for entries, failure, expected in (([], None, 2), (None, httpx.ConnectError('PRIVATE'), 1)):
            output = io.StringIO()
            with patch('app.import_trump_post.fetch_feed_items', return_value=entries, side_effect=failure), redirect_stdout(output):
                self.assertEqual(main(), expected)
            self.assertNotIn('PRIVATE', output.getvalue())
        engine.assert_not_called()
        detail.assert_not_called()

    @patch('app.import_trump_post.load_source', return_value=source_sample())
    @patch('app.import_trump_post.fetch_feed_items', return_value=[])
    @patch('app.import_trump_post.find_target', return_value=candidate())
    @patch('app.import_trump_post.fetch_post', return_value={})
    @patch('app.import_trump_post.prepare_extraction')
    @patch('app.import_trump_post.create_engine')
    def test_review_and_database_errors_do_not_expose_private_messages(self, engine, prepare, detail, target, feed, load):
        prepare.return_value = {'extraction_complete': False, 'ingestion_eligible': False}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 2)
        engine.assert_not_called()
        prepare.return_value = extraction()
        engine.side_effect = OperationalError('private SQL', {}, Exception('password=PRIVATE'))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertNotIn('private SQL', output.getvalue())


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class TrumpImportDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(database_url(Settings()), hide_parameters=True,
                                  connect_args={'connect_timeout': 5, 'options': '-c lock_timeout=5000 -c statement_timeout=10000'})

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.source, self.person = uuid4(), uuid4()
        add_fixture(self.connection, self.source, self.person)
        self.payload = extraction(self.source, self.person)

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()

    def save(self, payload=None, seen=SEEN):
        return save_post(self.connection, payload or self.payload, seen, source_id=self.source, person_id=self.person)

    def stored(self):
        record = dict(self.connection.execute(text('SELECT * FROM public_records WHERE source_id=:id'),
                                              {'id': self.source}).mappings().one())
        archive = dict(self.connection.execute(text('SELECT * FROM archive_post_details WHERE record_id=:id'),
                                               {'id': record['record_id']}).mappings().one())
        return record, archive

    def counts(self):
        return tuple(self.connection.execute(text(
            'SELECT (SELECT count(*) FROM public_records WHERE source_id=:id), '
            '(SELECT count(*) FROM archive_post_details a JOIN public_records r USING(record_id) WHERE r.source_id=:id)'
        ), {'id': self.source}).one())

    def test_saves_short_body_and_full_notes_in_separate_rows_with_flags_off(self):
        result = self.save()
        record, archive = self.stored()
        self.assertEqual(result['action'], 'inserted')
        self.assertTrue(result['saved'])
        self.assertEqual(record['body_text'], 'Short own text.')
        self.assertGreater(len(archive['archive_media_notes'][0]['text']), 1200)
        self.assertEqual(archive['archive_media_notes'], self.payload['archive_post']['archive_media_notes'])
        self.assertEqual(record['verification_status'], 'archive_only')
        self.assertEqual(record['provenance_type'], 'third_party_archive')
        self.assertEqual(record['first_seen_at'], SEEN)
        self.assertNotEqual(record['published_at'], archive['archive_captured_at'])
        self.assertEqual(record['record_id'], archive['record_id'])
        self.assertEqual(archive['snapshot_hash'], snapshot_hash(
            RecordDraft.model_validate(self.payload['record']), ArchiveDraft.model_validate(self.payload['archive_post'])))
        self.assertEqual(self.counts(), (1, 1))
        self.assertFalse(result['source_enabled'])
        self.assertFalse(result['monitoring_enabled'])
        self.assertFalse(result['link_enabled'])

    def test_repeat_preserves_every_field_and_timestamp(self):
        first = self.save()
        before = self.stored()
        payload = copy.deepcopy(self.payload)
        payload['record']['retrieved_url'] += '#post'
        second = self.save(payload, datetime.now(timezone.utc))
        self.assertEqual(second['action'], 'already_exists')
        self.assertFalse(second['saved'])
        self.assertEqual(second['record_id'], first['record_id'])
        self.assertEqual(self.stored(), before)
        self.assertEqual(self.counts(), (1, 1))

    def test_changed_body_title_media_or_capture_metadata_requires_review(self):
        self.save()
        before = self.stored()
        for section, key, value in (
            ('record', 'body_text', 'Changed own text.'), ('record', 'title', 'Changed title'),
            ('archive_post', 'archive_removed_raw', 'true'),
            ('archive_post', 'archive_media_notes', [{**self.payload['archive_post']['archive_media_notes'][0], 'text': 'Changed explanation.'}]),
        ):
            payload = copy.deepcopy(self.payload)
            payload[section][key] = value
            if key == 'body_text':
                payload['record']['content_hash'] = hashlib.sha256(value.encode('utf-8')).hexdigest()
            with self.subTest(key=key):
                self.assertEqual(self.save(payload)['action'], 'needs_review')
                self.assertEqual(self.stored(), before)

    def test_stored_hash_or_data_tampering_and_missing_detail_are_not_repaired(self):
        self.save()
        identity = self.stored()[0]['record_id']
        for update in (
            "UPDATE archive_post_details SET snapshot_hash=repeat('b',64) WHERE record_id=:id",
            "UPDATE archive_post_details SET image_count=image_count+1 WHERE record_id=:id",
            "UPDATE public_records SET body_text='Changed externally' WHERE record_id=:id",
        ):
            nested = self.connection.begin_nested()
            try:
                self.connection.execute(text(update), {'id': identity})
                before = self.stored()
                self.assertEqual(self.save()['action'], 'needs_review')
                self.assertEqual(self.stored(), before)
            finally:
                nested.rollback()
        self.connection.execute(text('DELETE FROM archive_post_details WHERE record_id=:id'), {'id': identity})
        self.assertEqual(self.save()['action'], 'needs_review')
        self.assertEqual(self.counts(), (1, 0))

    def test_changed_config_or_missing_association_prevents_insert(self):
        for update in (
            "UPDATE sources SET provenance_type='official' WHERE source_id=:id",
            "UPDATE sources SET feed_url='https://example.invalid/feed' WHERE source_id=:id",
            'DELETE FROM person_sources WHERE source_id=:id',
        ):
            nested = self.connection.begin_nested()
            try:
                self.connection.execute(text(update), {'id': self.source})
                with self.assertRaises(ValueError):
                    self.save()
                self.assertEqual(self.counts(), (0, 0))
            finally:
                nested.rollback()

    def test_unknown_publication_times_remain_null(self):
        payload = copy.deepcopy(self.payload)
        payload['record'].update(published_at=None, published_raw='unknown')
        payload['archive_post'].update(page_post_published_at=None, page_post_published_raw=None,
                                       archive_captured_at=None, archive_capture_raw=None, archive_removed_raw=None)
        self.save(payload)
        record, archive = self.stored()
        self.assertIsNone(record['published_at'])
        self.assertEqual(record['published_raw'], 'unknown')
        self.assertIsNone(archive['archive_captured_at'])
        self.assertIsNone(archive['page_post_published_at'])
        self.assertIsNone(archive['archive_removed_raw'])

    def test_detail_insert_failure_and_caller_rollback_leave_no_partial_rows(self):
        def fail_constraint(connection, identity, archive, digest):
            _insert_archive(connection, identity, archive, 'invalid')
        with patch('app.import_trump_post._insert_archive', side_effect=fail_constraint):
            with self.assertRaises(IntegrityError):
                self.save()
        self.assertEqual(self.counts(), (0, 0))
        nested = self.connection.begin_nested()
        self.save()
        self.assertEqual(self.counts(), (1, 1))
        nested.rollback()
        self.assertEqual(self.counts(), (0, 0))

    def test_concurrent_imports_commit_one_complete_pair(self):
        # These isolated fixtures must be visible across connections. They are
        # removed in finally; all other tests use rolled-back transactions.
        source, person = uuid4(), uuid4()
        payload = extraction(source, person)
        try:
            with self.engine.begin() as connection:
                add_fixture(connection, source, person)
            def run_import(_):
                with self.engine.begin() as connection:
                    return save_post(connection, payload, SEEN, source_id=source, person_id=person)
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(run_import, range(3)))
            self.assertEqual(sorted(result['action'] for result in results), ['already_exists', 'already_exists', 'inserted'])
            self.assertEqual(len({result['record_id'] for result in results}), 1)
            with self.engine.connect() as connection:
                self.assertEqual(connection.execute(text(
                    'SELECT count(*) FROM public_records r JOIN archive_post_details a USING(record_id) WHERE r.source_id=:id'
                ), {'id': source}).scalar_one(), 1)
        finally:
            with self.engine.begin() as connection:
                connection.execute(text('DELETE FROM archive_post_details WHERE record_id IN (SELECT record_id FROM public_records WHERE source_id=:id)'), {'id': source})
                connection.execute(text('DELETE FROM public_records WHERE source_id=:id'), {'id': source})
                connection.execute(text('DELETE FROM person_sources WHERE source_id=:id'), {'id': source})
                connection.execute(text('DELETE FROM sources WHERE source_id=:id'), {'id': source})
                connection.execute(text('DELETE FROM people WHERE person_id=:id'), {'id': person})
