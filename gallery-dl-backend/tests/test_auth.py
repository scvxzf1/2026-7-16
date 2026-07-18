from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gdl_backend.auth import AuthManager, PixivOAuthSession

from tests.helpers import make_settings


def write_cookies(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    expires = int(time.time()) + 86400
    lines = ["# Netscape HTTP Cookie File", ""]
    for domain, name, value in rows:
        lines.append(f"{domain}\tTRUE\t/\tTRUE\t{expires}\t{name}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class AuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name))
        self.auth = AuthManager(self.settings)

    def tearDown(self):
        asyncio.run(self.auth.stop())
        self.temp.cleanup()

    def test_public_statuses_do_not_expose_secrets(self):
        payload = self.auth.statuses()
        by_site = {item["site"]: item for item in payload["items"]}
        self.assertEqual(by_site["danbooru"]["state"], "ready")
        self.assertEqual(by_site["twitter"]["state"], "required")
        self.assertEqual(by_site["twitter"]["method"], "managed_browser")
        self.assertEqual(by_site["pixiv"]["state"], "required")
        self.assertFalse(payload["secrets_exposed"])
        serialized = json.dumps(payload)
        self.assertNotIn("refresh_token", serialized)
        self.assertNotIn("cookies_file", serialized)

    def test_managed_cookie_file_is_automatically_resolved(self):
        path = self.auth.managed_dir / "twitter.cookies.txt"
        write_cookies(
            path,
            [
                (".x.com", "auth_token", "AUTH_SECRET"),
                (".x.com", "ct0", "CSRF_SECRET"),
            ],
        )
        status = self.auth.status("twitter")
        self.assertTrue(status["authorized"])
        self.assertEqual(status["cookies"]["required_present"], ["auth_token", "ct0"])
        credentials = self.auth.credentials_for("twitter")
        self.assertEqual(credentials["cookies_file"], str(path))
        self.assertNotIn("AUTH_SECRET", json.dumps(status))

    def test_authentication_failure_invalidates_only_managed_cookie_file(self):
        path = self.auth.managed_dir / "twitter.cookies.txt"
        write_cookies(
            path,
            [
                (".x.com", "auth_token", "AUTH_SECRET"),
                (".x.com", "ct0", "CSRF_SECRET"),
            ],
        )
        changed = asyncio.run(
            self.auth.invalidate_if_managed(
                "twitter",
                str(path),
                "authenticated cookies needed to access this timeline",
            )
        )
        self.assertTrue(changed)
        status = self.auth.status("twitter")
        self.assertEqual(status["state"], "required")
        self.assertFalse(status["authorized"])
        self.assertIsNotNone(status["invalidated_at"])
        self.assertIsNone(self.auth.credentials_for("twitter")["cookies_file"])
        self.assertFalse(self.auth.managed_credentials_available("twitter", str(path)))

        restarted = AuthManager(self.settings)
        try:
            self.assertEqual(restarted.status("twitter")["state"], "required")
            self.assertIsNone(restarted.credentials_for("twitter")["cookies_file"])
        finally:
            asyncio.run(restarted.stop())

    def test_account_access_error_does_not_invalidate_managed_login(self):
        path = self.auth.managed_dir / "twitter.cookies.txt"
        write_cookies(
            path,
            [
                (".x.com", "auth_token", "AUTH_SECRET"),
                (".x.com", "ct0", "CSRF_SECRET"),
            ],
        )
        changed = asyncio.run(
            self.auth.invalidate_if_managed(
                "twitter",
                str(path),
                "AuthorizationError: artist's Tweets are protected\nworker exited with status 1",
            )
        )
        self.assertFalse(changed)
        self.assertTrue(self.auth.status("twitter")["authorized"])

        eh_path = self.auth.managed_dir / "exhentai.cookies.txt"
        write_cookies(
            eh_path,
            [
                (".e-hentai.org", "ipb_member_id", "MEMBER"),
                (".e-hentai.org", "ipb_pass_hash", "HASH"),
            ],
        )
        changed = asyncio.run(
            self.auth.invalidate_if_managed(
                "exhentai",
                str(eh_path),
                "AuthorizationError: Temporarily Banned",
            )
        )
        self.assertFalse(changed)
        self.assertTrue(self.auth.status("exhentai")["authorized"])

    def test_project_browser_login_persists_cookie_and_profile(self):
        class BrowserProcess:
            returncode = None

            async def wait(self):
                return 0

        cookies = [
            {
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "expires": time.time() + 86400,
                "name": "auth_token",
                "value": "AUTH_SECRET",
            },
            {
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "expires": time.time() + 86400,
                "name": "ct0",
                "value": "CSRF_SECRET",
            },
        ]
        previous_cookie = self.auth.managed_dir / "twitter.cookies.txt"
        write_cookies(previous_cookie, [(".x.com", "auth_token", "OLD_SECRET")])
        asyncio.run(
            self.auth.invalidate_if_managed(
                "twitter",
                str(previous_cookie),
                "authenticated cookies needed to access this timeline",
            )
        )
        self.assertFalse(self.auth.managed_credentials_available("twitter", str(previous_cookie)))

        async def run_login():
            self.auth._shutdown_browser_process = AsyncMock()
            spawn = AsyncMock(return_value=BrowserProcess())
            with (
                patch("gdl_backend.auth.discover_chrome_executable", return_value=Path("chrome.exe")),
                patch("gdl_backend.auth.asyncio.create_subprocess_exec", new=spawn),
                patch("gdl_backend.auth.allocate_debug_port", return_value=32123),
                patch("gdl_backend.auth.read_browser_websocket", return_value=(32123, "ws://browser")),
                patch("gdl_backend.auth.clear_site_cookies"),
                patch("gdl_backend.auth.open_login_target"),
                patch("gdl_backend.auth.get_site_cookies", return_value=cookies),
                patch("gdl_backend.auth.managed_login_ready", return_value=True),
            ):
                started = await self.auth.start_browser_login("twitter")
                command = spawn.await_args.args
                self.assertIn("--remote-debugging-port=32123", command)
                self.assertIn("--remote-debugging-address=127.0.0.1", command)
                self.assertNotIn("--remote-debugging-port=0", command)
                session_id = started["session"]["session_id"]
                session = self.auth._browser_sessions[session_id]
                await session.monitor_task
                return self.auth.browser_login_session("twitter", session_id)

        result = asyncio.run(run_login())
        self.assertEqual(result["session"]["state"], "authorized")
        self.assertTrue(result["status"]["authorized"])
        self.assertIsNone(result["status"]["invalidated_at"])
        self.assertTrue(self.auth.managed_credentials_available("twitter", str(previous_cookie)))
        self.assertTrue((self.auth.managed_dir / "twitter.cookies.txt").is_file())
        self.assertTrue((self.auth.browser_profiles_dir / "twitter").is_dir())
        serialized = json.dumps(result)
        self.assertNotIn("AUTH_SECRET", serialized)
        self.assertNotIn(str(self.auth.browser_profiles_dir), serialized)

        restarted = AuthManager(self.settings)
        try:
            self.assertTrue(restarted.status("twitter")["authorized"])
            self.assertEqual(
                restarted.credentials_for("twitter")["cookies_file"],
                str(restarted.managed_dir / "twitter.cookies.txt"),
            )
        finally:
            asyncio.run(restarted.stop())

    def test_pixiv_cache_authorizes_and_clear_removes_it(self):
        with closing(sqlite3.connect(self.auth.cache_file)) as db:
            db.execute(
                "INSERT INTO data (key, value, expires) VALUES (?, ?, ?)",
                (
                    "gallery_dl.extractor.pixiv._refresh_token_cache-None",
                    "REFRESH_SECRET",
                    int(time.time()) + 86400,
                ),
            )
            db.commit()
        self.assertTrue(self.auth.status("pixiv")["authorized"])
        cleared = asyncio.run(self.auth.clear("pixiv"))
        self.assertFalse(cleared["authorized"])
        with closing(sqlite3.connect(self.auth.cache_file)) as db:
            count = db.execute(
                "SELECT count(*) FROM data WHERE key LIKE 'gallery_dl.extractor.pixiv.%'"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_unknown_gallery_site_has_no_managed_credentials(self):
        self.assertEqual(
            self.auth.credentials_for("example-site"),
            {"cookies_file": None, "config_file": None, "credentials_ref": None},
        )

    def test_pixiv_oauth_reader_uses_isolated_session_cache(self):
        class EmptyStream:
            async def read(self, _size):
                return b""

        class SuccessfulProcess:
            def __init__(self):
                self.stdout = EmptyStream()
                self.returncode = 0

            async def wait(self):
                return self.returncode

        with closing(sqlite3.connect(self.auth.cache_file)) as db:
            db.execute(
                "INSERT INTO data (key, value, expires) VALUES (?, ?, ?)",
                (
                    "gallery_dl.extractor.pixiv._refresh_token_cache-None",
                    "EXISTING_REFRESH_SECRET",
                    int(time.time()) + 86400,
                ),
            )
            db.commit()

        session_cache = self.auth.managed_dir / ".pixiv-oauth-test.sqlite3"
        self.auth._ensure_cache(session_cache)
        session = PixivOAuthSession(
            id="test",
            process=SuccessfulProcess(),
            created_at=time.time(),
            cache_file=session_cache,
            state="exchanging",
        )
        asyncio.run(self.auth._read_pixiv_oauth(session))
        self.assertEqual(session.state, "failed")

        with closing(sqlite3.connect(session_cache)) as db:
            db.execute(
                "INSERT INTO data (key, value, expires) VALUES (?, ?, ?)",
                (
                    "gallery_dl.extractor.pixiv._refresh_token_cache-None",
                    "NEW_REFRESH_SECRET",
                    int(time.time()) + 86400,
                ),
            )
            db.commit()
        session.state = "exchanging"
        asyncio.run(self.auth._read_pixiv_oauth(session))
        self.assertEqual(session.state, "token_ready")
        self.auth._cleanup_oauth_cache(session_cache)


if __name__ == "__main__":
    unittest.main()
