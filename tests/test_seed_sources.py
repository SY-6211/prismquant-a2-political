"""Opt-in database tests: all fixtures roll back, with no network requests."""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.database import database_url
from app.seed_sources import seed_sources


@unittest.skipUnless(os.environ.get("PQ_RUN_DB_TESTS") == "1", "Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks")
class SeedSourcesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(database_url(Settings()), hide_parameters=True)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        person_id = self.connection.execute(text(
            "INSERT INTO people (name_original) VALUES ('Temporary source seed person') RETURNING person_id"
        )).scalar_one()
        self.source = {
            "source_id": uuid4(), "person_id": person_id, "name": "Temporary seed source",
            "publisher_name": "Test Publisher", "provenance_type": "third_party_archive", "fetch_method": "rss",
            "feed_url": "https://example.invalid/" + str(uuid4()), "homepage_url": "https://example.invalid/about",
            "poll_interval_seconds": 300,
        }

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def test_repeat_keeps_ids_user_edits_and_flags(self):
        first = seed_sources(self.connection, [self.source])
        self.assertEqual(first['sources_inserted'], 1)
        self.assertEqual(first['links_inserted'], 1)
        row = self.connection.execute(text(
            "SELECT s.enabled, ps.enabled FROM sources s JOIN person_sources ps USING(source_id) WHERE s.source_id=:source_id"
        ), self.source).one()
        self.assertEqual(tuple(row), (False, False))
        self.connection.execute(text(
            "UPDATE sources SET name='User edited', poll_interval_seconds=600, enabled=true WHERE source_id=:source_id"
        ), self.source)
        self.connection.execute(text("UPDATE person_sources SET enabled=true WHERE source_id=:source_id"), self.source)
        second = seed_sources(self.connection, [self.source])
        self.assertEqual(second, {'sources_inserted': 0, 'sources_skipped': 1, 'links_inserted': 0, 'links_skipped': 1})
        row = self.connection.execute(text(
            "SELECT s.name, s.poll_interval_seconds, s.enabled, ps.enabled FROM sources s JOIN person_sources ps USING(source_id) WHERE s.source_id=:source_id"
        ), self.source).one()
        self.assertEqual(tuple(row), ('User edited', 600, True, True))

    def test_missing_person_prevents_partial_import(self):
        missing = {**self.source, 'source_id': uuid4(), 'person_id': uuid4()}
        with self.assertRaises(ValueError):
            seed_sources(self.connection, [self.source, missing])
        count = self.connection.execute(text('SELECT count(*) FROM sources WHERE source_id=:source_id'), self.source).scalar_one()
        self.assertEqual(count, 0)

    def test_conflicting_feed_url_requires_manual_review(self):
        seed_sources(self.connection, [self.source])
        with self.assertRaises(ValueError):
            seed_sources(self.connection, [{**self.source, 'source_id': uuid4()}])
