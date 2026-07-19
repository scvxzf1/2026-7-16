from __future__ import annotations

import base64
import json
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

    def test_site_resolver_normalizes_twitter_media_hosts(self):
        resolver = SiteResolver(WORKSPACE / "gallery-dl-codeberg")
        image = resolver.resolve("https://pbs.twimg.com/media/sample?format=jpg&name=orig")
        video = resolver.resolve(
            "https://video.twimg.com/ext_tw_video/123/pu/vid/1280x720/sample.mp4?tag=12"
        )
        self.assertTrue(image.supported)
        self.assertEqual((image.site, image.host), ("twitter", "pbs.twimg.com"))
        self.assertTrue(video.supported)
        self.assertEqual((video.site, video.host), ("twitter", "video.twimg.com"))

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
            twitter_command = runner.build_command(
                marker="twitter",
                url="https://x.com/example/status/123",
                output_dir=str(Path(tmp) / "twitter"),
                proxy_url=None,
                http_timeout=10,
                gallery_retries=1,
                cookies_file=str(Path(tmp) / "twitter.cookies.txt"),
                config_file=None,
                extra_args=[],
            )
            self.assertIn("extractor.twitter.cookies-update=false", twitter_command)
            pixiv_command = runner.build_command(
                marker="pixiv",
                url="https://www.pixiv.net/artworks/123",
                output_dir=str(Path(tmp) / "pixiv"),
                proxy_url=None,
                http_timeout=10,
                gallery_retries=1,
                cookies_file=str(Path(tmp) / "pixiv.cookies.txt"),
                config_file=None,
                extra_args=[],
            )
            self.assertNotIn("extractor.twitter.cookies-update=false", pixiv_command)
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
        authenticated = parse_subscription_text("http://user:direct-secret@proxy.example:8080#AUTH")
        self.assertNotIn("direct-secret", repr(authenticated))

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
  - name: HY2-ALIAS
    type: hy2
    server: hy2.example
    port: 443
    password: secret
