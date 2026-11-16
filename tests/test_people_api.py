import os
import unittest
from datetime import datetime, timezone, timedelta
from uuid import UUID
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.main import create_app


def sample(number):
    return {
        "person_id": UUID(int=number), "name_original": "Sample Person",
        "name_zh": None, "aliases": [], "country_codes": [], "monitoring_enabled": False,
        "created_at": datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=8))),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


class PeopleApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment="test")))
        self.addCleanup(self.client.close)

    @patch("app.api.people.read_people", return_value=[])
    def test_empty_list_is_success(self, read):
        response = self.client.get("/api/v1/people")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"items": [], "next_cursor": None})

    @patch("app.api.people.read_people")
    def test_response_types_utc_and_pagination(self, read):
        read.return_value = [sample(1), sample(2)]
        response = self.client.get("/api/v1/people?limit=1")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["next_cursor"], str(UUID(int=1)))
        person = payload["items"][0]
        self.assertFalse(person["monitoring_enabled"])
        self.assertIsNone(person["name_zh"])
        self.assertEqual(person["created_at"], "2026-01-01T00:00:00Z")
        read.return_value = [sample(2)]
        response = self.client.get("/api/v1/people", params={"cursor": payload["next_cursor"], "limit": 1})
        self.assertIsNone(response.json()["next_cursor"])
        self.assertEqual(read.call_args.args[2], UUID(int=1))

    @patch("app.api.people.read_people")
    def test_invalid_parameters_do_not_query_database(self, read):
        for query in ("limit=0", "limit=101", "limit=abc", "cursor=invalid"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get("/api/v1/people?" + query).status_code, 422)
        read.assert_not_called()

    @patch("app.api.people.read_people")
    def test_database_failure_is_safe_and_health_still_works(self, read):
        read.side_effect = OperationalError("SELECT hidden", {}, Exception("password=DO_NOT_EXPOSE"))
        response = self.client.get("/api/v1/people")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "database_unavailable")
        self.assertNotIn("DO_NOT_EXPOSE", response.text)
        self.assertNotIn("SELECT", response.text)
        self.assertEqual(self.client.get("/health").status_code, 200)


@unittest.skipUnless(os.environ.get("PQ_RUN_DB_TESTS") == "1", "Set PQ_RUN_DB_TESTS=1 for PostgreSQL checks")
class PeopleApiDatabaseTests(unittest.TestCase):
    def test_detail_matches_list_and_missing_person_is_404(self):
        with TestClient(create_app()) as client:
            people = client.get("/api/v1/people").json()["items"]
            self.assertGreaterEqual(len(people), 2)
            for person in people:
                detail = client.get("/api/v1/people/" + person["person_id"])
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json(), person)
            missing = client.get("/api/v1/people/00000000-0000-0000-0000-000000000000")
            self.assertEqual(missing.status_code, 404)
            self.assertEqual(missing.json()["detail"]["code"], "person_not_found")

    def test_real_database_cursor_pages(self):
        with TestClient(create_app()) as client:
            first = client.get("/api/v1/people?limit=1")
            self.assertEqual(first.status_code, 200)
            payload = first.json()
            self.assertEqual(len(payload["items"]), 1)
            self.assertIsNotNone(payload["next_cursor"])
            second = client.get("/api/v1/people", params={"limit": 1, "cursor": payload["next_cursor"]})
            self.assertEqual(second.status_code, 200)
            self.assertEqual(len(second.json()["items"]), 1)
            self.assertNotEqual(payload["items"][0]["person_id"], second.json()["items"][0]["person_id"])


class PersonDetailTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(Settings(_env_file=None, environment="test")))
        self.addCleanup(self.client.close)

    @patch("app.api.people.read_person", return_value=sample(1))
    def test_success_returns_single_object_in_utc(self, read):
        response = self.client.get("/api/v1/people/" + str(UUID(int=1)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["person_id"], str(UUID(int=1)))
        self.assertEqual(response.json()["created_at"], "2026-01-01T00:00:00Z")
        self.assertFalse(response.json()["monitoring_enabled"])
        self.assertNotIn("items", response.json())
        self.assertEqual(read.call_args.args[1], UUID(int=1))

    @patch("app.api.people.read_person", return_value=None)
    def test_missing_person_is_404(self, read):
        response = self.client.get("/api/v1/people/" + str(UUID(int=1)))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "person_not_found")

    @patch("app.api.people.read_person")
    def test_invalid_uuid_is_422_without_query(self, read):
        response = self.client.get("/api/v1/people/not-a-uuid")
        self.assertEqual(response.status_code, 422)
        read.assert_not_called()

    @patch("app.api.people.read_person")
    def test_failure_is_503_not_404_and_hides_secrets(self, read):
        read.side_effect = OperationalError("SELECT secret", {}, Exception("password=DO_NOT_EXPOSE"))
        response = self.client.get("/api/v1/people/" + str(UUID(int=1)))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "database_unavailable")
        self.assertNotIn("DO_NOT_EXPOSE", response.text)
        self.assertNotIn("SELECT", response.text)


if __name__ == "__main__":
    unittest.main()
