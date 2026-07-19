from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
