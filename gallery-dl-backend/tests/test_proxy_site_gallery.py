from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gdl_backend.gallery import GalleryRunner
from gdl_backend.proxy import ProxyPoolAdapter
from gdl_backend.proxy_runtime import LocalHTTPForwarder, _connect_response_succeeded
from gdl_backend.proxy_sources import parse_subscription_text
from gdl_backend.site import SiteResolver

from tests.helpers import WORKSPACE, make_settings


class SiteAndGalleryTests(unittest.TestCase):
    def test_site_resolver_uses_gallery_extractor(self):
        resolver = SiteResolver(WORKSPACE / "gallery-dl-codeberg")
        info = resolver.resolve("https://www.pixiv.net/artworks/123456")
        self.assertTrue(info.supported)
        self.assertEqual(info.site, "pixiv")

    def test_gallery_runner_rejects_managed_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            runner = GalleryRunner(settings.gallery, settings.project_dir)
            with self.assertRaises(ValueError):
                runner.validate_args(["--proxy=http://other"])
            command = runner.build_command(
                marker="m",
                url="https://example.com/",
                output_dir=str(Path(tmp) / "out"),
                proxy_url="http://127.0.0.1:28000",
                http_timeout=10,
                gallery_retries=1,
                cookies_file=None,
                config_file=None,
                extra_args=["--sleep", "0"],
            )
            self.assertIn("gdl_backend.worker_entry", command)
            self.assertIn("--proxy", command)
            self.assertIn("--cache-file", command)
            self.assertIn(str(settings.gallery.cache_file), command)
            with self.assertRaises(ValueError):
                runner.validate_args(["--cache-file", "other.sqlite3"])

    def test_worker_entry_loads_local_gallery_source(self):
        command = [
            sys.executable,
            "-m",
            "gdl_backend.worker_entry",
            "--marker",
            "test-marker",
            "--gallery-root",
            str(WORKSPACE / "gallery-dl-codeberg"),
            "--",
            "--version",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1.32.7", result.stdout)


class ProxyAdapterTests(unittest.TestCase):
    def test_native_parser_accepts_base64_and_clash_yaml(self):
        encoded = base64.b64encode(
            b"http://127.0.0.1:18080#HTTP\nsocks5://127.0.0.1:18081#SOCKS"
        ).decode("ascii")
        base64_nodes = parse_subscription_text(encoded)
        self.assertEqual([node.scheme for node in base64_nodes], ["http", "socks5"])

        https_nodes = parse_subscription_text("https://proxy.example:443#TLS")
        self.assertEqual(https_nodes[0].scheme, "https")
        self.assertTrue(https_nodes[0].endpoint.startswith("https://"))

        clash_nodes = parse_subscription_text(
            """
proxies:
  - name: JP-HTTP
    type: http
    server: 127.0.0.1
    port: 18082
  - name: TUNNEL
    type: trojan
    server: tunnel.example
    port: 443
    password: secret
"""
        )
        self.assertEqual(len(clash_nodes), 2)
        self.assertTrue(clash_nodes[0].usable)
        self.assertFalse(clash_nodes[1].usable)

    def test_subscription_parser_enforces_content_limit(self):
        with patch("gdl_backend.proxy_sources.MAX_SUBSCRIPTION_BYTES", 32):
            with self.assertRaises(ValueError):
                parse_subscription_text("http://proxy.example:8080#" + "x" * 64)

    def test_connect_forwarder_accepts_only_2xx_status(self):
        self.assertTrue(_connect_response_succeeded(b"HTTP/1.1 200 Connection Established\r\n\r\n"))
        self.assertTrue(_connect_response_succeeded(b"HTTP/1.1 299 Fixture\r\n\r\n"))
        self.assertFalse(_connect_response_succeeded(b"HTTP/1.1 302 Found\r\n\r\n"))
        self.assertFalse(_connect_response_succeeded(b"malformed\r\n\r\n"))

    def test_native_authenticated_http_forwarder_injects_basic_auth(self):
        expected = "Basic " + base64.b64encode(b"user:pass").decode("ascii")
        payload = b"forwarded"

        class UpstreamHandler(BaseHTTPRequestHandler):
            seen_auth = ""

            def do_GET(self):
                type(self).seen_auth = self.headers.get("Proxy-Authorization", "")
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        forwarder = LocalHTTPForwarder(
            f"http://user:pass@127.0.0.1:{server.server_port}"
        )
        local_url = forwarder.start()
        try:
            import requests

            response = requests.get(
                "http://example.invalid/resource",
                proxies={"http": local_url},
                timeout=5,
            )
            self.assertEqual(response.content, payload)
            self.assertEqual(UpstreamHandler.seen_auth, expected)
        finally:
            forwarder.stop()
            server.shutdown()
            server.server_close()

    def test_imports_plain_airport_subscription_without_executable(self):
        body = (
            "http://127.0.0.1:18080#HTTP\n"
            "socks5://127.0.0.1:18081#SOCKS\n"
            "trojan://secret@tunnel.example:443#TUNNEL\n"
        ).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                settings = make_settings(Path(tmp))
                settings.proxy.enabled = True
                settings.proxy.subscription_urls = [
                    f"http://127.0.0.1:{server.server_port}/subscription"
                ]
                adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
                nodes, summary = adapter._collect_nodes(force_refresh=True)
                self.assertEqual(len(nodes), 2)
                self.assertEqual(summary["source_nodes"], 3)
                self.assertEqual(summary["skipped_nodes"], 1)
                self.assertEqual(summary["scheme_counts"]["trojan"], 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_collects_http_and_socks_and_reports_tunnel_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = [
                "http://127.0.0.1:18080#JP",
                "socks5://127.0.0.1:18081#HK",
                "vless://11111111-1111-1111-1111-111111111111@jp.example:443?security=tls#TUNNEL",
            ]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            nodes, summary = adapter._collect_nodes(force_refresh=True)
            self.assertEqual(len(nodes), 2)
            self.assertEqual(summary["pool_nodes"], 2)
            self.assertEqual(summary["skipped_nodes"], 1)
            self.assertEqual({node.protocol for node in nodes}, {"http", "socks5"})
            self.assertIn("jp", adapter._node_meta[nodes[0].id]["tags"])

    def test_rotator_lease_tag_filter_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.auto_start = True
            settings.proxy.inline_nodes = [
                "http://127.0.0.1:18080#JP",
                "http://127.0.0.1:18081#HK",
            ]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            adapter.probe = lambda **_: {"total": 2, "healthy": 2, "results": []}
            started = adapter.start(force_refresh=True)
            self.assertEqual(started["start"]["engine"], "native")
            lease = adapter.acquire("task-jp", node_tags=["jp"])
            self.assertIsNotNone(lease)
            self.assertIn("jp", lease.tags)
            self.assertEqual(adapter.active_leases, 1)
            adapter.release("task-jp", proxy_fault=False)
            self.assertEqual(adapter.active_leases, 0)
            self.assertFalse(adapter.stop()["running"])

    def test_tag_filter_uses_exact_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = ["http://127.0.0.1:18080#AUSTRALIA"]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            adapter.probe = lambda **_: {"total": 1, "healthy": 1, "results": []}
            adapter.start(force_refresh=True)
            try:
                self.assertIsNone(adapter.acquire("task-us", node_tags=["us"]))
                lease = adapter.acquire("task-au", node_tags=["au"])
                self.assertIsNotNone(lease)
                adapter.release("task-au", proxy_fault=False)
            finally:
                adapter.stop(force=True)

    def test_authenticated_socks_is_excluded_from_command_line_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = ["socks5://user:pass@127.0.0.1:18081#AUTH"]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            nodes, summary = adapter._collect_nodes(force_refresh=True)
            self.assertEqual(nodes, [])
            self.assertEqual(summary["skipped_nodes"], 1)

    def test_authenticated_http_proxy_uses_python_local_forwarder(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = ["http://user:pass@127.0.0.1:18082#AUTH"]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            adapter.probe = lambda **_: {"total": 1, "healthy": 1, "results": []}
            adapter.start(force_refresh=True)
            lease = adapter.acquire("task-auth")
            self.assertIsNotNone(lease)
            self.assertTrue(lease.endpoint.startswith("http://127.0.0.1:"))
            self.assertNotIn("user", lease.endpoint)
            adapter.release("task-auth", proxy_fault=True, reason="fixture")
            adapter.stop()

    def test_slow_acquire_probe_does_not_block_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = ["http://127.0.0.1:18083#SLOW"]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            adapter.probe = lambda **_: {"total": 1, "healthy": 1, "results": []}
            adapter.start(force_refresh=True)
            entered = threading.Event()
            release_probe = threading.Event()
            result: dict[str, object] = {}

            def slow_probe(*args, **kwargs):
                entered.set()
                release_probe.wait(timeout=1.0)
                return {"healthy": True}

            adapter._probe_endpoint = slow_probe
            thread = threading.Thread(
                target=lambda: result.setdefault(
                    "lease",
                    adapter.acquire("task-slow", probe_before_use=True),
                ),
                daemon=True,
            )
            thread.start()
            self.assertTrue(entered.wait(timeout=1.0))
            started = time.monotonic()
            status = adapter.status()
            elapsed = time.monotonic() - started
            self.assertTrue(status["running"])
            self.assertLess(elapsed, 0.4)
            release_probe.set()
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(result.get("lease"))
            adapter.release("task-slow", proxy_fault=False)
            adapter.stop()

    def test_probe_target_rejects_credentials_and_hides_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            with self.assertRaises(Exception):
                adapter._probe_parts("https://user:pass@example.com/private")
            public = adapter._public_probe_target("https://example.com/check?token=secret")
            self.assertEqual(public, "https://example.com/check")

    def test_probe_rejects_proxy_auth_and_gateway_server_errors(self):
        class Response:
            def __init__(self, status_code):
                self.status_code = status_code

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = ["http://127.0.0.1:18080#PROBE"]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            nodes, _ = adapter._collect_nodes(force_refresh=True)
            for status_code, expected in ((403, True), (407, False), (502, False)):
                with self.subTest(status_code=status_code):
                    with patch("gdl_backend.proxy.requests.get", return_value=Response(status_code)):
                        result = adapter._probe_endpoint(
                            nodes[0].id,
                            nodes[0].endpoint,
                            "https://example.com/",
                        )
                    self.assertEqual(result["healthy"], expected)


if __name__ == "__main__":
    unittest.main()
