"""Browser integration boundaries, without a database or source requests."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app


class CorsTests(unittest.TestCase):
    def client(self, origins):
        client = TestClient(create_app(Settings(_env_file=None, cors_origins=origins)))
        self.addCleanup(client.close)
        return client

    def test_default_has_no_cross_origin_permission(self):
        response = self.client([]).get('/health', headers={'Origin': 'http://localhost:5173'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_exact_origin_and_preflight(self):
        client = self.client(['http://localhost:5173'])
        response = client.get('/health', headers={'Origin': 'http://localhost:5173'})
        self.assertEqual(response.headers['access-control-allow-origin'], 'http://localhost:5173')
        self.assertNotIn('access-control-allow-credentials', response.headers)
        response = client.options('/api/v1/people', headers={
            'Origin': 'http://localhost:5173',
            'Access-Control-Request-Method': 'GET',
            'Access-Control-Request-Headers': 'content-type',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['access-control-allow-methods'], 'GET')

    def test_different_host_or_port_is_not_allowed(self):
        client = self.client(['http://localhost:5173'])
        for origin in ['http://127.0.0.1:5173', 'http://localhost:5174', 'https://example.com']:
            with self.subTest(origin=origin):
                response = client.get('/health', headers={'Origin': origin})
                self.assertNotIn('access-control-allow-origin', response.headers)

    def test_write_preflight_is_rejected(self):
        response = self.client(['http://localhost:5173']).options('/api/v1/people', headers={
            'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': 'POST',
        })
        self.assertEqual(response.status_code, 400)

    def test_invalid_origins_fail_configuration(self):
        for origin in ['*', 'null', 'file:///tmp', 'https://example.com/',
                       'https://user@example.com', 'https://example.com?q=1',
                       'https://example.com#x', 'http://localhost:99999',
                       'https://*.example.com', ' http://localhost:5173']:
            with self.subTest(origin=origin), self.assertRaises(ValidationError):
                Settings(_env_file=None, cors_origins=[origin])

    def test_environment_json_and_duplicate_origins(self):
        with patch.dict(os.environ, {'PQ_CORS_ORIGINS': '["http://localhost:5173","http://localhost:5173"]'}):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.cors_origins, ['http://localhost:5173'])


if __name__ == '__main__':
    unittest.main()
