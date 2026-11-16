"""Opt-in PostgreSQL checks; temporary people are always rolled back."""

import os
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.database import database_url
from app.seed_people import seed_people


@unittest.skipUnless(os.environ.get("PQ_RUN_DB_TESTS") == "1", "Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks")
class SeedPeopleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(database_url(Settings()), hide_parameters=True)
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.person = {
            "person_id": uuid4(), "name_original": "Temporary seed " + str(uuid4()),
            "name_zh": "临时导入测试", "aliases": ["测试别名"], "country_codes": ["US"],
        }

    def tearDown(self):
        self.transaction.rollback()
        self.connection.close()
        self.engine.dispose()

    def test_repeat_preserves_identity_and_user_edits(self):
        self.assertEqual(seed_people(self.connection, [self.person]), {"inserted": 1, "skipped": 0})
        row = self.connection.execute(text("SELECT monitoring_enabled FROM people WHERE person_id=:id"), {"id": self.person["person_id"]}).one()
        self.assertFalse(row[0])
        self.connection.execute(text("UPDATE people SET name_zh='用户修正姓名' WHERE person_id=:id"), {"id": self.person["person_id"]})
        self.assertEqual(seed_people(self.connection, [self.person]), {"inserted": 0, "skipped": 1})
        name = self.connection.execute(text("SELECT name_zh FROM people WHERE person_id=:id"), {"id": self.person["person_id"]}).scalar_one()
        self.assertEqual(name, "用户修正姓名")

    def test_same_name_different_identity_requires_review(self):
        seed_people(self.connection, [self.person])
        with self.assertRaises(ValueError):
            seed_people(self.connection, [{**self.person, "person_id": uuid4()}])
