from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from gdl_backend.database import Database
from gdl_backend.gallery import GalleryRunResult
from gdl_backend.proxy import ProxyLease
from gdl_backend.scheduler import TaskScheduler
from gdl_backend.schemas import SitePolicy

from tests.helpers import make_settings


class FakeGallery:
    def __init__(self, results: list[GalleryRunResult]):
        self.results = list(results)
        self.cancelled: list[str] = []
        self.calls: list[dict] = []

    async def run(self, task_id: str, **kwargs):
        self.calls.append({"task_id": task_id, **kwargs})
        await kwargs["on_started"](100 + len(self.results), f"marker-{task_id}")
        result = self.results.pop(0)
        if result.output_tail:
            await kwargs["on_line"]("stderr", result.output_tail)
        return result

    async def cancel(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True

    async def stop_all(self):
        return None


class FakeProxy:
    def __init__(self, with_nodes: bool = False):
        self.with_nodes = with_nodes
        self.releases: list[tuple[str, bool]] = []
        self.counter = 0

    def acquire(self, task_id: str, **kwargs):
        if not self.with_nodes:
            return None
        self.counter += 1
        node_id = f"node-{self.counter}"
        return ProxyLease(
            task_id=task_id,
            node_id=node_id,
            endpoint=f"http://127.0.0.1:{28000 + self.counter}",
            name=node_id,
            protocol="vless",
            tags=["jp"],
            acquired_at=time.time(),
        )

    def release(self, task_id: str, *, proxy_fault: bool, reason: str = ""):
        self.releases.append((task_id, proxy_fault))


class ReleaseFailingProxy(FakeProxy):
    def release(self, task_id: str, *, proxy_fault: bool, reason: str = ""):
        super().release(task_id, proxy_fault=proxy_fault, reason=reason)
        raise RuntimeError("release failed")


class CredentialProxy(FakeProxy):
    def acquire(self, task_id: str, **kwargs):
        lease = super().acquire(task_id, **kwargs)
        if lease is not None:
            lease.endpoint = "http://proxy-user:proxy-secret@127.0.0.1:28000"
        return lease


def values(root: Path, *, proxy_mode: str, attempts: int) -> dict:
    policy = SitePolicy(
        max_concurrency=1,
        retry_limit=attempts - 1,
        backoff_base_seconds=0,
        proxy_mode=proxy_mode,
        gallery_retries=0,
    )
    return {
        "id": "task-1",
        "url": "https://example.com/gallery/1",
        "site": "example.com",
        "subcategory": "",
        "extractor": "",
        "output_dir": str(root / "out"),
        "proxy_mode": proxy_mode,
        "max_attempts": attempts,
        "policy": policy.model_dump(),
        "extra_args": [],
    }


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.db = Database(self.settings.database_path)

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def wait_terminal(self, task_id: str, timeout: float = 3.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            task = self.db.get_task(task_id)
            if task and task["status"] in {"succeeded", "failed", "cancelled"}:
                return task
            await asyncio.sleep(0.03)
        self.fail("task did not reach terminal state")

    async def test_direct_success(self):
        self.db.create_task(values(self.root, proxy_mode="direct", attempts=1))
        gallery = FakeGallery([GalleryRunResult(0, "saved file", False, "m", 101)])
        scheduler = TaskScheduler(self.db, gallery, FakeProxy(), self.settings.scheduler)
        await scheduler.start()
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        self.assertEqual(task["status"], "succeeded")
        self.assertTrue(self.db.get_logs("task-1"))

    async def test_prefer_uses_proxy_when_a_node_is_available(self):
        self.db.create_task(values(self.root, proxy_mode="prefer", attempts=1))
        gallery = FakeGallery([GalleryRunResult(0, "saved file", False, "m", 101)])
        proxy = FakeProxy(with_nodes=True)
        scheduler = TaskScheduler(self.db, gallery, proxy, self.settings.scheduler)
        await scheduler.start()
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(gallery.calls[0]["proxy_url"], "http://127.0.0.1:28001")
        self.assertEqual(proxy.releases, [("task-1", False)])

    async def test_prefer_falls_back_to_direct_when_pool_is_empty(self):
        self.db.create_task(values(self.root, proxy_mode="prefer", attempts=1))
        gallery = FakeGallery([GalleryRunResult(0, "saved file", False, "m", 101)])
        scheduler = TaskScheduler(self.db, gallery, FakeProxy(), self.settings.scheduler)
        await scheduler.start()
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        self.assertEqual(task["status"], "succeeded")
        self.assertIsNone(gallery.calls[0]["proxy_url"])
        self.assertTrue(
            any("本次任务使用直连" in row["line"] for row in self.db.get_logs("task-1"))
        )

    async def test_proxy_failure_switches_node_then_succeeds(self):
        self.db.create_task(values(self.root, proxy_mode="required", attempts=2))
        gallery = FakeGallery(
            [
                GalleryRunResult(4, "ProxyError: tunnel connection failed", False, "m1", 101),
                GalleryRunResult(0, "done", False, "m2", 102),
            ]
        )
        proxy = FakeProxy(with_nodes=True)
        scheduler = TaskScheduler(self.db, gallery, proxy, self.settings.scheduler)
        await scheduler.start()
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["attempt_count"], 2)
        self.assertEqual(proxy.releases[0], ("task-1", True))
        self.assertEqual(proxy.releases[1], ("task-1", False))

    async def test_cancel_between_claim_and_begin_attempt_stays_cancelled(self):
        self.db.create_task(values(self.root, proxy_mode="direct", attempts=1))
        self.assertTrue(self.db.claim_task("task-1"))
        self.db.request_cancel("task-1")
        scheduler = TaskScheduler(self.db, FakeGallery([]), FakeProxy(), self.settings.scheduler)
        await scheduler._execute("task-1")
        self.assertEqual(self.db.get_task("task-1")["status"], "cancelled")

    async def test_release_exception_does_not_leave_running_task_or_db_lease(self):
        self.db.create_task(values(self.root, proxy_mode="required", attempts=1))
        gallery = FakeGallery([GalleryRunResult(0, "done", False, "m", 101)])
        proxy = ReleaseFailingProxy(with_nodes=True)
        scheduler = TaskScheduler(self.db, gallery, proxy, self.settings.scheduler)
        await scheduler.start()
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        self.assertEqual(task["status"], "succeeded")
        self.assertIsNone(self.db.get_task("task-1")["lease"])

    async def test_proxy_credentials_are_redacted_before_database_and_logs(self):
        self.db.create_task(values(self.root, proxy_mode="required", attempts=1))
        gallery = FakeGallery([GalleryRunResult(0, "done", False, "m", 101)])
        scheduler = TaskScheduler(
            self.db,
            gallery,
            CredentialProxy(with_nodes=True),
            self.settings.scheduler,
        )
        await scheduler.start()
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        serialized = str(task) + str(self.db.get_logs("task-1"))
        self.assertNotIn("proxy-user", serialized)
        self.assertNotIn("proxy-secret", serialized)

    async def test_invalid_managed_login_pauses_queue_until_reauthorized(self):
        task_values = values(self.root, proxy_mode="direct", attempts=1)
        task_values.update({"site": "twitter", "cookies_file": str(self.root / "twitter.cookies.txt")})
        self.db.create_task(task_values)
        gallery = FakeGallery([GalleryRunResult(0, "done", False, "m", 101)])
        available = False

        def validate(_site, _cookies_file):
            return available

        scheduler = TaskScheduler(
            self.db,
            gallery,
            FakeProxy(),
            self.settings.scheduler,
            credential_validator=validate,
        )
        await scheduler.start()
        await asyncio.sleep(0.15)
        self.assertEqual(self.db.get_task("task-1")["status"], "queued")
        self.assertFalse(gallery.calls)
        available = True
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        self.assertEqual(task["status"], "succeeded")

    async def test_authentication_failure_notifies_managed_auth_callback(self):
        task_values = values(self.root, proxy_mode="direct", attempts=1)
        cookie_file = str(self.root / "twitter.cookies.txt")
        task_values.update({"site": "twitter", "cookies_file": cookie_file})
        self.db.create_task(task_values)
        gallery = FakeGallery(
            [GalleryRunResult(1, "authenticated cookies needed to access this timeline", False, "m", 101)]
        )
        calls = []

        async def invalidate(site, cookies, message):
            calls.append((site, cookies, message))
            return True

        scheduler = TaskScheduler(
            self.db,
            gallery,
            FakeProxy(),
            self.settings.scheduler,
            auth_failure_callback=invalidate,
        )
        await scheduler.start()
        task = await self.wait_terminal("task-1")
        await scheduler.stop()
        self.assertEqual(task["last_error_class"], "authentication")
        self.assertEqual(calls[0][:2], ("twitter", cookie_file))
        self.assertIn("authenticated cookies needed", calls[0][2])
        self.assertTrue(any("等待重新授权" in row["line"] for row in self.db.get_logs("task-1")))


if __name__ == "__main__":
    unittest.main()
