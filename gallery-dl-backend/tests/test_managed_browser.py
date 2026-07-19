from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from gdl_backend.managed_browser import (
    MANAGED_BROWSER_SITES,
    _is_pixiv_oauth_callback,
    _page_websocket,
    capture_pixiv_oauth_callback,
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

    def test_page_websocket_is_bound_to_requested_target(self):
        with patch(
            "gdl_backend.managed_browser._devtools_json",
            return_value=[
                {
                    "id": "other",
                    "type": "page",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:32123/devtools/page/other",
                },
                {
                    "id": "pixiv-target",
                    "type": "page",
                    "webSocketDebuggerUrl": (
                        "ws://127.0.0.1:32123/devtools/page/pixiv-target"
                    ),
                },
            ],
        ):
            self.assertEqual(
                _page_websocket(
                    "ws://127.0.0.1:32123/devtools/browser/session",
                    "pixiv-target",
                ),
                "ws://127.0.0.1:32123/devtools/page/pixiv-target",
            )

    def test_twitter_login_waits_until_account_access_challenge_is_left(self):
        with patch(
            "gdl_backend.managed_browser.cdp_request",
            return_value={
                "targetInfos": [
                    {
                        "targetId": "twitter-target",
                        "type": "page",
                        "url": "https://x.com/account/access",
                    }
                ]
            },
        ):
            self.assertFalse(
                managed_login_ready(
                    "ws://127.0.0.1:32123/devtools/browser/id",
                    MANAGED_BROWSER_SITES["twitter"],
                    "twitter-target",
                )
            )

        with patch(
            "gdl_backend.managed_browser.cdp_request",
            return_value={
                "targetInfos": [
                    {
                        "targetId": "twitter-target",
                        "type": "page",
                        "url": "https://x.com/home",
                    }
                ]
            },
        ):
            self.assertTrue(
                managed_login_ready(
                    "ws://127.0.0.1:32123/devtools/browser/id",
                    MANAGED_BROWSER_SITES["twitter"],
                    "twitter-target",
                )
            )

    def test_pixiv_callback_capture_enables_network_before_navigation(self):
        callback = (
            "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
            "?state=STATE&code=CODE"
        )

        class Connection:
            def __init__(self):
                self.sent = []
                self.messages = [
                    {"id": 1, "result": {}},
                    {"id": 2, "result": {}},
                    {"id": 3, "result": {}},
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {"request": {"url": callback}},
                    },
                ]
                self.closed = False

            def send(self, value):
                self.sent.append(json.loads(value))

            def recv(self):
                return json.dumps(self.messages.pop(0))

            def settimeout(self, _timeout):
                pass

            def close(self):
                self.closed = True

        connection = Connection()
        login_url = (
            "https://app-api.pixiv.net/web/v1/login?client=pixiv-android"
            "&code_challenge=CHALLENGE"
        )
        with (
            patch(
                "gdl_backend.managed_browser._page_websocket",
                return_value="ws://127.0.0.1:32123/devtools/page/page-id",
            ),
            patch(
                "gdl_backend.managed_browser.websocket.create_connection",
                return_value=connection,
            ),
        ):
            result = capture_pixiv_oauth_callback(
                "ws://127.0.0.1:32123/devtools/browser/browser-id",
                "page-id",
                login_url,
            )

        self.assertEqual(result, callback)
        self.assertEqual(
            [message["method"] for message in connection.sent],
            [
                "Network.enable",
                "Page.enable",
                "Page.navigate",
            ],
        )
        self.assertEqual(connection.sent[-1]["params"], {"url": login_url})
        self.assertTrue(connection.closed)

    def test_pixiv_callback_is_not_lost_before_navigate_acknowledgement(self):
        callback = (
            "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
            "?state=STATE&code=CODE"
        )

        class Connection:
            def __init__(self):
                self.messages = [
                    {"id": 1, "result": {}},
                    {"id": 2, "result": {}},
                    {
                        "method": "Network.requestWillBeSent",
                        "params": {"request": {"url": callback}},
                    },
                    {"id": 3, "result": {}},
                ]
                self.closed = False

            def send(self, _value):
                pass

            def recv(self):
                return json.dumps(self.messages.pop(0))

            def settimeout(self, _timeout):
                pass

            def close(self):
                self.closed = True

        connection = Connection()
        with (
            patch(
                "gdl_backend.managed_browser._page_websocket",
                return_value="ws://127.0.0.1:32123/devtools/page/page-id",
            ),
            patch(
                "gdl_backend.managed_browser.websocket.create_connection",
                return_value=connection,
            ),
        ):
            result = capture_pixiv_oauth_callback(
                "ws://127.0.0.1:32123/devtools/browser/browser-id",
                "page-id",
                "https://app-api.pixiv.net/web/v1/login"
                "?client=pixiv-android&code_challenge=CHALLENGE",
            )

        self.assertEqual(result, callback)
        self.assertTrue(connection.closed)

    def test_pixiv_callback_requires_state_and_code(self):
        base = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
        self.assertTrue(_is_pixiv_oauth_callback(f"{base}?state=STATE&code=CODE"))
        self.assertFalse(_is_pixiv_oauth_callback(f"{base}?code=CODE"))
        self.assertFalse(_is_pixiv_oauth_callback(f"{base}?state=STATE"))
        self.assertFalse(_is_pixiv_oauth_callback(f"{base}?state=&code=CODE"))


if __name__ == "__main__":
    unittest.main()
