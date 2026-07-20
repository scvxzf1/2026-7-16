from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gdl_backend.config import AppSettings


class ConfigDefaultsTests(unittest.TestCase):
    def test_native_pool_is_the_managed_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = AppSettings.load(Path(temporary) / "missing-config.json")

        self.assertTrue(settings.proxy.enabled)
        self.assertTrue(settings.proxy.auto_start)
        self.assertEqual(settings.proxy.engine, "native")
        self.assertTrue(settings.proxy.allow_socks)
        self.assertIsNone(settings.proxy.node_file)
        self.assertEqual(settings.proxy.probe_timeout_seconds, 10.0)
        self.assertFalse(hasattr(settings.proxy, "max_nodes"))
        self.assertNotIn("max_nodes", settings.public_dict()["proxy"])

        self.assertIsNone(settings.proxy.transport_core_binary)
        self.assertEqual(settings.proxy.transport_core_sha256, "")

    def test_transport_core_defaults_are_external_on_all_platforms(self):
        settings = AppSettings()
        self.assertIsNone(settings.proxy.transport_core_binary)
        self.assertEqual(settings.proxy.transport_core_sha256, "")

    def test_explicit_transport_core_path_and_digest_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "proxy": {
                            "transport_core_binary": "bin/mihomo",
                            "transport_core_sha256": "1" * 64,
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = AppSettings.load(config_path)

        self.assertEqual(settings.proxy.transport_core_binary, (root / "bin" / "mihomo").resolve())
        self.assertEqual(settings.proxy.transport_core_sha256, "1" * 64)


if __name__ == "__main__":
    unittest.main()
