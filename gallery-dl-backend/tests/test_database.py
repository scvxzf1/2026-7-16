from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gdl_backend.database import Database


def task_values(root: Path) -> dict:
    return {
        "id": "task-1",
        "url": "https://example.com/gallery/1",
        "site": "example.com",
        "subcategory": "gallery",
        "extractor": "ExampleExtractor",
        "output_dir": str(root / "out"),
        "proxy_mode": "prefer",
        "max_attempts": 3,
        "policy": {"max_concurrency": 1},
        "extra_args": [],
    }


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "db.sqlite3", max_logs_per_task=100)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_task_attempt_retry_and_completion(self):
        created, is_new = self.db.create_task(task_values(self.root), idempotency_key="same")
        self.assertTrue(is_new)
        duplicate, is_new = self.db.create_task({**task_values(self.root), "id": "other"}, idempotency_key="same")
        self.assertFalse(is_new)
        self.assertEqual(created["id"], duplicate["id"])

        self.assertTrue(self.db.claim_task("task-1"))
        attempt = self.db.begin_attempt("task-1")
        self.db.set_process("task-1", attempt["id"], 123, "marker")
        self.db.set_lease("task-1", attempt["id"], "node", "http://127.0.0.1:28000", "example.com")
        self.db.finish_attempt(
            attempt["id"],
            exit_code=4,
            status="failed",
            error_class="proxy_failure",
            error_message="ProxyError",
            retryable=True,
            proxy_node_id="node",
        )
        self.db.clear_lease("task-1")
        self.db.requeue_task(
            "task-1",
            next_run_at=0,
            exit_code=4,
            error_class="proxy_failure",
            error_message="ProxyError",
            tried_proxy_ids=["node"],
        )
        task = self.db.get_task("task-1")
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["tried_proxy_ids"], ["node"])

        self.db.complete_task("task-1", "failed", error_class="exhausted")
        self.assertEqual(self.db.get_task("task-1")["status"], "failed")
        retried = self.db.retry_task("task-1", 2)
        self.assertEqual(retried["status"], "queued")

    def test_logs_events_and_policy(self):
        self.db.create_task(task_values(self.root))
        self.db.append_log("task-1", None, "stderr", "token=secret")
        logs = self.db.get_logs("task-1")
        self.assertEqual(len(logs), 1)
        self.assertNotIn("secret", logs[0]["line"])
        self.assertTrue(self.db.get_events("task-1"))
        policy = {"max_concurrency": 3}
        self.db.put_site_policy("example.com", policy)
        self.assertEqual(self.db.get_site_policy("example.com")["policy"], policy)
        self.assertTrue(self.db.delete_site_policy("example.com"))

    def test_lease_cleanup_is_scoped_to_attempt(self):
        self.db.create_task(task_values(self.root))
        self.assertTrue(self.db.claim_task("task-1"))
        attempt = self.db.begin_attempt("task-1")
        self.db.set_lease(
            "task-1",
            attempt["id"],
            "node",
            "http://127.0.0.1:28000",
            "example.com",
        )

        self.db.clear_lease("task-1", "stale-attempt")
        remaining = self.db._conn.execute(
            "SELECT attempt_id FROM leases WHERE task_id=?", ("task-1",)
        ).fetchone()
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining["attempt_id"], attempt["id"])

        self.db.clear_lease("task-1", attempt["id"])
        remaining = self.db._conn.execute(
            "SELECT attempt_id FROM leases WHERE task_id=?", ("task-1",)
        ).fetchone()
        self.assertIsNone(remaining)


if __name__ == "__main__":
    unittest.main()
