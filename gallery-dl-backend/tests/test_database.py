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

    def test_ordered_crawl_batch_persists_order_links_and_idempotency(self):
        addresses = [
            {
                "id": "address-1",
                "site": "twitter",
                "source_order": 0,
                "address_order": 0,
                "url": "https://x.com/a/media",
                "proxy_mode": "required",
                "max_attempts": 3,
            },
            {
                "id": "address-2",
                "site": "pixiv",
                "source_order": 1,
                "address_order": 0,
                "url": "https://www.pixiv.net/users/1/artworks",
                "proxy_mode": "required",
                "max_attempts": 3,
            },
        ]
        batch_id, created = self.db.create_crawl_batch(
            {
                "id": "batch-1",
                "output_dir": str(self.root / "batch"),
                "concurrency": 20,
                "max_tasks": 100,
            },
            addresses,
            idempotency_key="same-batch",
        )
        self.assertTrue(created)
        duplicate, created = self.db.create_crawl_batch(
            {
                "id": "batch-2",
                "output_dir": str(self.root / "other"),
                "concurrency": 1,
                "max_tasks": 1,
            },
            addresses,
            idempotency_key="same-batch",
        )
        self.assertFalse(created)
        self.assertEqual(duplicate, batch_id)

        probe = self.db.save_crawl_address_proxy_probe(
            "address-1",
            target_url="https://x.com/",
            total_count=3,
            healthy_node_ids=["node-b", "node-a", "node-b"],
        )
        self.assertEqual(probe["healthy_count"], 2)
        self.assertEqual(probe["node_ids"], ["node-a", "node-b"])
        batch = self.db.get_crawl_batch(batch_id)
        first_address = batch["sources"][0]["addresses"][0]
        self.assertEqual(first_address["proxy_probe_target"], "https://x.com/")
        self.assertEqual(first_address["probed_proxy_count"], 3)
        self.assertEqual(first_address["healthy_proxy_count"], 2)

        self.assertEqual(self.db.next_crawl_address(batch_id)["id"], "address-1")
        self.assertTrue(self.db.begin_crawl_address_planning("address-1"))

        values = task_values(self.root)
        self.db.create_task(values)
        self.db.link_crawl_task("address-1", "task-1", 1)
        self.assertTrue(self.db.mark_crawl_address_running("address-1"))
        self.db.complete_task("task-1", "succeeded")
        self.assertTrue(self.db.finish_crawl_address_if_terminal("address-1"))
        self.assertEqual(self.db.next_crawl_address(batch_id)["id"], "address-2")
        batch = self.db.get_crawl_batch(batch_id)
        self.assertEqual(batch["task_count"], 1)
        self.assertEqual(batch["succeeded_task_count"], 1)
        self.assertEqual(batch["sources"][0]["status"], "succeeded")
        self.assertEqual(batch["sources"][1]["status"], "pending")

    def test_ordered_crawl_recovery_resets_only_planning_address(self):
        self.db.create_crawl_batch(
            {
                "id": "batch-recovery",
                "output_dir": str(self.root / "batch"),
                "concurrency": 20,
                "max_tasks": 100,
            },
            [
                {
                    "id": "address-recovery",
                    "site": "danbooru",
                    "source_order": 0,
                    "address_order": 0,
                    "url": "https://danbooru.donmai.us/posts?tags=a",
                    "proxy_mode": "prefer",
                    "max_attempts": 3,
                }
            ],
        )
        self.assertTrue(self.db.begin_crawl_address_planning("address-recovery"))
        self.assertEqual(self.db.recover_ordered_crawls(), 1)
        self.assertEqual(self.db.next_crawl_address("batch-recovery")["status"], "pending")

    def test_ordered_crawl_recovery_drains_partially_linked_address(self):
        self.db.create_crawl_batch(
            {
                "id": "batch-linked-recovery",
                "output_dir": str(self.root / "batch"),
                "concurrency": 20,
                "max_tasks": 100,
            },
            [
                {
                    "id": "address-linked-recovery",
                    "site": "twitter",
                    "source_order": 0,
                    "address_order": 0,
                    "url": "https://x.com/artist/media",
                    "proxy_mode": "prefer",
                    "max_attempts": 3,
                }
            ],
        )
        self.assertTrue(self.db.begin_crawl_address_planning("address-linked-recovery"))
        self.db.create_task(task_values(self.root))
        self.db.link_crawl_task("address-linked-recovery", "task-1", 1)

        self.assertEqual(self.db.recover_ordered_crawls(), 1)
        address = self.db.next_crawl_address("batch-linked-recovery")
        self.assertEqual(address["status"], "running")
        self.assertEqual(address["planned_task_count"], 1)
        self.assertIn("部分规划", address["last_error"])

        self.db.complete_task("task-1", "succeeded")
        self.assertTrue(self.db.finish_crawl_address_if_terminal(address["id"]))
        batch = self.db.get_crawl_batch("batch-linked-recovery")
        self.assertEqual(batch["sources"][0]["addresses"][0]["status"], "failed")

    def test_schema_v1_reopen_creates_ordered_crawl_tables(self):
        path = self.root / "legacy.sqlite3"
        legacy = Database(path)
        with legacy._transaction() as conn:
            conn.execute("DROP TABLE crawl_address_proxy_nodes")
            conn.execute("DROP TABLE crawl_address_proxy_probes")
            conn.execute("DROP TABLE crawl_address_tasks")
            conn.execute("DROP TABLE crawl_addresses")
            conn.execute("DROP TABLE crawl_batches")
            conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        legacy.close()

        upgraded = Database(path)
        try:
            version = upgraded._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in upgraded._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertEqual(version, "3")
            self.assertTrue(
                {
                    "crawl_batches",
                    "crawl_addresses",
                    "crawl_address_tasks",
                    "crawl_address_proxy_probes",
                    "crawl_address_proxy_nodes",
                }.issubset(tables)
            )
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