"""
        )
        self.assertEqual(len(clash_nodes), 3)
        self.assertTrue(clash_nodes[0].usable)
        self.assertFalse(clash_nodes[1].usable)
        self.assertEqual(
            (clash_nodes[2].scheme, clash_nodes[2].core_config["type"]),
            ("hysteria2", "hysteria2"),
        )

    def test_common_uri_subscriptions_build_private_core_configs(self):
        encode = lambda value: base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")
        vmess = {
            "v": "2",
            "ps": "VMESS",
            "add": "vmess.example",
            "port": "443",
            "id": "11111111-1111-1111-1111-111111111111",
            "aid": "1",
            "scy": "auto",
            "net": "ws",
            "host": "cdn.example",
            "path": "/ws",
            "tls": "tls",
            "sni": "edge.example",
        }
        ssr = encode(
            "ssr.example:443:auth_sha1_v4:aes-256-cfb:tls1.2_ticket_auth:"
            + encode("ssr-secret")
            + "/?remarks="
            + encode("SSR")
        )
        payload = "\n".join(
            [
                "vless://11111111-1111-1111-1111-111111111111@vless.example:443"
                "?security=reality&type=tcp&sni=www.example.com&fp=chrome&pbk=public-key"
                "&sid=abcd&flow=xtls-rprx-vision#VLESS",
                "trojan://trojan-secret@trojan.example:443"
                "?type=ws&host=cdn.example&path=%2Fws&sni=edge.example#TROJAN",
                "ss://"
                + encode("aes-256-gcm:ss-secret")
                + "@ss.example:8388?plugin=v2ray-plugin%3Bmode%3Dwebsocket%3Bhost%3Dcdn.example#SS",
                "ssr://" + ssr,
                "vmess://" + encode(json.dumps(vmess)),
                "hysteria://hysteria.example:443?protocol=udp&upmbps=50&downmbps=100"
                "#HYSTERIA",
                "hy2://hy2-secret@hy2.example:443?sni=edge.example&obfs=salamander"
                "&obfs-password=mask#HY2",
                "tuic://11111111-1111-1111-1111-111111111111:tuic-secret@tuic.example:443"
                "?sni=edge.example#TUIC",
                "anytls://any-secret@anytls.example:443?sni=edge.example&insecure=1#ANYTLS",
                "mieru://user:mieru-secret@mieru.example:8443"
                "?transport=tcp&multiplexing=low&port-range=8443-8450#MIERU",
            ]
        )
        nodes = parse_subscription_text(payload)
        self.assertEqual(
            [node.scheme for node in nodes],
            [
                "vless",
                "trojan",
                "ss",
                "ssr",
                "vmess",
                "hysteria",
                "hysteria2",
                "tuic",
                "anytls",
                "mieru",
            ],
        )
        self.assertTrue(all(node.core_config and not node.usable for node in nodes))
        self.assertEqual(nodes[0].core_config["reality-opts"]["public-key"], "public-key")
        self.assertEqual(nodes[1].core_config["ws-opts"]["path"], "/ws")
        self.assertEqual(nodes[2].core_config["plugin"], "v2ray-plugin")
        self.assertTrue(nodes[4].core_config["tls"])
        self.assertEqual(nodes[4].core_config["alterId"], 1)
        self.assertNotIn("alter-id", nodes[4].core_config)
        self.assertNotIn("auth-str", nodes[5].core_config)
        self.assertEqual((nodes[5].core_config["up"], nodes[5].core_config["down"]), (50, 100))
        self.assertEqual(nodes[9].core_config["multiplexing"], "MULTIPLEXING_LOW")
        self.assertEqual(nodes[9].core_config["port-range"], "8443-8450")
        self.assertNotIn("port", nodes[9].core_config)
        representation = repr(nodes)
        for secret in ("trojan-secret", "ss-secret", "ssr-secret", "hy2-secret", "tuic-secret"):
            self.assertNotIn(secret, representation)

    def test_json_subscriptions_accept_singbox_and_sip008(self):
        singbox = parse_subscription_text(
            json.dumps(
                {
                    "outbounds": [
                        {
                            "type": "vless",
                            "tag": "SINGBOX",
                            "server": "singbox.example",
                            "server_port": 443,
                            "uuid": "11111111-1111-1111-1111-111111111111",
                            "tls": {
                                "enabled": True,
                                "server_name": "edge.example",
                                "utls": {"enabled": True, "fingerprint": "chrome"},
                            },
                            "transport": {
                                "type": "ws",
                                "path": "/ws",
                            "headers": {
                                "Host": "cdn.example",
                                "User-Agent": "fixture-agent",
                            },
                        },
                    },
                    {
                        "type": "mieru",
                        "tag": "SINGBOX-MIERU",
                        "server": "mieru.example",
                        "server_ports": ["5000:5010"],
                        "username": "user",
                        "password": "mieru-secret",
                        "transport": "tcp",
                        "multiplexing": "middle",
                    },
                    ]
                }
            )
        )
        self.assertEqual((singbox[0].scheme, singbox[0].name), ("vless", "SINGBOX"))
        self.assertEqual(singbox[0].core_config["ws-opts"]["headers"]["Host"], "cdn.example")
        self.assertEqual(
            singbox[0].core_config["ws-opts"]["headers"]["User-Agent"],
            "fixture-agent",
        )
        self.assertEqual(singbox[1].core_config["port-range"], "5000-5010")
        self.assertEqual(singbox[1].core_config["multiplexing"], "MULTIPLEXING_MIDDLE")
        self.assertNotIn("port", singbox[1].core_config)

        sip008 = parse_subscription_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": [
                        {
                            "id": "SIP008",
                            "server": "sip008.example",
                            "server_port": 8388,
                            "method": "aes-256-gcm",
                            "password": "sip-secret",
                        }
                    ],
                }
            )
        )
        self.assertEqual((sip008[0].scheme, sip008[0].name), ("ss", "SIP008"))
        self.assertNotIn("sip-secret", repr(sip008[0]))

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
                self.assertEqual(summary["core_candidates"], 1)
                self.assertEqual(summary["skipped_nodes"], 0)
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
            self.assertEqual(summary["core_candidates"], 1)
            self.assertEqual(summary["skipped_nodes"], 0)
            self.assertEqual({node.protocol for node in nodes}, {"http", "socks5"})
            self.assertIn("jp", adapter._node_meta[nodes[0].id]["tags"])

    def test_collects_every_imported_node_without_a_count_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = [
                f"vless://11111111-1111-1111-1111-{index:012d}@node-{index}.example:443"
                "?security=tls#NODE-"
                + str(index)
                for index in range(75)
            ]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            nodes, summary = adapter._collect_nodes(force_refresh=True)
            self.assertEqual(nodes, [])
            self.assertEqual(summary["source_nodes"], 75)
            self.assertEqual(summary["core_candidates"], 75)
            self.assertEqual(summary["skipped_nodes"], 0)
            self.assertEqual(len(adapter._core_candidates), 75)

    def test_start_finishes_loading_every_node_before_first_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = [
                f"http://127.0.0.1:{18000 + index}#NODE-{index}" for index in range(75)
            ]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            observed: dict[str, int] = {}

            def probe_after_load(**_):
                status = adapter.status()
                observed["total"] = int(status["total"])
                observed["source_nodes"] = int(status["sources"]["source_nodes"])
                for record in adapter._records:
                    record.healthy = True
                return {"total": len(adapter._records), "healthy": len(adapter._records), "results": []}

            adapter.probe = probe_after_load
            try:
                started = adapter.start(force_refresh=True)
                self.assertEqual(observed, {"total": 75, "source_nodes": 75})
                self.assertEqual(started["probe"]["total"], 75)
                self.assertEqual(started["status"]["healthy"], 75)
            finally:
                adapter.stop(force=True)

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

    def test_rotator_restricts_leases_to_address_probe_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = [
                "http://127.0.0.1:18080#FIRST",
                "http://127.0.0.1:18081#SECOND",
            ]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            adapter.probe = lambda **_: {"total": 2, "healthy": 2, "results": []}
            adapter.start(force_refresh=True)
            try:
                allowed_id = adapter._records[1].id
                lease = adapter.acquire("task-allowed", allowed_ids={allowed_id})
                self.assertIsNotNone(lease)
                self.assertEqual(lease.node_id, allowed_id)
                adapter.release("task-allowed", proxy_fault=False)
                self.assertIsNone(adapter.acquire("task-empty", allowed_ids=set()))
            finally:
                adapter.stop(force=True)

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

    def test_authenticated_socks_is_bridged_by_transport_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.inline_nodes = ["socks5://user:pass@127.0.0.1:18081#AUTH"]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            nodes, summary = adapter._collect_nodes(force_refresh=True)
            self.assertEqual(nodes, [])
            self.assertEqual(summary["core_candidates"], 1)
            self.assertEqual(summary["skipped_nodes"], 0)
            self.assertEqual(adapter._core_candidates[0].core_config["type"], "socks5")

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

    def test_health_and_retry_eligibility_are_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.fail_cooldown_seconds = 0
            settings.proxy.inline_nodes = ["http://127.0.0.1:18080#PROBE"]
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            adapter.probe = lambda **_: {"total": 1, "healthy": 0, "results": []}
            adapter.start(force_refresh=True)
            try:
                node = adapter.status()["nodes"][0]
                self.assertFalse(node["healthy"])
                self.assertTrue(node["retry_eligible"])
                self.assertEqual(adapter.status()["retry_eligible"], 1)

                lease = adapter.acquire("task-after-cooldown")
                self.assertIsNotNone(lease)
                adapter.release("task-after-cooldown", proxy_fault=False)
                released = adapter.status()["nodes"][0]
                self.assertFalse(released["healthy"])
                self.assertTrue(released["retry_eligible"])
            finally:
                adapter.stop(force=True)


if __name__ == "__main__":
    unittest.main()
