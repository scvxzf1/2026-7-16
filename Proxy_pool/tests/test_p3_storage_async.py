# -*- coding: utf-8 -*-
"""P3: orjson/SQLite stats backend + asyncio APIs."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from proxy_pool import ProxyRotator, _json_dumps, orjson_available
import proxy_subscription as sub


class OrjsonJsonlTests(unittest.TestCase):
    def test_json_dumps_roundtrip(self):
        payload = {"country": "US", "result": "success", "proxy": "http://***@h:1"}
        text = _json_dumps(payload)
        self.assertIn("US", text)
        # When orjson is present, dumps still returns str
        self.assertIsInstance(text, str)

    def test_jsonl_flush_uses_json_dumps(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = os.path.join(tmp, "stats.log")
            rot = ProxyRotator(
                ["http://u_zone_US:p@a.example:8080"],
                stats_file=stats,
                save_interval=0.0,
                auto_flush_thread=False,
                stats_backend="jsonl",
            )
            rot.record_result(rot.proxies()[0], True, "ok")
            rot.close()
            lines = Path(stats).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("success", lines[0])


class SqliteBackendTests(unittest.TestCase):
    def test_sqlite_flush_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "stats.db")
            proxies = [
                "http://u_zone_JP:p@a.example:8080",
                "http://u_zone_US:p@b.example:8080",
            ]
            rot = ProxyRotator(
                proxies,
                stats_file=db,
                save_interval=0.0,
                auto_flush_thread=False,
                stats_backend="sqlite",
            )
            self.assertEqual(rot._stats_backend, "sqlite")
            rot.record_result(proxies[0], True, "ok")
            rot.record_result(proxies[0], False, "boom")
            rot.record_result(proxies[1], True, "ok")
            self.assertEqual(rot.pending_log_count(), 0)
            rot.close()

            conn = sqlite3.connect(db)
            n_events = conn.execute("SELECT COUNT(*) FROM proxy_events").fetchone()[0]
            self.assertEqual(n_events, 3)
            rows = {
                r[0]: (r[1], r[2])
                for r in conn.execute(
                    "SELECT country, success, fail FROM country_stats"
                ).fetchall()
            }
            conn.close()
            self.assertEqual(rows["JP"], (1, 1))
            self.assertEqual(rows["US"], (1, 0))

            # Reload aggregates from SQLite
            rot2 = ProxyRotator(
                proxies,
                stats_file=db,
                auto_flush_thread=False,
                stats_backend="sqlite",
            )
            stats = {r["country"]: r for r in rot2.get_country_stats()}
            self.assertEqual(stats["JP"]["success"], 1)
            self.assertEqual(stats["JP"]["fail"], 1)
            self.assertEqual(stats["US"]["success"], 1)
            rot2.close()

    def test_auto_backend_from_db_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "x.db")
            rot = ProxyRotator(
                ["http://u:p@h.example:1"],
                stats_file=db,
                auto_flush_thread=False,
            )
            self.assertEqual(rot._stats_backend, "sqlite")
            rot.close()


class AsyncApiTests(unittest.TestCase):
    def test_import_subscriptions_async(self):
        def fake_body(url, timeout=20.0):
            return "http://u:p@3.3.3.3:8080\n", "plain"

        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / "state.json"
                with mock.patch.object(sub, "fetch_subscription_body", side_effect=fake_body):
                    return await sub.import_proxy_subscriptions_async(
                        ["https://async.example/s"],
                        force=True,
                        fetch_state_path=state,
                    )

        result = asyncio.run(run())
        self.assertEqual(len(result.usable_pool_lines), 1)

    def test_check_proxies_async_fallback(self):
        proxies = [f"http://h{i}.example:1" for i in range(4)]

        class FakeResp:
            def read(self, _n=None):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_build_opener(*_a, **_k):
            class O:
                def open(self, req, timeout=10):
                    time.sleep(0.02)
                    return FakeResp()

            return O()

        async def run():
            with mock.patch("urllib.request.build_opener", side_effect=fake_build_opener):
                with mock.patch("urllib.request.ProxyHandler", return_value=object()):
                    # Force fallback path by hiding aiohttp if present
                    import sys

                    saved = sys.modules.get("aiohttp")
                    sys.modules["aiohttp"] = None  # type: ignore
                    try:
                        return await sub.check_proxies_async(
                            proxies,
                            concurrency=4,
                            timeout=2.0,
                        )
                    finally:
                        if saved is None:
                            sys.modules.pop("aiohttp", None)
                        else:
                            sys.modules["aiohttp"] = saved

        t0 = time.time()
        out = asyncio.run(run())
        elapsed = time.time() - t0
        self.assertEqual(len(out), 4)
        self.assertTrue(all(v["ok"] for v in out.values()))
        self.assertLess(elapsed, 0.15)


if __name__ == "__main__":
    unittest.main()
