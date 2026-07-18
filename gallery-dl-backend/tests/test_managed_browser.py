from __future__ import annotations

import unittest
from unittest.mock import call, patch

from gdl_backend.managed_browser import (
    MANAGED_BROWSER_SITES,
    clear_site_cookies,
    managed_login_ready,
    read_browser_websocket,
)


class ManagedBrowserTests(unittest.TestCase):
    def test_browser_websocket_is_discovered_from_fixed_loopback_port(self):
        with patch(
            "gdl_backend.managed_browser._devtools_json",
            return_value={
                "webSocketDebuggerUrl": "ws://127.0.0.1:32123/devtools/browser/session"
            },
        ):
            self.assertEqual(
                read_browser_websocket(32123),
                (32123, "ws://127.0.0.1:32123/devtools/browser/session"),
            )

    def test_clear_site_cookies_deletes_only_login_cookies_on_page_target(self):
        cookies = [
            {"name": "auth_token", "domain": ".x.com", "path": "/"},
            {"name": "ct0", "domain": ".x.com", "path": "/"},
            {"name": "cf_clearance", "domain": ".x.com", "path": "/"},
        ]
        with (
            patch(
                "gdl_backend.managed_browser._page_websocket",
                return_value="ws://127.0.0.1:32123/devtools/page/page-id",
            ),
            patch("gdl_backend.managed_browser.get_site_cookies", return_value=cookies),
            patch("gdl_backend.managed_browser.cdp_request") as request,
        ):
            clear_site_cookies(
                "ws://127.0.0.1:32123/devtools/browser/browser-id",
                MANAGED_BROWSER_SITES["twitter"],
            )

        self.assertEqual(
            request.call_args_list,
            [
                call(
                    "ws://127.0.0.1:32123/devtools/page/page-id",
                    "Network.deleteCookies",
                    {"name": "auth_token", "domain": ".x.com", "path": "/"},
                ),
                call(
                    "ws://127.0.0.1:32123/devtools/page/page-id",
                    "Network.deleteCookies",
                    {"name": "ct0", "domain": ".x.com", "path": "/"},
                ),
            ],
        )

    def test_twitter_login_waits_until_account_access_challenge_is_left(self):
        with patch(
            "gdl_backend.managed_browser.cdp_request",
            return_value={
                "targetInfos": [
                    {
                        "type": "page",
                        "url": "https://x.com/account/access",
                    }
                ]
            },
        ):
            self.assertFalse(
                managed_login_ready("ws://127.0.0.1:32123/devtools/browser/id", MANAGED_BROWSER_SITES["twitter"])
            )

        with patch(
            "gdl_backend.managed_browser.cdp_request",
            return_value={
                "targetInfos": [
                    {
                        "type": "page",
                        "url": "https://x.com/home",
                    }
                ]
            },
        ):
            self.assertTrue(
                managed_login_ready("ws://127.0.0.1:32123/devtools/browser/id", MANAGED_BROWSER_SITES["twitter"])
            )


if __name__ == "__main__":
    unittest.main()
