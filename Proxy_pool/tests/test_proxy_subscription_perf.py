# -*- coding: utf-8 -*-
"""P0/P1 tests for concurrent subscription fetch, intervals, new-only merge."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import proxy_subscription as sub


class IntervalTests(unittest.TestCase):
    def test_should_skip_respects_interval(self):
        now = 1_000_000.0
        self.assertFalse(
            sub.should_skip_fetch(
                "https://a.example/s",
                interval_seconds=300,
                last_success_at=0,
                now=now,
            )
        )
        self.assertTrue(
            sub.should_skip_fetch(
                "https://a.example/s",
                interval_seconds=300,
                last_success_at=now - 10,
                now=now,
            )
        )
        self.assertFalse(
            sub.should_skip_fetch(
                "https://a.example/s",
                interval_seconds=300,
                last_success_at=now - 10,
                now=now,
                force=True,
            )
        )
        self.assertFalse(
            sub.should_skip_fetch(
                "https://a.example/s",
                interval_seconds=0,
                last_success_at=now - 1,
                now=now,
            )
        )


class ConcurrentFetchTests(unittest.TestCase):
    def test_multi_url_fetch_is_concurrent(self):
        started = []
        barrier = {"n": 0}

        def fake_body(url, timeout=20.0):
            started.append(url)
            barrier["n"] += 1
            # Wait until both have entered (or timeout) to prove overlap.
            deadline = time.time() + 2.0
            while barrier["n"] < 2 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            host = "1.1.1.1" if url.endswith("/sub-a") else "2.2.2.2"
            return f"http://u:p@{host}:8080\n", "plain"

        urls = ["https://host1.example/sub-a", "https://host2.example/sub-b"]
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            with mock.patch.object(sub, "fetch_subscription_body", side_effect=fake_body):
                t0 = time.time()
                result = sub.import_proxy_subscriptions(
                    urls,
                    max_workers=4,
                    force=True,
                    fetch_state_path=state,
                )
                elapsed = time.time() - t0
        self.assertEqual(len(result.usable_pool_lines), 2)
        # Serial would be ~0.1s+; concurrent stays well under 0.2s for 50ms sleeps.
        self.assertLess(elapsed, 0.18)
        self.assertEqual(set(started), set(urls))

    def test_interval_skips_second_pull(self):
        calls = []

        def fake_body(url, timeout=20.0):
            calls.append(url)
            return "http://u:p@9.9.9.9:8080\n", "plain"

        url = "https://limited.example/sub"
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            with mock.patch.object(sub, "fetch_subscription_body", side_effect=fake_body):
                r1 = sub.import_proxy_subscriptions(
                    [url],
                    default_interval_seconds=600,
                    force=False,
                    fetch_state_path=state,
                )
                self.assertEqual(len(r1.usable_pool_lines), 1)
                r2 = sub.import_proxy_subscriptions(
                    [url],
                    default_interval_seconds=600,
                    force=False,
                    fetch_state_path=state,
                )
                self.assertTrue(r2.per_url[0].get("skipped"))
                self.assertEqual(len(calls), 1)
                r3 = sub.import_proxy_subscriptions(
                    [url],
                    default_interval_seconds=600,
                    force=True,
                    fetch_state_path=state,
                )
                self.assertFalse(r3.per_url[0].get("skipped"))
                self.assertEqual(len(calls), 2)

    def test_fetch_and_merge_new_only(self):
        def fake_body(url, timeout=20.0):
            return (
                "http://u:p@1.1.1.1:8080\n"
                "http://u:p@2.2.2.2:8080\n"
            ), "plain"

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            with mock.patch.object(sub, "fetch_subscription_body", side_effect=fake_body):
                existing = ["1.1.1.1:8080:u:p"]
                # pool_line format for http with auth is host:port:user:pass
                new_lines = sub.fetch_and_merge_new(
                    ["https://x.example/s"],
                    existing,
                    force=True,
                    fetch_state_path=state,
                )
        self.assertEqual(new_lines, ["2.2.2.2:8080:u:p"])


class HealthCheckConcurrentTests(unittest.TestCase):
    def test_check_proxies_concurrent_uses_workers(self):
        seen = []

        def fake_one(proxy):
            seen.append(proxy)
            time.sleep(0.03)
            return proxy, {"ok": True, "latency_ms": 1.0, "error": ""}

        proxies = [f"http://h{i}.example:1" for i in range(6)]
        # Patch the inner worker by wrapping ThreadPoolExecutor path via module function
        with mock.patch.object(
            sub,
            "check_proxies_concurrent",
            wraps=sub.check_proxies_concurrent,
        ):
            # Direct unit: mock urllib path inside _one by patching build path is heavy;
            # instead call with patched __import__ style — use mock on opener.
            pass

        real_openers = []

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
                    real_openers.append(req.full_url if hasattr(req, "full_url") else True)
                    time.sleep(0.03)
                    return FakeResp()

            return O()

        with mock.patch("urllib.request.build_opener", side_effect=fake_build_opener):
            with mock.patch("urllib.request.ProxyHandler", return_value=object()):
                t0 = time.time()
                out = sub.check_proxies_concurrent(
                    proxies,
                    max_workers=6,
                    timeout=2.0,
                )
                elapsed = time.time() - t0
        self.assertEqual(len(out), 6)
        self.assertTrue(all(v["ok"] for v in out.values()))
        # Concurrent 6 * 30ms << serial 180ms
        self.assertLess(elapsed, 0.12)


if __name__ == "__main__":
    unittest.main()
