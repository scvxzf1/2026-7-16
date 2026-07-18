from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gdl_backend.app import ServiceContainer, _validate_network_target, create_app

from tests.helpers import make_settings


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name))
        self.settings.server.api_key = "test-key"
        self.container = ServiceContainer(self.settings)
        self.app = create_app(self.settings, container=self.container, start_background=False)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.headers = {"X-API-Key": "test-key"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_health_and_auth(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/tasks").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self.headers).status_code, 200)

    def test_task_idempotency_cancel_logs_and_files(self):
        body = {"url": "https://www.pixiv.net/artworks/123456", "proxy_mode": "direct"}
        headers = {**self.headers, "Idempotency-Key": "same-request"}
        first = self.client.post("/api/v1/tasks", headers=headers, json=body)
        self.assertEqual(first.status_code, 202, first.text)
        second = self.client.post("/api/v1/tasks", headers=headers, json=body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        task_id = first.json()["id"]
        cancelled = self.client.post(f"/api/v1/tasks/{task_id}/cancel", headers=self.headers)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}/logs", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}/files", headers=self.headers).status_code, 200)

    def test_site_policy_crud_and_proxy_status(self):
        policy = {
            "max_concurrency": 1,
            "retry_limit": 1,
            "backoff_base_seconds": 0,
            "proxy_mode": "required",
            "probe_url": "https://www.pixiv.net/",
            "probe_before_use": True,
            "node_tags": ["jp"],
            "http_timeout": 15,
            "gallery_retries": 1,
            "task_timeout_seconds": 60,
            "extra_args": [],
        }
        response = self.client.put("/api/v1/sites/policies/pixiv", headers=self.headers, json=policy)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["policy"]["node_tags"], ["jp"])
        status = self.client.get("/api/v1/proxy/status", headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["running"])
        self.assertTrue(status.json()["managed_by_backend"])
        self.assertFalse(status.json()["auto_start"])
        self.assertEqual(status.json()["engine"], "native")
        self.assertFalse(status.json()["executable_required"])

    def test_private_target_guard(self):
        with self.assertRaises(ValueError):
            _validate_network_target("http://127.0.0.1:8080/a", False)
        _validate_network_target("http://127.0.0.1:8080/a", True)

    def test_non_loopback_bind_requires_api_key(self):
        self.settings.server.host = "0.0.0.0"
        self.settings.server.api_key = ""
        with self.assertRaises(ValueError):
            self.settings.validate()


if __name__ == "__main__":
    unittest.main()
