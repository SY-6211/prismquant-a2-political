import hashlib
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import create_read_engine, database_url
from app.main import create_app
from app.repositories.public_records import select_public_records


def sample(number):
    return {
        'record_id': UUID(int=number), 'source_id': UUID(int=20), 'source_name': 'Test source',
        'publisher_name': 'Publisher', 'person_id': UUID(int=30),
        'person_name_original': 'Test Person', 'person_name_zh': None,
        'title': 'Test statement', 'content_type': 'public_statement', 'provenance_type': 'official',
        'original_url': 'https://example.invalid/statement', 'retrieved_url': 'https://example.invalid/statement',
        'published_at': datetime(2026, 10, 17, 8, tzinfo=timezone(timedelta(hours=8))),
        'page_date': date(2026, 10, 17), 'first_seen_at': datetime(2026, 10, 17, tzinfo=timezone.utc),
        'language': 'en', 'verification_status': 'verified_page_attribution',
        'created_at': datetime(2026, 10, 17, tzinfo=timezone.utc),
    }


class PublicRecordsApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment='test')))
        self.addCleanup(self.client.close)

    @patch('app.api.public_records.read_public_records', return_value=[])
    def test_empty_and_unmatched_filter_return_200(self, read):
        for params in ({}, {'person_id': str(uuid4())}):
            response = self.client.get('/api/v1/public-records', params=params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'items': [], 'next_cursor': None})

    @patch('app.api.public_records.read_public_records')
    def test_summary_utc_dates_nulls_and_no_body_exposure(self, read):
        record = {**sample(1), 'body_text': 'Do not expose full body', 'speaker_evidence': 'Do not expose evidence'}
        read.return_value = [record]
        response = self.client.get('/api/v1/public-records')
        self.assertEqual(response.status_code, 200)
        item = response.json()['items'][0]
        self.assertEqual(item['published_at'], '2026-10-17T00:00:00Z')
        self.assertEqual(item['page_date'], '2026-10-17')
        self.assertEqual(item['verification_status'], 'verified_page_attribution')
        self.assertEqual(item['record_id'], str(UUID(int=1)))
        for name in ('body_text', 'body_preview', 'speaker_evidence', 'content_hash', 'published_raw', 'archive_post'):
            self.assertNotIn(name, item)
        record.update(person_id=None, person_name_original=None, published_at=None, page_date=None,
                      original_url=None, language=None, verification_status='needs_review')
        item = self.client.get('/api/v1/public-records').json()['items'][0]
        for name in ('person_id', 'person_name_original', 'published_at', 'page_date', 'original_url', 'language'):
            self.assertIsNone(item[name])

    @patch('app.api.public_records.read_public_records')
    def test_cursor_lookahead_and_person_filter_are_forwarded(self, read):
        person = UUID(int=30)
        read.return_value = [sample(1), sample(2)]
        first = self.client.get('/api/v1/public-records', params={'limit': 1, 'person_id': str(person)}).json()
        self.assertEqual(len(first['items']), 1)
        self.assertEqual(first['next_cursor'], str(UUID(int=1)))
        self.assertEqual(read.call_args.args[1:], (1, None, person))
        read.return_value = [sample(2)]
        second = self.client.get('/api/v1/public-records', params={
            'limit': 1, 'person_id': str(person), 'cursor': first['next_cursor'],
        }).json()
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(read.call_args.args[1:], (1, UUID(int=1), person))

    @patch('app.api.public_records.read_public_records')
    def test_invalid_parameters_do_not_query_database(self, read):
        for query in ('limit=0', 'limit=101', 'limit=abc', 'cursor=abc', 'person_id=abc', 'person_id='):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/v1/public-records?' + query).status_code, 422)
        read.assert_not_called()

    @patch('app.api.public_records.read_public_records')
    def test_database_failure_is_redacted_503_with_documented_response(self, read):
        for failure in (OperationalError('SELECT private', {}, Exception('password=DO_NOT_EXPOSE')),
                        ValueError('DO_NOT_EXPOSE')):
            read.side_effect = failure
            response = self.client.get('/api/v1/public-records')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['detail']['code'], 'database_unavailable')
            self.assertNotIn('DO_NOT_EXPOSE', response.text)
            self.assertNotIn('SELECT', response.text)
        self.assertEqual(self.client.get('/health').status_code, 200)
        documented = self.client.get('/openapi.json').json()['paths']['/api/v1/public-records']['get']
        self.assertIn('503', documented['responses'])


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class PublicRecordsApiDatabaseTests(unittest.TestCase):
    def test_saved_speech_and_person_filter_use_read_only_connection(self):
        settings = Settings()
        engine = create_read_engine(settings)
        try:
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SHOW transaction_read_only')).scalar_one(), 'on')
                before = connection.execute(text('SELECT count(*) FROM public_records')).scalar_one()
            with TestClient(create_app(settings)) as client:
                response = client.get('/api/v1/public-records')
                self.assertEqual(response.status_code, 200)
                items = response.json()['items']
                speech = next(item for item in items if item['title'] == 'IMFC Statement')
                self.assertEqual(speech['person_name_original'], 'Christine Lagarde')
                self.assertEqual(speech['publisher_name'], 'European Central Bank')
                self.assertEqual(speech['provenance_type'], 'official')
                self.assertEqual(speech['content_type'], 'public_statement')
                self.assertNotIn('body_text', speech)
                filtered = client.get('/api/v1/public-records', params={'person_id': speech['person_id']}).json()
                self.assertIn(speech, filtered['items'])
                empty = client.get('/api/v1/public-records', params={'person_id': str(uuid4())}).json()
                self.assertEqual(empty, {'items': [], 'next_cursor': None})
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text('SELECT count(*) FROM public_records')).scalar_one(), before)
        finally:
            engine.dispose()

    def test_pagination_filters_before_limit_and_keeps_unattributed_records(self):
        engine = create_engine(database_url(Settings()), hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    source = connection.execute(text(
                        "INSERT INTO sources (name, publisher_name, provenance_type, feed_url) "
                        "VALUES ('Temporary list source', 'Synthetic publisher', 'official', 'https://example.invalid/feed') "
                        'RETURNING source_id'
                    )).scalar_one()
                    person = connection.execute(text(
                        "INSERT INTO people (name_original) VALUES ('Temporary list person') RETURNING person_id"
                    )).scalar_one()
                    for number, owner in ((1, None), (2, person), (3, person)):
                        connection.execute(text(
                            'INSERT INTO public_records (record_id, source_id, person_id, title, provenance_type, '
                            'retrieved_url, body_text, content_hash) '
                            "VALUES (:id, :source, :person, 'Synthetic summary', 'official', :url, :body, :hash)"
                        ), {'id': UUID(int=number), 'source': source, 'person': owner,
                            'url': 'https://example.invalid/' + str(number), 'body': 'Synthetic full body',
                            'hash': hashlib.sha256(b'Synthetic full body').hexdigest()})
                    rows = select_public_records(connection, 1, None, None)
                    self.assertEqual([r['record_id'] for r in rows], [UUID(int=1), UUID(int=2)])
                    self.assertIsNone(rows[0]['person_id'])
                    self.assertIsNone(rows[0]['person_name_original'])
                    self.assertIsNone(rows[0]['published_at'])
                    filtered = select_public_records(connection, 1, None, person)
                    self.assertEqual([r['record_id'] for r in filtered], [UUID(int=2), UUID(int=3)])
                    last = select_public_records(connection, 1, filtered[0]['record_id'], person)
                    self.assertEqual([r['record_id'] for r in last], [UUID(int=3)])
                    self.assertNotIn('body_text', last[0])
                    self.assertEqual(select_public_records(connection, 1, UUID(int=3), person), [])
                    # The stored category must survive a source setting change.
                    connection.execute(text(
                        "UPDATE sources SET provenance_type='third_party_archive' WHERE source_id=:id"
                    ), {'id': source})
                    self.assertEqual(select_public_records(connection, 1, None, person)[0]['provenance_type'], 'official')
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
