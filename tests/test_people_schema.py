"""Opt-in PostgreSQL checks; all test records are rolled back."""

import os
import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.database import database_url


@unittest.skipUnless(os.environ.get("PQ_RUN_DB_TESTS") == "1", "Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks")
class PeopleSchemaTests(unittest.TestCase):
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

    def test_columns_and_primary_key(self):
        inspector = inspect(self.connection)
        columns = {c["name"]: c for c in inspector.get_columns("people")}
        self.assertEqual(set(columns), {
            "person_id", "name_original", "name_zh", "aliases", "country_codes",
            "monitoring_enabled", "created_at", "updated_at",
        })
        self.assertTrue(columns["name_zh"]["nullable"])
        self.assertFalse(columns["name_original"]["nullable"])
        self.assertEqual(inspector.get_pk_constraint("people")["constrained_columns"], ["person_id"])

    def test_defaults_and_duplicate_names(self):
        query = text("INSERT INTO people (name_original) VALUES (:name) RETURNING *")
        first = self.connection.execute(query, {"name": "Temporary Test Person"}).mappings().one()
        second = self.connection.execute(query, {"name": "Temporary Test Person"}).mappings().one()
        self.assertNotEqual(first["person_id"], second["person_id"])
        self.assertFalse(first["monitoring_enabled"])
        self.assertEqual(first["aliases"], [])
        self.assertEqual(first["country_codes"], [])
        self.assertIsNone(first["name_zh"])
        self.assertIsNotNone(first["created_at"].tzinfo)

    def test_unicode_arrays_and_update_timestamp(self):
        person_id = self.connection.execute(text(
            "INSERT INTO people (name_original, name_zh, aliases, country_codes) "
            "VALUES (:name, :zh, :aliases, :countries) RETURNING person_id"
        ), {"name": "Temporary Test Person", "zh": "临时测试人物", "aliases": ["测试别名"], "countries": ["US", "FR"]}).scalar_one()
        row = self.connection.execute(text(
            "UPDATE people SET monitoring_enabled=true, updated_at=:old "
            "WHERE person_id=:id RETURNING name_zh, aliases, country_codes, updated_at"
        ), {"old": datetime(2000, 1, 1, tzinfo=timezone.utc), "id": person_id}).one()
        self.assertEqual(row[0], "临时测试人物")
        self.assertEqual(row[1], ["测试别名"])
        self.assertEqual(row[2], ["US", "FR"])
        self.assertGreater(row[3].year, 2000)

    def test_blank_required_name_rejected(self):
        for name in (None, "", "   "):
            with self.subTest(name=name):
                with self.assertRaises(IntegrityError):
                    with self.connection.begin_nested():
                        self.connection.execute(text("INSERT INTO people (name_original) VALUES (:name)"), {"name": name})

    def test_null_array_elements_rejected(self):
        with self.assertRaises(IntegrityError):
            with self.connection.begin_nested():
                self.connection.execute(text("INSERT INTO people (name_original, aliases) VALUES ('Temporary Test Person', ARRAY[NULL]::text[])"))


if __name__ == "__main__":
    unittest.main()
