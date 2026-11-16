import hashlib
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.database import database_url
from app.main import create_app
from app.repositories.public_records import select_public_records


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None)))
        self.addCleanup(self.client.close)

    def test_frontend_and_assets_are_served(self):
        response = self.client.get('/monitor/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('人物动态', response.text)
        for path in ('styles.css', 'app.js', 'api.js', 'realtime.js', 'analysis.js', 'chat.js', 'chat-api.js', 'catalog.js'):
            self.assertEqual(self.client.get('/monitor/' + path).status_code, 200)
        self.assertEqual(self.client.get('/monitor/.env').status_code, 404)
        self.assertEqual(self.client.get('/monitor/../.env').status_code, 404)

    @patch('app.api.public_records.read_public_records', return_value=[])
    def test_new_sort_is_forwarded_with_filter_and_cursor(self, read):
        person, cursor = uuid4(), uuid4()
        response = self.client.get('/api/v1/public-records', params={
            'person_id': str(person), 'cursor': str(cursor), 'order': 'published_desc', 'limit': 5,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read.call_args.args[1:], (5, cursor, person, 'published_desc'))
        self.assertEqual(self.client.get('/api/v1/public-records?order=invalid').status_code, 422)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Explicit database checks only')
class TimelineDatabaseTests(unittest.TestCase):
    def test_timestamp_ties_nulls_filter_and_insertion_between_pages(self):
        engine = create_engine(database_url(Settings()), hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    source = connection.execute(text(
                        "INSERT INTO sources (name,publisher_name,provenance_type,feed_url) "
                        "VALUES ('Timeline test','Test','official','https://example.invalid/timeline') RETURNING source_id"
                    )).scalar_one()
                    person = connection.execute(text("INSERT INTO people (name_original) VALUES ('Timeline test') RETURNING person_id")).scalar_one()
                    def insert(number, published, owner=person):
                        connection.execute(text(
                            'INSERT INTO public_records (record_id,source_id,person_id,title,provenance_type,'
                            'retrieved_url,body_text,content_hash,published_at) '
                            "VALUES (:id,:source,:person,'Timeline','official',:url,'Test',:hash,:published)"
                        ), {'id': UUID(int=number), 'source': source, 'person': owner,
                            'url': f'https://example.invalid/timeline/{number}',
                            'hash': hashlib.sha256(b'Test').hexdigest(), 'published': published})
                    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
                    insert(101, None); insert(102, stamp); insert(103, stamp); insert(104, None); insert(105, stamp, None)
                    first = select_public_records(connection, 1, None, person, 'published_desc')
                    self.assertEqual([row['record_id'] for row in first], [UUID(int=103), UUID(int=102)])
                    insert(106, datetime(2026, 2, 1, tzinfo=timezone.utc))
                    second = select_public_records(connection, 2, UUID(int=103), person, 'published_desc')
                    self.assertEqual([row['record_id'] for row in second], [UUID(int=102), UUID(int=104), UUID(int=101)])
                    last = select_public_records(connection, 2, UUID(int=104), person, 'published_desc')
                    self.assertEqual([row['record_id'] for row in last], [UUID(int=101)])
                    self.assertEqual(select_public_records(connection, 2, UUID(int=101), person, 'published_desc'), [])
                    self.assertEqual(select_public_records(connection, 2, UUID(int=105), person, 'published_desc'), [])
                    self.assertEqual(select_public_records(connection, 1, None, person, 'published_desc')[0]['record_id'], UUID(int=106))
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()
