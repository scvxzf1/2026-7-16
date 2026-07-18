from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from gdl_backend.app import ServiceContainer, _validate_network_target, create_app
from gdl_backend.discovery import DiscoveryError

from tests.helpers import make_settings


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name))
        self.settings.server.api_key = "test-key"
        self.container = ServiceContainer(self.settings)
        self.app = create_app(self.settings, container=self.container, start_background=False)
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.headers = {"X-API-Key": "test-key"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_health_and_auth(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/tasks").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self.headers).status_code, 200)

    def test_webui_static_assets_and_root_link(self):
        root = self.client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.json()["ui"], "/ui/")

        index = self.client.get("/ui/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("聚合爬取测试台", index.text)
        self.assertIn('id="ehTagFilter"', index.text)
        self.assertIn('id="ehTagGroups"', index.text)
        self.assertIn('id="authCenter"', index.text)
        self.assertIn('data-managed-browser-auth="twitter"', index.text)
        self.assertIn('data-managed-browser-auth="exhentai"', index.text)
        self.assertIn('id="startPixivOAuth"', index.text)
        self.assertIn('id="cancelPixivOAuth"', index.text)
        self.assertNotIn('id="authBrowser"', index.text)
        self.assertNotIn('data-browser-auth=', index.text)
        self.assertNotIn("Cookie 文件</span><span>gallery-dl 配置", index.text)
        self.assertIn("text/html", index.headers["content-type"])

        script = self.client.get("/ui/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("/api/v1/search", script.text)
        self.assertIn("/api/v1/crawls", script.text)
        self.assertIn("address-thumbnail", script.text)
        self.assertIn("gallery-tags", script.text)
        self.assertIn("ehEntryMatchesTagFilter", script.text)
        self.assertIn("eh-tag-option", script.text)
        self.assertIn("keyword_gallery_search", script.text)
        self.assertIn("/api/v1/auth", script.text)
        self.assertIn("startManagedBrowserLogin", script.text)
        self.assertIn("scheduleBrowserLoginPoll", script.text)
        self.assertNotIn("importBrowserLogin", script.text)
        self.assertIn("completePixivOAuth", script.text)

        styles = self.client.get("/ui/styles.css")
        self.assertEqual(styles.status_code, 200)
        self.assertIn(".source-card", styles.text)
        self.assertIn(".address-thumbnail", styles.text)
        self.assertIn(".gallery-tag", styles.text)
        self.assertIn(".eh-tag-filter", styles.text)
        self.assertIn(".eh-tag-option.exclude", styles.text)
        self.assertIn(".auth-center", styles.text)
        self.assertIn(".oauth-panel", styles.text)

    def test_managed_auth_api_contract(self):
        self.assertEqual(self.client.get("/api/v1/auth").status_code, 401)
        listing = self.client.get("/api/v1/auth", headers=self.headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(
            [item["site"] for item in listing.json()["items"]],
            ["danbooru", "twitter", "pixiv", "exhentai"],
        )
        self.assertFalse(listing.json()["secrets_exposed"])
        unknown = self.client.get("/api/v1/auth/unknown", headers=self.headers)
        self.assertEqual(unknown.status_code, 404)

        browser_status = {
            "site": "twitter",
            "label": "X / Twitter",
            "method": "managed_browser",
            "state": "authorizing",
            "authorized": False,
            "summary": "项目专属浏览器已打开。",
            "actions": ["managed_browser_login", "clear"],
        }
        browser_session = {
            "session_id": "b" * 32,
            "site": "twitter",
            "state": "awaiting_login",
            "message": "请完成登录。",
            "created_at": 1,
            "expires_at": 901,
            "cookie_count": 0,
            "recommended_missing": [],
            "error": None,
        }
        browser_result = {"status": browser_status, "session": browser_session}
        self.container.auth.start_browser_login = AsyncMock(return_value=browser_result)
        started_browser = self.client.post(
            "/api/v1/auth/twitter/login/start",
            headers=self.headers,
        )
        self.assertEqual(started_browser.status_code, 202, started_browser.text)
        self.assertEqual(started_browser.json()["session"]["session_id"], "b" * 32)
        self.container.auth.start_browser_login.assert_awaited_once_with("twitter")

        self.container.auth.browser_login_session = Mock(return_value=browser_result)
        polled = self.client.get(
            f"/api/v1/auth/twitter/login/{'b' * 32}",
            headers=self.headers,
        )
        self.assertEqual(polled.status_code, 200, polled.text)
        self.container.auth.browser_login_session.assert_called_once_with("twitter", "b" * 32)

        self.container.auth.cancel_browser_login = AsyncMock(return_value=browser_result)
        closed = self.client.delete(
            f"/api/v1/auth/twitter/login/{'b' * 32}",
            headers=self.headers,
        )
        self.assertEqual(closed.status_code, 200, closed.text)
        self.container.auth.cancel_browser_login.assert_awaited_once_with("twitter", "b" * 32)
        self.assertEqual(
            self.client.post("/api/v1/auth/twitter/browser", headers=self.headers, json={}).status_code,
            404,
        )

        session = {
            "session_id": "a" * 32,
            "state": "awaiting_code",
            "authorization_url": "https://app-api.pixiv.net/web/v1/login?state=test",
            "created_at": 1,
            "expires_at": 601,
            "error": None,
        }
        self.container.auth.start_pixiv_oauth = AsyncMock(return_value=session)
        started = self.client.post("/api/v1/auth/pixiv/oauth/start", headers=self.headers)
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["session_id"], "a" * 32)

        pixiv_status = {
            "site": "pixiv",
            "label": "Pixiv",
            "method": "oauth",
            "state": "authorized",
            "authorized": True,
            "summary": "Pixiv 登录授权有效。",
            "actions": ["oauth", "clear"],
        }
        self.container.auth.complete_pixiv_oauth = AsyncMock(return_value=pixiv_status)
        completed = self.client.post(
            "/api/v1/auth/pixiv/oauth/complete",
            headers=self.headers,
            json={"session_id": "a" * 32, "callback": "https://callback/?code=VALUE"},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertTrue(completed.json()["authorized"])
        self.container.auth.complete_pixiv_oauth.assert_awaited_once_with(
            "a" * 32, "https://callback/?code=VALUE"
        )

        self.container.auth.cancel_pixiv_oauth = AsyncMock(return_value=pixiv_status)
        cancelled = self.client.delete(
            "/api/v1/auth/pixiv/oauth/session",
            headers=self.headers,
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.container.auth.cancel_pixiv_oauth.assert_awaited_once()

        self.container.auth.clear = AsyncMock(return_value={**pixiv_status, "authorized": False})
        cleared = self.client.delete("/api/v1/auth/pixiv", headers=self.headers)
        self.assertEqual(cleared.status_code, 200, cleared.text)

    def test_task_idempotency_cancel_logs_and_files(self):
        body = {"url": "https://www.pixiv.net/artworks/123456", "proxy_mode": "direct"}
        headers = {**self.headers, "Idempotency-Key": "same-request"}
        first = self.client.post("/api/v1/tasks", headers=headers, json=body)
        self.assertEqual(first.status_code, 202, first.text)
        second = self.client.post("/api/v1/tasks", headers=headers, json=body)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        task_id = first.json()["id"]
        cancelled = self.client.post(f"/api/v1/tasks/{task_id}/cancel", headers=self.headers)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}/logs", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}/files", headers=self.headers).status_code, 200)

    def test_task_automatically_uses_managed_site_login(self):
        cookie_path = self.container.auth.managed_dir / "twitter.cookies.txt"
        cookie_path.write_text(
            "# Netscape HTTP Cookie File\n\n"
            ".x.com\tTRUE\t/\tTRUE\t4102444800\tauth_token\tSECRET\n"
            ".x.com\tTRUE\t/\tTRUE\t4102444800\tct0\tSECRET2\n",
            encoding="utf-8",
        )
        response = self.client.post(
            "/api/v1/tasks",
            headers=self.headers,
            json={"url": "https://x.com/example/status/123456", "proxy_mode": "direct"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["cookies_file"], str(cookie_path))

    def test_site_policy_crud_and_proxy_status(self):
        policy = {
            "max_concurrency": 1,
            "retry_limit": 1,
            "backoff_base_seconds": 0,
            "proxy_mode": "required",
            "probe_url": "https://www.pixiv.net/",
            "probe_before_use": True,
            "node_tags": ["jp"],
            "http_timeout": 15,
            "gallery_retries": 1,
            "task_timeout_seconds": 60,
            "extra_args": [],
        }
        response = self.client.put("/api/v1/sites/policies/pixiv", headers=self.headers, json=policy)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["policy"]["node_tags"], ["jp"])
        status = self.client.get("/api/v1/proxy/status", headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["running"])
        self.assertTrue(status.json()["managed_by_backend"])
        self.assertFalse(status.json()["auto_start"])
        self.assertEqual(status.json()["engine"], "native")
        self.assertFalse(status.json()["executable_required"])

    def test_private_target_guard(self):
        with self.assertRaises(ValueError):
            _validate_network_target("http://127.0.0.1:8080/a", False)
        _validate_network_target("http://127.0.0.1:8080/a", True)
        mixed = [
            (None, None, None, None, ("127.0.0.1", 443)),
            (None, None, None, None, ("1.1.1.1", 443)),
        ]
        with patch("gdl_backend.app.socket.getaddrinfo", return_value=mixed):
            _validate_network_target("https://example.com/a", False)

    def test_known_site_must_match_extractor(self):
        response = self.client.post(
            "/api/v1/tasks",
            headers=self.headers,
            json={
                "url": "https://www.pixiv.net/artworks/123456",
                "site": "twitter",
                "proxy_mode": "direct",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "invalid_task")

    def test_non_loopback_bind_requires_api_key(self):
        self.settings.server.host = "0.0.0.0"
        self.settings.server.api_key = ""
        with self.assertRaises(ValueError):
            self.settings.validate()

    def test_search_sites_and_grouped_source_addresses(self):
        sites = self.client.get("/api/v1/search/sites", headers=self.headers)
        self.assertEqual(sites.status_code, 200)
        self.assertEqual(
            {item["site"] for item in sites.json()["items"]},
            {"twitter", "pixiv", "danbooru", "exhentai"},
        )
        eh_catalog = next(
            item for item in sites.json()["items"] if item["site"] == "exhentai"
        )
        self.assertEqual(
            {item["namespace"] for item in eh_catalog["tag_namespaces"]},
            {
                "artist",
                "character",
                "cosplayer",
                "female",
                "group",
                "language",
                "location",
                "male",
                "mixed",
                "other",
                "parody",
                "reclass",
                "temp",
            },
        )
        expected = {
            "site": "twitter",
            "keyword": "clover days",
            "search_url": "https://x.com/search?q=clover+days",
            "candidate_count": 1,
            "author_count": 1,
            "candidates": [
                {
                    "id": "123",
                    "site": "twitter",
                    "kind": "work",
                    "url": "https://x.com/artist/status/123",
                    "download_url": "https://x.com/artist/status/123",
                    "media_count": 1,
                    "author": {"id": "42"},
                }
            ],
            "authors": [
                {
                    "id": "42",
                    "site": "twitter",
                    "kind": "author",
                    "name": "artist",
                    "display_name": "clover days",
                    "url": "https://x.com/artist",
                    "works_url": "https://x.com/artist/media",
                }
            ],
            "proxy": {"mode": "direct", "used": False},
            "attempts": 1,
        }
        self.container.discovery.search = AsyncMock(return_value=expected)
        response = self.client.post(
            "/api/v1/search",
            headers=self.headers,
            json={
                "sites": ["x"],
                "keyword": "clover days",
                "limit": 10,
                "proxy_mode": "direct",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["source_count"], 1)
        self.assertEqual(payload["address_count"], 1)
        self.assertEqual(payload["weak_evidence_count"], 0)
        self.assertEqual(payload["sources"][0]["site"], "twitter")
        self.assertEqual(payload["sources"][0]["addresses"][0]["url"], "https://x.com/artist/media")
        self.assertEqual(payload["sources"][0]["addresses"][0]["confidence"], "verified")
        self.assertEqual(payload["sources"][0]["weak_evidence"], [])
        self.assertEqual(payload["selection_contract"]["execution_order"], "source_then_address")
        self.assertEqual(payload["selection_contract"]["default_visibility"], "addresses_only")
        self.assertEqual(self.container.discovery.search.await_args.kwargs["site"], "twitter")

    def test_exhentai_search_returns_selectable_galleries_with_previews(self):
        raw = {
            "site": "exhentai",
            "keyword": "ogipote",
            "search_url": "https://e-hentai.org/?f_search=ogipote",
            "candidate_count": 1,
            "author_count": 0,
            "candidates": [
                {
                    "id": "3079340",
                    "site": "exhentai",
                    "kind": "gallery",
                    "title": "Gallery 3079340",
                    "url": "https://e-hentai.org/g/3079340/991425f1c4/",
                    "download_url": "https://e-hentai.org/g/3079340/991425f1c4/",
                    "thumbnail_url": None,
                    "media_count": None,
                    "metadata": {"gallery_token": "991425f1c4"},
                }
            ],
            "authors": [],
            "proxy": {"used": False},
            "attempts": 1,
        }
        enriched = {
            **raw,
            "preview_count": 1,
            "preview_missing_count": 0,
            "candidates": [
                {
                    **raw["candidates"][0],
                    "title": "(C104) Catchy & Punk",
                    "thumbnail_url": "https://ehgt.org/w/cover.webp",
                    "media_count": 13,
                    "metadata": {
                        "gallery_token": "991425f1c4",
                        "tags": ["artist:ogipote"],
                    },
                }
            ],
        }
        self.container.discovery.search = AsyncMock(return_value=raw)
        self.container.discovery.enrich_exhentai_previews = AsyncMock(
            return_value=enriched
        )

        response = self.client.post(
            "/api/v1/search",
            headers=self.headers,
            json={
                "sites": ["eh"],
                "keyword": "ogipote",
                "limit": 20,
                "proxy_mode": "direct",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        source = response.json()["sources"][0]
        self.assertEqual(source["address_count"], 1)
        self.assertEqual(source["weak_evidence_count"], 0)
        self.assertEqual(source["preview_count"], 1)
        self.assertEqual(source["preview_missing_count"], 0)
        gallery = source["addresses"][0]
        self.assertEqual(gallery["confidence"], "site_search")
        self.assertEqual(gallery["label"], "(C104) Catchy & Punk")
        self.assertEqual(gallery["thumbnail_url"], "https://ehgt.org/w/cover.webp")
        self.assertEqual(gallery["media_count"], 13)
        self.assertEqual(gallery["metadata"]["tags"], ["artist:ogipote"])
        self.assertEqual(source["tag_facets"][0]["namespace"], "artist")
        self.assertEqual(source["tag_facets"][0]["tags"][0]["tag"], "artist:ogipote")
        self.assertEqual(source["weak_evidence"], [])
        self.assertEqual(
            response.json()["tag_filter_contract"]["same_namespace"],
            "or",
        )
        self.container.discovery.enrich_exhentai_previews.assert_awaited_once()

    def test_exhentai_preview_failure_keeps_all_galleries_selectable(self):
        raw = {
            "site": "exhentai",
            "keyword": "ogipote",
            "search_url": "https://e-hentai.org/?f_search=ogipote",
            "candidate_count": 1,
            "author_count": 0,
            "candidates": [
                {
                    "id": "3079340",
                    "site": "exhentai",
                    "kind": "gallery",
                    "title": "Gallery 3079340",
                    "url": "https://e-hentai.org/g/3079340/991425f1c4/",
                    "download_url": "https://e-hentai.org/g/3079340/991425f1c4/",
                    "thumbnail_url": None,
                    "media_count": None,
                    "metadata": {"gallery_token": "991425f1c4"},
                }
            ],
            "authors": [],
            "proxy": {"used": False},
            "attempts": 1,
        }
        self.container.discovery.search = AsyncMock(return_value=raw)
        self.container.discovery.enrich_exhentai_previews = AsyncMock(
            side_effect=DiscoveryError("exhentai_preview_lookup_failed", "temporary")
        )

        response = self.client.post(
            "/api/v1/search",
            headers=self.headers,
            json={
                "sites": ["eh"],
                "keyword": "ogipote",
                "limit": 20,
                "proxy_mode": "direct",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        source = response.json()["sources"][0]
        self.assertEqual(source["status"], "partial")
        self.assertEqual(source["address_count"], 1)
        self.assertEqual(source["weak_evidence_count"], 0)
        self.assertEqual(source["preview_count"], 0)
        self.assertEqual(source["preview_missing_count"], 1)
        self.assertEqual(source["addresses"][0]["confidence"], "site_search")
        self.assertEqual(
            source["enrichment_errors"][0]["stage"],
            "exhentai_gallery_previews",
        )

    def test_cross_source_search_uses_danbooru_curated_profiles(self):
        async def search_side_effect(*, site, keyword, **kwargs):
            if site == "danbooru":
                return {
                    "site": site,
                    "keyword": keyword,
                    "search_url": "https://danbooru.donmai.us/posts?tags=artist_name",
                    "candidate_count": 1,
                    "author_count": 1,
                    "candidates": [
                        {
                            "id": "10",
                            "site": "danbooru",
                            "kind": "post",
                            "metadata": {
                                "artists": ["artist_name"],
                                "characters": ["character_name"],
                            },
                        }
                    ],
                    "authors": [
                        {
                            "name": "artist_name",
                            "url": "https://danbooru.donmai.us/artists?search[name]=artist_name",
                            "works_url": "https://danbooru.donmai.us/posts?tags=artist_name",
                        }
                    ],
                    "proxy": {"used": False},
                    "attempts": 1,
                }
            if site == "pixiv":
                return {
                    "site": site,
                    "keyword": keyword,
                    "search_url": "https://www.pixiv.net/en/tags/artist%20name/artworks",
                    "candidate_count": 2,
                    "author_count": 2,
                    "candidates": [
                        {
                            "id": "9990",
                            "site": "pixiv",
                            "kind": "work",
                            "author": {"id": "999", "name": "fan_account"},
                        },
                        {
                            "id": "770",
                            "site": "pixiv",
                            "kind": "work",
                            "author": {"id": "77", "name": "artist_archive"},
                        }
                    ],
                    "authors": [
                        {
                            "id": "999",
                            "name": "fan_account",
                            "display_name": "Artist Name Fan",
                            "url": "https://www.pixiv.net/users/999",
                            "works_url": "https://www.pixiv.net/users/999/artworks",
                        },
                        {
                            "id": "77",
                            "name": "artist_archive",
                            "display_name": "Artist Name Archive",
                            "url": "https://www.pixiv.net/users/77",
                            "works_url": "https://www.pixiv.net/users/77/artworks",
                        }
                    ],
                    "proxy": {"used": False},
                    "attempts": 1,
                }
            return {
                "site": site,
                "keyword": keyword,
                "search_url": "https://example.invalid/search",
                "candidate_count": 0,
                "author_count": 0,
                "candidates": [],
                "authors": [],
                "proxy": {"used": False},
                "attempts": 1,
            }

        self.container.discovery.search = AsyncMock(side_effect=search_side_effect)
        self.container.discovery.search_danbooru_artists = AsyncMock(
            return_value={"authors": []}
        )
        self.container.discovery.danbooru_artist_profiles = AsyncMock(
            return_value=(
                [
                    {
                        "id": "55",
                        "name": "artist_name",
                        "other_names": ["Artist Name"],
                        "group_name": None,
                        "profile_url": "https://danbooru.donmai.us/artists/55",
                        "related_profiles": [
                            {
                                "url": "https://x.com/artist_name",
                                "platform": "twitter",
                                "crawl_site": "twitter",
                                "crawl_url": "https://x.com/artist_name/media",
                                "active": True,
                            },
                            {
                                "url": "https://www.pixiv.net/users/77",
                                "platform": "pixiv",
                                "crawl_site": "pixiv",
                                "crawl_url": "https://www.pixiv.net/users/77/artworks",
                                "active": True,
                            },
                            {
                                "url": "https://x.com/artist_alt",
                                "platform": "twitter",
                                "crawl_site": "twitter",
                                "crawl_url": "https://x.com/artist_alt/media",
                                "active": True,
                            },
                        ],
                    }
                ],
                [],
            )
        )
        response = self.client.post(
            "/api/v1/search",
            headers=self.headers,
            json={
                "sites": ["danbooru", "x", "pixiv"],
                "keyword": "artist name",
                "limit": 2,
                "proxy_mode": "direct",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [item["site"] for item in response.json()["sources"]],
            ["danbooru", "twitter", "pixiv"],
        )
        sources = {item["site"]: item for item in response.json()["sources"]}
        dan_types = {item["address_type"] for item in sources["danbooru"]["addresses"]}
        self.assertEqual(dan_types, {"artist_tag"})
        self.assertNotIn(
            "character_name",
            {item.get("tag") for item in sources["danbooru"]["addresses"]},
        )
        self.assertEqual(sources["twitter"]["addresses"][0]["url"], "https://x.com/artist_name/media")
        self.assertEqual(sources["pixiv"]["addresses"][0]["url"], "https://www.pixiv.net/users/77/artworks")
        self.assertEqual(sources["twitter"]["addresses"][0]["origin"], "danbooru_artist_url")
        self.assertEqual(sources["pixiv"]["addresses"][0]["confidence"], "verified")
        self.assertEqual(
            sources["pixiv"]["addresses"][0]["origins"],
            ["site_search", "danbooru_artist_url"],
        )
        self.assertIn(
            "danbooru_artist_url",
            sources["pixiv"]["addresses"][0]["evidence_reasons"],
        )
        self.assertEqual(sources["pixiv"]["weak_evidence_count"], 1)
        self.assertEqual(
            sources["pixiv"]["weak_evidence"][0]["url"],
            "https://www.pixiv.net/users/999/artworks",
        )
        self.assertEqual(
            sources["pixiv"]["weak_evidence"][0]["confidence"], "weak_evidence"
        )
        self.assertNotIn(
            "https://www.pixiv.net/users/999/artworks",
            {item["url"] for item in sources["pixiv"]["addresses"]},
        )
        self.assertEqual(len(sources["twitter"]["addresses"]), 2)
        self.assertEqual(response.json()["weak_evidence_count"], 1)
        self.assertEqual(len(response.json()["related_profiles"]), 3)

    def test_cross_source_search_preserves_order_when_one_source_fails(self):
        async def search_side_effect(*, site, keyword, **kwargs):
            if site == "pixiv":
                raise DiscoveryError("extractor_error", "pixiv login expired")
            return {
                "site": site,
                "keyword": keyword,
                "search_url": "https://x.com/search?q=artist",
                "candidate_count": 0,
                "author_count": 0,
                "candidates": [],
                "authors": [],
                "proxy": {"used": False},
                "attempts": 1,
            }

        self.container.discovery.search = AsyncMock(side_effect=search_side_effect)
        response = self.client.post(
            "/api/v1/search",
            headers=self.headers,
            json={"keyword": "artist", "sites": ["pixiv", "x"], "proxy_mode": "direct"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [source["site"] for source in response.json()["sources"]],
            ["pixiv", "twitter"],
        )
        self.assertEqual(response.json()["sources"][0]["status"], "failed")
        self.assertEqual(response.json()["sources"][1]["status"], "succeeded")

    def test_ordered_crawl_sequence_and_batch_idempotency(self):
        async def discover_side_effect(*, site, url, **kwargs):
            count = 2 if url.endswith("artist-a/media") else 1
            work_id = "1" if "artist-a" in url else "2"
            work_url = (
                f"https://x.com/example/status/{work_id}"
                if site == "twitter"
                else f"https://www.pixiv.net/artworks/{work_id}"
            )
            return {
                "candidates": [
                    {
                        "id": work_id,
                        "site": site,
                        "kind": "work",
                        "url": work_url,
                        "media_count": count,
                    }
                ]
            }

        self.container.discovery.discover_url = AsyncMock(side_effect=discover_side_effect)
        body = {
            "sources": [
                {
                    "site": "x",
                    "addresses": [
                        {"url": "https://x.com/artist-a/media", "label": "A"},
                        {"url": "https://x.com/artist-b/media", "label": "B"},
                    ],
                    "extra_args": ["--filter", "favorite_count >= 0"],
                },
                {
                    "site": "pixiv",
                    "addresses": [
                        {"url": "https://www.pixiv.net/users/77/artworks", "label": "P"}
                    ],
                },
            ],
            "concurrency": 20,
            "proxy_mode": "direct",
            "extra_args": ["--sleep", "0"],
        }
        headers = {**self.headers, "Idempotency-Key": "ordered-batch"}
        first = self.client.post("/api/v1/crawls", headers=headers, json=body)
        self.assertEqual(first.status_code, 202, first.text)
        batch_id = first.json()["id"]
        self.assertEqual(first.json()["task_count"], 0)
        self.assertEqual(first.json()["execution_order"], "source_then_address")

        second = self.client.post("/api/v1/crawls", headers=headers, json=body)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["id"], batch_id)

        import asyncio

        asyncio.run(self.container.ordered_crawls.run_once())
        active = self.container.db.get_crawl_batch(batch_id)
        first_address = active["sources"][0]["addresses"][0]
        self.assertEqual(first_address["status"], "running")
        self.assertEqual(first_address["planned_task_count"], 2)
        self.assertEqual(active["sources"][0]["addresses"][1]["status"], "pending")
        self.assertEqual(active["sources"][1]["addresses"][0]["status"], "pending")
        tasks = self.container.db.list_crawl_tasks(batch_id)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task["address_order"] == 0 for task in tasks))
        self.assertEqual(tasks[0]["policy"]["max_concurrency"], 20)
        self.assertEqual(
            tasks[0]["extra_args"][:4],
            ["--sleep", "0", "--filter", "favorite_count >= 0"],
        )
        listing = self.client.get("/api/v1/crawls?limit=1", headers=self.headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"][0]["id"], batch_id)
        detail = self.client.get(f"/api/v1/crawls/{batch_id}", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        task_page = self.client.get(
            f"/api/v1/crawls/{batch_id}/tasks?limit=1",
            headers=self.headers,
        )
        self.assertEqual(task_page.status_code, 200, task_page.text)
        self.assertEqual(len(task_page.json()["items"]), 1)
        missing = self.client.get("/api/v1/crawls/missing", headers=self.headers)
        self.assertEqual(missing.status_code, 404)

        for task in tasks:
            self.container.db.complete_task(task["id"], "succeeded")
        asyncio.run(self.container.ordered_crawls.run_once())
        asyncio.run(self.container.ordered_crawls.run_once())
        progressed = self.container.db.get_crawl_batch(batch_id)
        self.assertEqual(progressed["sources"][0]["addresses"][0]["status"], "succeeded")
        self.assertEqual(progressed["sources"][0]["addresses"][1]["status"], "running")
        self.assertEqual(progressed["sources"][1]["addresses"][0]["status"], "pending")
        self.assertEqual(len(self.container.db.list_crawl_tasks(batch_id)), 3)

    def test_crawl_contract_rejects_legacy_modes_and_non_gallery_eh_address(self):
        legacy = self.client.post(
            "/api/v1/crawls",
            headers=self.headers,
            json={
                "items": [{"url": "https://x.com/artist/media"}],
                "fanout": "media",
            },
        )
        self.assertEqual(legacy.status_code, 422)

        invalid_eh = self.client.post(
            "/api/v1/crawls",
            headers=self.headers,
            json={
                "sources": [
                    {
                        "site": "eh",
                        "addresses": [{"url": "https://e-hentai.org/?f_search=tag"}],
                    }
                ],
                "proxy_mode": "direct",
            },
        )
        self.assertEqual(invalid_eh.status_code, 422, invalid_eh.text)
        self.assertEqual(invalid_eh.json()["error"]["code"], "invalid_crawl")

    def test_partial_enqueue_drains_current_address_before_next_address(self):
        import asyncio

        self.container.discovery.discover_url = AsyncMock(
            return_value={
                "candidates": [
                    {
                        "id": "1",
                        "site": "twitter",
                        "kind": "work",
                        "url": "https://x.com/artist/status/1",
                        "media_count": 2,
                    }
                ]
            }
        )
        response = self.client.post(
            "/api/v1/crawls",
            headers=self.headers,
            json={
                "sources": [
                    {
                        "site": "twitter",
                        "addresses": [
                            {"url": "https://x.com/artist/media"},
                            {"url": "https://x.com/artist2/media"},
                        ],
                    }
                ],
                "proxy_mode": "direct",
            },
        )
        batch_id = response.json()["id"]
        original_enqueue = self.container.ordered_crawls._enqueue
        calls = 0

        async def flaky_enqueue(body, key, concurrency):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected enqueue failure")
            return await original_enqueue(body, key, concurrency)

        self.container.ordered_crawls.set_enqueue(flaky_enqueue)
        asyncio.run(self.container.ordered_crawls.run_once())
        batch = self.container.db.get_crawl_batch(batch_id)
        self.assertEqual(batch["sources"][0]["addresses"][0]["status"], "running")
        self.assertEqual(batch["sources"][0]["addresses"][1]["status"], "pending")

        asyncio.run(self.container.ordered_crawls.run_once())
        batch = self.container.db.get_crawl_batch(batch_id)
        self.assertEqual(batch["sources"][0]["addresses"][0]["status"], "failed")
        self.assertEqual(batch["sources"][0]["addresses"][1]["status"], "pending")
        self.container.ordered_crawls.set_enqueue(original_enqueue)

    def test_manager_cancellation_drains_linked_tasks_without_replanning(self):
        import asyncio

        self.container.discovery.discover_url = AsyncMock(
            return_value={
                "candidates": [
                    {
                        "id": "1",
                        "site": "twitter",
                        "kind": "work",
                        "url": "https://x.com/artist/status/1",
                        "media_count": 2,
                    }
                ]
            }
        )
        response = self.client.post(
            "/api/v1/crawls",
            headers=self.headers,
            json={
                "sources": [
                    {
                        "site": "twitter",
                        "addresses": [
                            {"url": "https://x.com/artist/media"},
                            {"url": "https://x.com/artist2/media"},
                        ],
                    }
                ],
                "proxy_mode": "direct",
            },
        )
        batch_id = response.json()["id"]
        original_enqueue = self.container.ordered_crawls._enqueue

        async def scenario():
            second_enqueue = asyncio.Event()
            calls = 0

            async def blocking_enqueue(body, key, concurrency):
                nonlocal calls
                calls += 1
                if calls == 2:
                    second_enqueue.set()
                    await asyncio.Event().wait()
                return await original_enqueue(body, key, concurrency)

            self.container.ordered_crawls.set_enqueue(blocking_enqueue)
            worker = asyncio.create_task(self.container.ordered_crawls.run_once())
            await asyncio.wait_for(second_enqueue.wait(), timeout=1)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

        try:
            asyncio.run(scenario())
            batch = self.container.db.get_crawl_batch(batch_id)
            first = batch["sources"][0]["addresses"][0]
            self.assertEqual(first["status"], "running")
            self.assertEqual(first["planned_task_count"], 1)
            self.assertEqual(batch["sources"][0]["addresses"][1]["status"], "pending")
            linked = self.container.db.crawl_address_tasks(first["id"])
            self.assertEqual(len(linked), 1)
            self.assertEqual(linked[0]["status"], "cancelled")

            asyncio.run(self.container.ordered_crawls.run_once())
            batch = self.container.db.get_crawl_batch(batch_id)
            self.assertEqual(batch["sources"][0]["addresses"][0]["status"], "failed")
            self.assertEqual(batch["sources"][0]["addresses"][1]["status"], "pending")
        finally:
            self.container.ordered_crawls.set_enqueue(original_enqueue)

    def test_cancel_queued_ordered_crawl(self):
        response = self.client.post(
            "/api/v1/crawls",
            headers=self.headers,
            json={
                "sources": [
                    {
                        "site": "danbooru",
                        "addresses": [
                            {"url": "https://danbooru.donmai.us/posts?tags=artist_name"}
                        ],
                    }
                ],
                "proxy_mode": "direct",
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        batch_id = response.json()["id"]
        cancelled = self.client.post(f"/api/v1/crawls/{batch_id}/cancel", headers=self.headers)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(cancelled.json()["sources"][0]["addresses"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
