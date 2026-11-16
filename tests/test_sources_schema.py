"""Opt-in local PostgreSQL checks, rolled back without contacting any feed."""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.database import database_url


@unittest.skipUnless(os.environ.get("PQ_RUN_DB_TESTS") == "1", "Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks")
class SourcesSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(database_url(Settings()), hide_parameters=True)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()

    def source(self, **changes):
        values = {
            "name": "临时来源测试", "publisher_name": "Temporary Publisher",
            "provenance_type": "official", "fetch_method": "rss",
            "feed_url": "https://example.invalid/feed", "homepage_url": None,
            "poll_interval_seconds": 300,
        }
        values.update(changes)
        return self.connection.execute(text(
            "INSERT INTO sources (name, publisher_name, provenance_type, fetch_method, feed_url, homepage_url, poll_interval_seconds) "
            "VALUES (:name, :publisher_name, :provenance_type, :fetch_method, :feed_url, :homepage_url, :poll_interval_seconds) RETURNING *"
        ), values).mappings().one()

    def person(self):
        return self.connection.execute(text(
            "INSERT INTO people (name_original) VALUES ('Temporary source test person') RETURNING person_id"
        )).scalar_one()

    def link(self, person_id, source_id):
        return self.connection.execute(text(
            "INSERT INTO person_sources (person_id, source_id) VALUES (:person, :source) RETURNING enabled"
        ), {"person": person_id, "source": source_id}).scalar_one()

    def test_columns_keys_and_foreign_keys(self):
        inspector = inspect(self.connection)
        self.assertEqual({c['name'] for c in inspector.get_columns('sources')}, {
            'source_id', 'name', 'publisher_name', 'provenance_type', 'fetch_method',
            'feed_url', 'homepage_url', 'poll_interval_seconds', 'enabled', 'created_at', 'updated_at',
        })
        self.assertEqual({c['name'] for c in inspector.get_columns('person_sources')}, {'person_id', 'source_id', 'enabled'})
        self.assertEqual(inspector.get_pk_constraint('person_sources')['constrained_columns'], ['person_id', 'source_id'])
        self.assertEqual({f['referred_table'] for f in inspector.get_foreign_keys('person_sources')}, {'people', 'sources'})

    def test_defaults_are_disabled_rss_and_300_seconds(self):
        row = self.connection.execute(text(
            "INSERT INTO sources (name, publisher_name, provenance_type, feed_url) "
            "VALUES ('Temporary', 'Publisher', 'third_party_archive', 'https://example.invalid/feed') RETURNING *"
        )).mappings().one()
        self.assertFalse(row['enabled'])
        self.assertEqual(row['fetch_method'], 'rss')
        self.assertEqual(row['poll_interval_seconds'], 300)
        self.assertIsNotNone(row['source_id'])
        self.assertIsNotNone(row['created_at'].tzinfo)

    def test_invalid_source_values_are_rejected(self):
        for changes in (
            {'name': ''}, {'publisher_name': '   '}, {'provenance_type': 'unverified'},
            {'fetch_method': 'scrape'}, {'poll_interval_seconds': 0},
            {'poll_interval_seconds': -1}, {'feed_url': 'file:///local'},
            {'homepage_url': 'not-a-url'}, {'feed_url': None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(IntegrityError):
                    with self.connection.begin_nested():
                        self.source(**changes)

    def test_many_to_many_links_are_disabled_and_unique(self):
        a, b = self.person(), self.person()
        x, y = self.source()['source_id'], self.source()['source_id']
        self.assertFalse(self.link(a, x))
        self.assertFalse(self.link(a, y))
        self.assertFalse(self.link(b, x))
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.link(a, x)

    def test_missing_references_and_deletion_are_rejected(self):
        person, source = self.person(), self.source()['source_id']
        for p, s in ((uuid4(), source), (person, uuid4())):
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.link(p, s)
        self.link(person, source)
        for query, identity in (
            ('DELETE FROM people WHERE person_id=:id', person),
            ('DELETE FROM sources WHERE source_id=:id', source),
        ):
            with self.assertRaises(IntegrityError):
                with self.connection.begin_nested():
                    self.connection.execute(text(query), {'id': identity})

    def test_updated_at_is_refreshed_by_database(self):
        source = self.source()['source_id']
        updated = self.connection.execute(text(
            "UPDATE sources SET name='修改后的临时来源', updated_at='2000-01-01T00:00:00Z' "
            "WHERE source_id=:id RETURNING updated_at"
        ), {'id': source}).scalar_one()
        self.assertGreater(updated.year, 2000)


if __name__ == '__main__':
    unittest.main()
