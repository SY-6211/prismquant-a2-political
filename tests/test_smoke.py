"""Verify the first task's public contract and configuration boundary."""

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.main import create_app


class BackendSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(
            create_app(Settings(_env_file=None, environment="test"))
        )
        self.addCleanup(self.client.close)

    def test_health_describes_process_only(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["scope"], "api_process")
        self.assertTrue(payload["service"])
        self.assertEqual(payload["version"], "0.1.0")

    def test_docs_expose_the_openapi_document(self) -> None:
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/openapi.json", response.text)
        schema = self.client.get("/openapi.json").json()
        self.assertEqual(set(schema["paths"]), {
            "/health", "/api/v1/people", "/api/v1/people/{person_id}",
            "/api/v1/sources", "/api/v1/public-records",
            "/api/v1/public-records/{record_id}",
            "/api/v1/collection-runs", "/api/v1/collection-runs/{run_id}",
            "/api/v1/collection-rules",
            "/api/v1/system-status",
            "/api/v1/monitoring-baseline",
            "/api/v1/monitoring-status",
            "/api/v1/events", "/api/v1/events/{event_id}",
            "/api/v1/events/{event_id}/versions", "/api/v1/events/{event_id}/versions/{version_id}",
            "/api/v1/events/{event_id}/observations", "/api/v1/public-records/{record_id}/event",
            "/api/v1/task-queue", "/api/v1/collection-tasks", "/api/v1/collection-tasks/{task_id}",
            "/api/v1/collection-tasks/{task_id}/attempts",
            "/api/v1/ecb-discovery", "/api/v1/ecb-discovery/candidates",
            "/api/v1/content-stream", "/api/v1/content-stream/state",
            "/api/v1/keyword-rules", "/api/v1/keyword-rules/{rule_id}",
            "/api/v1/keyword-rules/{rule_id}/drafts", "/api/v1/keyword-rules/{rule_id}/versions",
            "/api/v1/keyword-rules/{rule_id}/versions/{version_id}/preview", "/api/v1/keyword-rules/{rule_id}/publish",
            "/api/v1/keyword-rules/{rule_id}/previews/{preview_id}", "/api/v1/keyword-rules/{rule_id}/publications",
            "/api/v1/annotation-datasets", "/api/v1/annotation-datasets/{dataset_id}",
            "/api/v1/annotation-datasets/{dataset_id}/samples", "/api/v1/annotation-datasets/{dataset_id}/capture-records",
            "/api/v1/annotation-datasets/{dataset_id}/seal", "/api/v1/annotation-datasets/{dataset_id}/export",
            "/api/v1/annotation-datasets/{dataset_id}/benchmark", "/api/v1/annotation-datasets/{dataset_id}/runs/{run_id}",
            "/api/v1/annotation-review/{dataset_id}/samples", "/api/v1/annotation-review/{dataset_id}/samples/{sample_id}/labels",
            "/api/v1/annotation-review/{dataset_id}/disputes",
            "/api/v1/agent-analyses", "/api/v1/agent-analyses/{run_id}",
            "/api/v1/agent-analysis-automation",
            "/api/v1/knowledge/status", "/api/v1/knowledge/search",
            "/api/v1/reminders", "/api/v1/reminders/summary", "/api/v1/reminders/{reminder_id}",
            "/api/v1/agent-chat/reminder-feedback",
            "/api/v1/agent-chat/me", "/api/v1/agent-chat/conversations",
            "/api/v1/agent-chat/conversations/{conversation_id}",
            "/api/v1/agent-chat/conversations/{conversation_id}/messages",
            "/api/v1/agent-chat/conversations/{conversation_id}/answers",
            "/api/v1/agent-chat/conversations/{conversation_id}/answers/{run_id}",
            "/api/v1/agent-chat/generation-budget",
            "/api/v1/agent-chat/conversations/{conversation_id}/answers/{run_id}/execute",
            "/api/v1/source-catalog", "/api/v1/source-catalog/summary",
            "/api/v1/web-pilot/summary", "/api/v1/web-pilot/documents", "/api/v1/web-pilot/documents/{document_id}",
            "/api/v1/monitor-speed", "/api/v1/monitor-speed/receipts",
            "/api/v1/module-info",
            "/api/v1/agent-chat/quality-report", "/api/v1/agent-chat/quality-cases/{case_id}",
            "/api/v1/agent-chat/conversations/{conversation_id}/answers/{run_id}/feedback",
            "/api/v1/agent-chat/conversations/{conversation_id}/answers/{run_id}/quality-case",
            "/api/v1/quality-review/{case_id}",
            "/api/v1/source-catalog/{candidate_id}", "/api/v1/source-catalog/{candidate_id}/reviews",
        })
        self.assertIn("get", schema["paths"]["/health"])

    def test_environment_changes_service_name(self) -> None:
        with patch.dict(os.environ, {"PQ_SERVICE_NAME": "Team integration test"}):
            app = create_app(Settings(_env_file=None))
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").json()["service"], "Team integration test")
            self.assertEqual(client.get("/openapi.json").json()["info"]["title"], "Team integration test")

    def test_invalid_port_fails_before_server_start(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, port=70000)


if __name__ == "__main__":
    unittest.main()
