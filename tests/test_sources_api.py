import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.database import database_url
from app.main import create_app
from app.repositories.sources import select_sources


def source_sample(number):
    return {
        'source_id': UUID(int=number), 'name': 'Sample source', 'publisher_name': 'Publisher',
        'provenance_type': 'official', 'fetch_method': 'rss', 'feed_url': 'https://example.invalid/feed',
        'homepage_url': None, 'poll_interval_seconds': 300, 'enabled': False,
        'created_at': datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))),
        'updated_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
        'people': [{'person_id': str(UUID(int=10)), 'name_original': 'Sample person',
                    'name_zh': None, 'monitoring_enabled': True, 'link_enabled': False}],
    }


class SourcesApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment='test')))
        self.addCleanup(self.client.close)

    @patch('app.api.sources.read_sources', return_value=[])
    def test_empty_list_is_200(self, read):
        response = self.client.get('/api/v1/sources')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'items': [], 'next_cursor': None})

    @patch('app.api.sources.read_sources')
    def test_types_independent_flags_utc_and_cursor(self, read):
        read.return_value = [source_sample(1), source_sample(2)]
        response = self.client.get('/api/v1/sources?limit=1')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['items']), 1)
        self.assertEqual(body['next_cursor'], str(UUID(int=1)))
        source = body['items'][0]
        self.assertEqual(source['created_at'], '2026-01-01T00:00:00Z')
        self.assertIsNone(source['homepage_url'])
        self.assertFalse(source['enabled'])
        self.assertTrue(source['people'][0]['monitoring_enabled'])
        self.assertFalse(source['people'][0]['link_enabled'])
        next_source = source_sample(2)
        next_source['people'] = []
        read.return_value = [next_source]
        second = self.client.get('/api/v1/sources', params={'limit': 1, 'cursor': body['next_cursor']})
        self.assertIsNone(second.json()['next_cursor'])
        self.assertEqual(second.json()['items'][0]['people'], [])
        self.assertEqual(read.call_args.args[2], UUID(int=1))

    @patch('app.api.sources.read_sources')
    def test_invalid_parameters_never_query_database(self, read):
        for query in ('limit=0', 'limit=101', 'limit=abc', 'cursor=invalid'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get('/api/v1/sources?' + query).status_code, 422)
        read.assert_not_called()

    @patch('app.api.sources.read_sources')
    def test_database_error_is_redacted_503(self, read):
        read.side_effect = OperationalError('SELECT secret', {}, Exception('password=DO_NOT_EXPOSE'))
        response = self.client.get('/api/v1/sources')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['code'], 'database_unavailable')
        self.assertNotIn('DO_NOT_EXPOSE', response.text)
        self.assertNotIn('SELECT', response.text)
        self.assertEqual(self.client.get('/health').status_code, 200)


@unittest.skipUnless(os.environ.get('PQ_RUN_DB_TESTS') == '1', 'Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks')
class SourcesApiDatabaseTests(unittest.TestCase):
    def test_real_api_associations_and_pagination(self):
        with TestClient(create_app()) as client:
            response = client.get('/api/v1/sources')
            self.assertEqual(response.status_code, 200)
            items = response.json()['items']
            self.assertEqual(len(items), 2)
            expected = {'official': 'Christine Lagarde', 'third_party_archive': 'Donald J. Trump'}
            for source in items:
                self.assertFalse(source['enabled'])
                self.assertEqual(source['poll_interval_seconds'], 300)
                self.assertEqual(len(source['people']), 1)
                person = source['people'][0]
                self.assertEqual(person['name_original'], expected[source['provenance_type']])
                self.assertFalse(person['monitoring_enabled'])
                self.assertFalse(person['link_enabled'])
            first = client.get('/api/v1/sources?limit=1').json()
            self.assertIsNotNone(first['next_cursor'])
            second = client.get('/api/v1/sources', params={'limit': 1, 'cursor': first['next_cursor']}).json()
            self.assertIsNone(second['next_cursor'])
            self.assertNotEqual(first['items'][0]['source_id'], second['items'][0]['source_id'])

    def test_paging_keeps_all_people_and_unlinked_sources(self):
        engine = create_engine(database_url(Settings()), hide_parameters=True)
        try:
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    for number in (1, 2):
                        connection.execute(text(
                            "INSERT INTO sources (source_id, name, publisher_name, provenance_type, feed_url) "
                            "VALUES (:id, 'Temporary source API test', 'Publisher', 'official', 'https://example.invalid/feed')"
                        ), {'id': UUID(int=number)})
                    person_ids = []
                    for number in (1, 2):
                        person = connection.execute(text(
                            "INSERT INTO people (name_original) VALUES ('Temporary source API person') RETURNING person_id"
                        )).scalar_one()
                        person_ids.append(str(person))
                        connection.execute(text(
                            'INSERT INTO person_sources (person_id, source_id) VALUES (:person, :source)'
                        ), {'person': person, 'source': UUID(int=1)})
                    rows = select_sources(connection, 1, None)
                    self.assertEqual(len(rows), 2)  # one result plus lookahead
                    self.assertEqual(rows[0]['source_id'], UUID(int=1))
                    self.assertEqual({p['person_id'] for p in rows[0]['people']}, set(person_ids))
                    self.assertEqual(rows[1]['source_id'], UUID(int=2))
                    self.assertEqual(rows[1]['people'], [])
                    next_rows = select_sources(connection, 1, UUID(int=1))
                    self.assertEqual(next_rows[0]['source_id'], UUID(int=2))
                finally:
                    transaction.rollback()
        finally:
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
