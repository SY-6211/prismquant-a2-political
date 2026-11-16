import hashlib
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import create_read_engine, database_url
from app.main import create_app
from app.repositories.public_records import select_public_record
from test_public_records_api import sample


def detail_sample():
    body = '完整测试正文。\n\nSynthetic speech paragraph. ' * 100
    return {
        **sample(1), 'body_text': body,
        'speaker_evidence': 'Statement by Test Person',
        'content_hash': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'published_raw': 'Sat, 17 Oct 2026 08:00:00 +0800',
    }


class PublicRecordDetailTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment='test')))
        self.addCleanup(self.client.close)
        self.url = '/api/v1/public-records/' + str(UUID(int=1))

    @patch('app.api.public_records.read_public_record')
    def test_success_preserves_full_body_evidence_hash_and_normalizes_utc(self, read):
        row = detail_sample()
        read.return_value = row
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIsNone(result['archive_post'])
        self.assertEqual(result['body_text'], row['body_text'])
        self.assertGreater(len(result['body_text']), 1200)
        self.assertEqual(result['speaker_evidence'], row['speaker_evidence'])
        self.assertEqual(result['content_hash'], hashlib.sha256(result['body_text'].encode('utf-8')).hexdigest())
        self.assertEqual(result['published_raw'], row['published_raw'])
        self.assertEqual(result['published_at'], '2026-10-17T00:00:00Z')
        self.assertEqual(result['page_date'], '2026-10-17')
        self.assertEqual(read.call_args.args[1], UUID(int=1))

    @patch('app.api.public_records.read_public_record')
    def test_unknown_person_and_times_do_not_invent_values(self, read):
        nullable = ('person_id', 'person_name_original', 'person_name_zh', 'published_at',
                    'published_raw', 'page_date', 'speaker_evidence', 'original_url', 'language')
        read.return_value = {**detail_sample(), **dict.fromkeys(nullable), 'verification_status': 'needs_review'}
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for field in nullable:
            self.assertIsNone(response.json()[field])

    @patch('app.api.public_records.read_public_record', return_value=None)
    def test_missing_record_is_404(self, read):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'detail': {
            'code': 'public_record_not_found', 'message': '未找到该内容记录。',
        }})

    @patch('app.api.public_records.read_public_record')
    def test_invalid_uuid_is_422_without_database_access(self, read):
        for value in ('abc', '1', "x' OR '1'='1"):
            self.assertEqual(self.client.get('/api/v1/public-records/' + value).status_code, 422)
        read.assert_not_called()

    @patch('app.api.public_records.read_public_record')
    def test_database_failure_is_redacted_503_not_404(self, read):
        for failure in (OperationalError('SELECT private', {}, Exception('password=DO_NOT_EXPOSE')),
                        ValueError('DO_NOT_EXPOSE')):
            read.side_effect = failure
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['detail']['code'], 'database_unavailable')
            self.assertNotIn('DO_NOT_EXPOSE', response.text)
            self.assertNotIn('SELECT', response.text)
        documented = self.client.get('/openapi.json').json()['paths']['/api/v1/public-records/{record_id}']['get']
        self.assertTrue({'200', '404', '422', '503'}.issubset(documented['responses']))
        self.assertEqual(self.client.get('/health').status_code, 200)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class PublicRecordDetailDatabaseTests(unittest.TestCase):
    def test_saved_detail_matches_database_list_and_hash_without_writing(self):
        engine = create_read_engine(Settings())
        try:
            with engine.connect() as connection:
                before = [dict(row) for row in connection.execute(text(
                    'SELECT * FROM public_records ORDER BY record_id'
                )).mappings()]
                self.assertEqual(connection.execute(text('SHOW transaction_read_only')).scalar_one(), 'on')
            with TestClient(create_app()) as client:
                item = next(row for row in client.get('/api/v1/public-records').json()['items']
                            if row['title'] == 'IMFC Statement')
                response = client.get('/api/v1/public-records/' + item['record_id'])
                self.assertEqual(response.status_code, 200)
                result = response.json()
                for field, value in item.items():
                    self.assertEqual(result[field], value)
                stored = next(row for row in before if str(row['record_id']) == item['record_id'])
                for field in ('body_text', 'speaker_evidence', 'published_raw', 'content_hash'):
                    self.assertEqual(result[field], stored[field])
                self.assertGreater(len(result['body_text']), 1200)
                self.assertEqual(result['content_hash'], hashlib.sha256(result['body_text'].encode('utf-8')).hexdigest())
                self.assertEqual(client.get('/api/v1/public-records/' + str(uuid4())).status_code, 404)
            with engine.connect() as connection:
                after = [dict(row) for row in connection.execute(text(
                    'SELECT * FROM public_records ORDER BY record_id'
                )).mappings()]
                self.assertEqual(before, after)
        finally:
            engine.dispose()

    def test_unattributed_record_is_returned_with_snapshot_and_null_metadata(self):
        engine = create_engine(database_url(Settings()), hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    source = connection.execute(text(
                        "INSERT INTO sources (name, publisher_name, provenance_type, feed_url) "
                        "VALUES ('Temporary detail source', 'Test publisher', 'official', 'https://example.invalid/feed') "
                        'RETURNING source_id'
                    )).scalar_one()
                    body = 'Synthetic unattributed content. ' * 100
                    record = connection.execute(text(
                        'INSERT INTO public_records (source_id, title, provenance_type, retrieved_url, body_text, content_hash) '
                        "VALUES (:source, 'Synthetic detail', 'official', 'https://example.invalid/detail', :body, :hash) "
                        'RETURNING record_id'
                    ), {'source': source, 'body': body, 'hash': hashlib.sha256(body.encode('utf-8')).hexdigest()}).scalar_one()
                    connection.execute(text(
                        "UPDATE sources SET provenance_type='third_party_archive' WHERE source_id=:source"
                    ), {'source': source})
                    with patch('app.api.public_records.read_public_record',
                               side_effect=lambda settings, identity: select_public_record(connection, identity)):
                        with TestClient(create_app()) as client:
                            response = client.get('/api/v1/public-records/' + str(record))
                    self.assertEqual(response.status_code, 200)
                    result = response.json()
                    self.assertEqual(result['body_text'], body)
                    self.assertEqual(result['provenance_type'], 'official')
                    self.assertEqual(result['verification_status'], 'needs_review')
                    for field in ('person_id', 'person_name_original', 'person_name_zh', 'speaker_evidence',
                                  'published_at', 'published_raw', 'page_date', 'language', 'original_url'):
                        self.assertIsNone(result[field])
                    self.assertIsNone(select_public_record(connection, uuid4()))
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
