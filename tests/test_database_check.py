import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import psycopg

from app.check_database import check_database, main
from app.core.config import Settings


class DatabaseCheckTests(unittest.TestCase):
    def settings(self, password="test-only-secret"):
        return Settings(_env_file=None, db_password=password)

    @patch("app.check_database.psycopg.connect")
    def test_missing_password_does_not_connect(self, connect):
        with self.assertRaises(ValueError):
            check_database(self.settings(""))
        connect.assert_not_called()

    @patch("app.check_database.psycopg.connect")
    def test_read_only_query_closes_resources(self, connect):
        connection = connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("prismquant_political", "prismquant_app", "on")
        result = check_database(self.settings("special'@:#secret"))
        self.assertEqual(result["status"], "ok")
        self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
        self.assertTrue(connect.call_args.kwargs["autocommit"])
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 5)
        self.assertEqual(connect.call_args.kwargs["password"], "special'@:#secret")
        cursor.execute.assert_called_once_with(
            "SELECT current_database(), current_user, current_setting('transaction_read_only')"
        )
        connection.cursor.return_value.__exit__.assert_called_once()
        connect.return_value.__exit__.assert_called_once()

    @patch("app.check_database.Settings")
    @patch("app.check_database.psycopg.connect")
    def test_failure_does_not_print_driver_secrets(self, connect, settings):
        settings.return_value = self.settings()
        connect.side_effect = psycopg.OperationalError("password=test-only-secret")
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main()
        self.assertEqual(exit_code, 1)
        self.assertNotIn("test-only-secret", output.getvalue())

    def test_password_hidden_from_settings_representation(self):
        self.assertNotIn("test-only-secret", repr(self.settings()))


if __name__ == "__main__":
    unittest.main()
