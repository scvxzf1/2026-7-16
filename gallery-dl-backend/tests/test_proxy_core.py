from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gdl_backend.proxy import ProxyPoolAdapter
from gdl_backend.proxy_core import CoreEndpoint, build_transport_config, resolve_core_binary
from gdl_backend.proxy_sources import parse_subscription_text

from tests.helpers import make_settings


CLASH_TUNNEL_FIXTURE = """
proxies:
  - name: JP-TROJAN
    type: trojan
    server: jp.example
    port: 443
    password: fixture-secret
    sni: edge.example
  - name: US-VLESS
    type: vless
    server: us.example
    port: 443
    uuid: 11111111-1111-1111-1111-111111111111
    tls: true
  - name: SG-MIERU
    type: mieru
    server: sg.example
    port: 8443
    username: fixture
    password: fixture-secret
"""


class ProxyCoreTests(unittest.TestCase):
    def test_core_binary_sha256_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "proxy-core.exe"
            binary.write_bytes(b"fixture-core")
            digest = hashlib.sha256(b"fixture-core").hexdigest()
            self.assertEqual(resolve_core_binary(binary, digest), binary.resolve())
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                resolve_core_binary(binary, "0" * 64)

    def test_clash_tunnel_nodes_keep_private_core_config(self):
        nodes = parse_subscription_text(CLASH_TUNNEL_FIXTURE)
        self.assertEqual([node.scheme for node in nodes], ["trojan", "vless", "mieru"])
        self.assertTrue(all(not node.usable for node in nodes))
        self.assertTrue(all(node.core_config for node in nodes))
        self.assertNotIn("fixture-secret", repr(nodes[0]))

    def test_builds_one_local_http_listener_per_tunnel_node(self):
        nodes = parse_subscription_text(CLASH_TUNNEL_FIXTURE)
        config, endpoints = build_transport_config(
            nodes,
            listen_host="127.0.0.1",
            base_port=29100,
        )
        self.assertEqual(len(config["proxies"]), 3)
        self.assertEqual(len(config["listeners"]), 3)
        self.assertEqual([item["port"] for item in config["listeners"]], [29100, 29101, 29102])
        self.assertEqual(
            [item["proxy"] for item in config["listeners"]],
            [item["name"] for item in config["proxies"]],
        )
        self.assertEqual(endpoints[0].local_http, "http://127.0.0.1:29100")

    def test_mieru_port_range_does_not_reintroduce_a_single_port(self):
        nodes = parse_subscription_text(
            "mieru://user:secret@range.example:5000"
            "?transport=tcp&multiplexing=low&port-range=5000-5010#RANGE"
        )
        config, endpoints = build_transport_config(
            nodes,
            listen_host="127.0.0.1",
            base_port=29100,
        )
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(config["proxies"][0]["port-range"], "5000-5010")
        self.assertNotIn("port", config["proxies"][0])

    def test_adapter_wires_core_endpoints_into_native_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            settings.proxy.enabled = True
            settings.proxy.transport_core_enabled = True
            node_file = Path(tmp) / "nodes.yaml"
            node_file.write_text(CLASH_TUNNEL_FIXTURE, encoding="utf-8")
            settings.proxy.node_file = node_file
            adapter = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
            adapter.probe = lambda **_: {"total": 3, "healthy": 3, "results": []}
            fake_endpoints = [
                CoreEndpoint(
                    id=f"node-{index}",
                    name=f"fixture-{index}",
                    source_protocol="trojan",
                    source_host="fixture.example",
                    local_http=f"http://127.0.0.1:{29100 + index}",
                )
                for index in range(3)
            ]
            with patch("gdl_backend.proxy.TunnelTransportCore") as core_class:
                core = core_class.return_value
                core.start.return_value = fake_endpoints
                core.status.return_value = {"enabled": True, "running": True, "listeners": 3}
                started = adapter.start(force_refresh=True)
                self.assertEqual(
                    core_class.call_args.kwargs["expected_sha256"],
                    settings.proxy.transport_core_sha256,
                )
                self.assertEqual(started["status"]["sources"]["core_nodes"], 3)
                self.assertEqual(started["status"]["total"], 3)
                lease = adapter.acquire("fixture-task")
                self.assertIsNotNone(lease)
                self.assertTrue(lease.endpoint.startswith("http://127.0.0.1:291"))
                adapter.release("fixture-task", proxy_fault=False)
                adapter.stop()
                core.stop.assert_called()


if __name__ == "__main__":
    unittest.main()
