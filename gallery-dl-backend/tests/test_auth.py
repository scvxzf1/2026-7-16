from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import ANY, AsyncMock, patch

from gdl_backend.auth import AuthError, AuthManager, ManagedBrowserHost, PixivOAuthSession

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

    def test_shared_project_browser_login_persists_cookie_and_profile(self):
        class BrowserProcess:
            def __init__(self):
                self.returncode = None

            async def wait(self):
                self.returncode = 0
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
            spawn = AsyncMock(return_value=BrowserProcess())
            with (
                patch("gdl_backend.auth.discover_chrome_executable", return_value=Path("chrome.exe")),
                patch("gdl_backend.auth.asyncio.create_subprocess_exec", new=spawn),
                patch("gdl_backend.auth.allocate_debug_port", return_value=32123),
                patch("gdl_backend.auth.read_browser_websocket", return_value=(32123, "ws://browser")),
                patch("gdl_backend.auth.open_login_target", return_value="twitter-target"),
                patch("gdl_backend.auth.get_site_cookies", return_value=cookies),
                patch("gdl_backend.auth.managed_login_ready", return_value=True),
                patch("gdl_backend.auth.close_target"),
            ):
                started = await self.auth.start_browser_login("twitter")
                session_id = started["session"]["session_id"]
                session = self.auth._browser_sessions[session_id]
                await session.monitor_task
                command = spawn.await_args.args
                self.assertIn("--remote-debugging-port=32123", command)
                self.assertIn("--remote-debugging-address=127.0.0.1", command)
                self.assertIn("--profile-directory=Default", command)
                self.assertIn(f"--user-data-dir={self.auth.browser_profile_dir}", command)
                self.assertNotIn("--remote-debugging-port=0", command)
                return self.auth.browser_login_session("twitter", session_id)

        result = asyncio.run(run_login())
        self.assertEqual(result["session"]["state"], "authorized")
        self.assertTrue(result["status"]["authorized"])
        self.assertIsNone(result["status"]["invalidated_at"])
        self.assertTrue(self.auth.managed_credentials_available("twitter", str(previous_cookie)))
        self.assertTrue((self.auth.managed_dir / "twitter.cookies.txt").is_file())
        self.assertTrue(self.auth.browser_profile_dir.is_dir())
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

    def test_site_clear_removes_exported_credential_but_keeps_shared_profile(self):
        profile_marker = self.auth.browser_profile_dir / "stable-device-marker"
        profile_marker.parent.mkdir(parents=True, exist_ok=True)
        profile_marker.write_text("keep", encoding="utf-8")
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
        self.assertTrue(profile_marker.exists())

        twitter = self.auth.managed_dir / "twitter.cookies.txt"
        write_cookies(
            twitter,
            [(".x.com", "auth_token", "AUTH"), (".x.com", "ct0", "CSRF")],
        )
        asyncio.run(self.auth.clear("twitter"))
        self.assertFalse(twitter.exists())
        self.assertTrue(profile_marker.exists())

        result = asyncio.run(self.auth.clear_browser_profile())
        self.assertFalse(profile_marker.exists())
        self.assertFalse(result["browser_profile"]["present"])

    def test_clear_browser_profile_keeps_exported_site_credentials(self):
        profile_marker = self.auth.browser_profile_dir / "profile-marker"
        profile_marker.parent.mkdir(parents=True, exist_ok=True)
        profile_marker.write_text("profile", encoding="utf-8")
        twitter = self.auth.managed_dir / "twitter.cookies.txt"
        write_cookies(
            twitter,
            [(".x.com", "auth_token", "AUTH"), (".x.com", "ct0", "CSRF")],
        )
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

        asyncio.run(self.auth.clear_browser_profile())

        self.assertFalse(profile_marker.exists())
        self.assertTrue(twitter.exists())
        self.assertTrue(self.auth.status("twitter")["authorized"])
        self.assertTrue(self.auth.status("pixiv")["authorized"])

    def test_pixiv_oauth_enables_input_for_piped_stdin(self):
        class BlockingStream:
            def __init__(self):
                self.calls = 0
                self.blocked = asyncio.Event()

            async def read(self, _size):
                self.calls += 1
                if self.calls == 1:
                    return b"https://app-api.pixiv.net/web/v1/login?client=pixiv-android\n"
                await self.blocked.wait()
                return b""

        class OAuthProcess:
            def __init__(self):
                self.stdout = BlockingStream()
                self.stdin = object()
                self.returncode = None

            async def wait(self):
                await asyncio.Event().wait()

        async def run_start():
            process = OAuthProcess()
            spawn = AsyncMock(return_value=process)
            browser_start = AsyncMock()
            with (
                patch("gdl_backend.auth.asyncio.create_subprocess_exec", new=spawn),
                patch.object(self.auth, "_start_pixiv_browser", new=browser_start),
            ):
                started = await self.auth.start_pixiv_oauth()
            command = spawn.await_args.args
            self.assertIn("extractor.oauth.browser=false", command)
            self.assertIn("extractor.oauth.input=true", command)
            self.assertEqual(command[-1], "oauth:pixiv")
            self.assertIs(spawn.await_args.kwargs["stdin"], asyncio.subprocess.PIPE)
            self.assertEqual(started["state"], "awaiting_code")

            session = self.auth._pixiv_session
            self.assertIsNotNone(session)
            browser_start.assert_awaited_once_with(session)
            session.reader_task.cancel()
            await asyncio.gather(session.reader_task, return_exceptions=True)
            self.auth._pixiv_session = None
            await self.auth._release_authorization(session.id)
            self.auth._cleanup_oauth_cache(session.cache_file)

        asyncio.run(run_start())

    def test_shared_browser_host_is_reused_and_uses_one_profile(self):
        class BrowserProcess:
            def __init__(self):
                self.returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        async def run_host():
            spawn = AsyncMock(return_value=BrowserProcess())
            with (
                patch("gdl_backend.auth.discover_chrome_executable", return_value=Path("chrome.exe")),
                patch("gdl_backend.auth.allocate_debug_port", return_value=32123),
                patch("gdl_backend.auth.asyncio.create_subprocess_exec", new=spawn),
                patch(
                    "gdl_backend.auth.read_browser_websocket",
                    return_value=(32123, "ws://browser"),
                ),
            ):
                first = await self.auth._ensure_browser_host()
                second = await self.auth._ensure_browser_host()
            command = spawn.await_args.args
            self.assertIs(first, second)
            self.assertEqual(spawn.await_count, 1)
            self.assertIn("--remote-debugging-port=32123", command)
            self.assertIn("--remote-debugging-address=127.0.0.1", command)
            self.assertIn("--profile-directory=Default", command)
            self.assertNotIn("--disable-blink-features=AutomationControlled", command)
            self.assertIn("--start-maximized", command)
            self.assertIn(f"--user-data-dir={self.auth.browser_profile_dir}", command)
            self.assertEqual(command[-1], "about:blank")

        asyncio.run(run_host())

    def test_shared_browser_serializes_site_authorization(self):
        async def run():
            self.auth._active_authorization_id = "active-session"
            with self.assertRaises(AuthError) as browser_error:
                await self.auth.start_browser_login("twitter")
            self.assertEqual(browser_error.exception.code, "shared_browser_busy")
            with self.assertRaises(AuthError) as pixiv_error:
                await self.auth.start_pixiv_oauth()
            self.assertEqual(pixiv_error.exception.code, "shared_browser_busy")
            self.auth._active_authorization_id = ""

        asyncio.run(run())

    def test_pixiv_browser_monitor_captures_and_completes_callback(self):
        class OAuthProcess:
            returncode = None

        class BrowserProcess:
            returncode = None

        session_cache = self.auth.managed_dir / ".pixiv-oauth-browser-test.sqlite3"
        self.auth._ensure_cache(session_cache)
        session = PixivOAuthSession(
            id="browser-test",
            process=OAuthProcess(),
            created_at=time.time(),
            cache_file=session_cache,
            authorization_url=(
                "https://app-api.pixiv.net/web/v1/login?client=pixiv-android"
                "&code_challenge=CHALLENGE"
            ),
            state="starting_browser",
        )
        self.auth._pixiv_session = session
        self.auth._active_authorization_id = session.id
        callback = (
            "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
            "?state=STATE&code=CODE"
        )

        async def complete(current, value):
            self.assertIs(current, session)
            self.assertEqual(value, callback)
            session.state = "authorized"
            session.message = "Pixiv 登录授权完成。"
            self.auth._pixiv_session = None
            return {"authorized": True}

        host = ManagedBrowserHost(
            process=BrowserProcess(),
            profile_dir=self.auth.browser_profile_dir,
            debug_port=32123,
            websocket_url="ws://browser",
        )
        with (
            patch.object(self.auth, "_ensure_browser_host", new=AsyncMock(return_value=host)),
            patch("gdl_backend.auth.open_login_target", return_value="pixiv-target"),
            patch("gdl_backend.auth.capture_pixiv_oauth_callback", return_value=callback) as capture,
            patch.object(self.auth, "_complete_pixiv_oauth", side_effect=complete) as complete_mock,
            patch("gdl_backend.auth.close_target") as close,
        ):
            asyncio.run(self.auth._monitor_pixiv_browser(session))

        capture.assert_called_once_with(
            "ws://browser",
            "pixiv-target",
            session.authorization_url,
            timeout=ANY,
        )
        complete_mock.assert_awaited_once_with(session, callback)
        close.assert_called_once_with("ws://browser", "pixiv-target")
        self.assertEqual(session.state, "authorized")
        self.assertEqual(self.auth._active_authorization_id, "")
        self.auth._cleanup_oauth_cache(session_cache)

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
