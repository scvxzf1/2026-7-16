from __future__ import annotations

import asyncio
import json
import os
import requests
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gdl_backend.discovery import (
    DiscoveryService,
    DiscoveryError,
    canonical_gallery_address,
    classify_external_profile,
    discovery_addresses,
    exhentai_tag_facets,
    parse_discovery_output,
    search_site,
    validate_discovery_args,
)
from gdl_backend.gallery import GalleryCaptureResult, GalleryRunner
from gdl_backend.proxy import ProxyLease
from gdl_backend.schemas import SitePolicy
from tests.helpers import make_settings


class _FakeGallery:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.calls = []

    async def capture(self, operation_id, **kwargs):
        self.calls.append((operation_id, kwargs))
        return GalleryCaptureResult(0, self.stdout, "", False, "marker", 123)


class _FakeProxy:
    def __init__(self):
        self.acquired = []
        self.released = []

    def acquire(self, task_id, **kwargs):
        self.acquired.append((task_id, kwargs))
        return ProxyLease(
            task_id=task_id,
            node_id="node-1",
            endpoint="http://127.0.0.1:29001",
            name="JP-1",
            protocol="trojan",
            tags=["jp"],
            acquired_at=1.0,
        )

    def release(self, task_id, **kwargs):
        self.released.append((task_id, kwargs))


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b""):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.pid = 123
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0


class DiscoveryParserTests(unittest.TestCase):
    def test_site_aliases_and_search_urls(self):
        self.assertEqual(search_site("x").site, "twitter")
        self.assertEqual(search_site("eh").site, "exhentai")
        self.assertIn("q=clover+days", search_site("twitter").search_url("clover days"))
        self.assertIn("tags=clover_days", search_site("danbooru").search_url("clover_days"))
        self.assertIn("f_search=clover+days", search_site("exhentai").search_url("clover days"))
        with self.assertRaises(ValueError):
            search_site("pixiv").search_url("clover days")

    def test_exhentai_tag_facets_follow_official_namespaces(self):
        facets = exhentai_tag_facets(
            [
                {
                    "metadata": {
                        "tags": [
                            "artist:Ogipote",
                            "a:ogipote",
                            "language:english",
                            "m:glasses",
                            "temporary tag",
                            "custom:value",
                        ]
                    }
                },
                {
                    "metadata": {
                        "tags": ["artist:ogipote", "language:japanese"]
                    }
                },
            ]
        )
        self.assertEqual(
            [facet["namespace"] for facet in facets],
            ["artist", "language", "male", "temp", "unknown"],
        )
        artist = facets[0]
        self.assertEqual(artist["gallery_count"], 2)
        self.assertEqual(artist["tags"][0]["count"], 2)
        self.assertEqual(artist["tags"][0]["tag"].lower(), "artist:ogipote")
        language = facets[1]
        self.assertEqual(language["tag_count"], 2)
        self.assertEqual(facets[2]["tags"][0]["tag"], "male:glasses")
        self.assertEqual(facets[3]["tags"][0]["tag"], "temp:temporary tag")
        self.assertEqual(facets[4]["label"], "未识别命名空间")

    def test_twitter_candidates_and_authors(self):
        payload = [
            [
                2,
                {
                    "tweet_id": 123456789,
                    "content": "sample post",
                    "count": 2,
                    "author": {"id": 42, "name": "artist", "nick": "Artist"},
                    "favorite_count": 10,
                    "hashtags": ["art"],
                },
            ],
            [
                3,
                "https://pbs.twimg.com/media/sample?format=jpg&name=orig",
                {"tweet_id": 123456789, "num": 1},
            ],
            [
                3,
                "https://video.twimg.com/ext_tw_video/123/pu/vid/1280x720/sample.mp4?tag=12",
                {"tweet_id": 123456789, "num": 2},
            ],
        ]
        candidates, authors = parse_discovery_output(
            "twitter",
            json.dumps(payload),
            source_url="https://x.com/search?q=sample",
            limit=20,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], "https://x.com/artist/status/123456789")
        self.assertEqual(candidates[0]["media_count"], 2)
        self.assertEqual(
            candidates[0]["thumbnail_url"],
            "https://pbs.twimg.com/media/sample?format=jpg&name=orig",
        )
        self.assertEqual(
            candidates[0]["media_urls"],
            [
                "https://pbs.twimg.com/media/sample?format=jpg&name=orig",
                "https://video.twimg.com/ext_tw_video/123/pu/vid/1280x720/sample.mp4?tag=12",
            ],
        )
        self.assertEqual(authors[0]["works_url"], "https://x.com/artist/media")

    def test_twitter_author_deduplication_is_case_insensitive(self):
        payload = [
            [
                2,
                {
                    "tweet_id": 1,
                    "count": 1,
                    "author": {"name": "Artist_Name", "nick": "Artist"},
                },
            ],
            [
                2,
                {
                    "tweet_id": 2,
                    "count": 1,
                    "author": {"name": "artist_name", "nick": "Artist"},
                },
            ],
        ]
        candidates, authors = parse_discovery_output(
            "twitter",
            json.dumps(payload),
            source_url="https://x.com/search?q=artist",
            limit=20,
        )
        self.assertEqual(len(authors), 1)
        addresses = discovery_addresses(
            "twitter",
            {"candidates": candidates, "authors": authors},
            keyword="artist",
            limit=20,
        )
        self.assertEqual(len(addresses), 1)
        self.assertEqual(addresses[0]["url"], "https://x.com/artist_name/media")
        self.assertEqual(addresses[0]["matched_items"], 2)
        self.assertNotEqual(addresses[0]["id"], "twitter:account:")

    def test_pixiv_candidates_and_authors(self):
        payload = [
            [
                2,
                {
                    "id": 9988,
                    "title": "sample artwork",
                    "count": 3,
                    "user": {"id": 77, "account": "artist77", "name": "Artist 77"},
                    "tags": [{"name": "clover_days"}],
                    "rating": "General",
                },
            ],
            [3, "https://i.pximg.net/img-original/sample.jpg", {"id": 9988}],
        ]
        candidates, authors = parse_discovery_output(
            "pixiv",
            json.dumps(payload),
            source_url="https://www.pixiv.net/en/tags/sample/artworks",
            limit=20,
        )
        self.assertEqual(candidates[0]["url"], "https://www.pixiv.net/artworks/9988")
        self.assertEqual(candidates[0]["media_count"], 3)
        self.assertEqual(candidates[0]["metadata"]["tags"], ["clover_days"])
        self.assertEqual(authors[0]["works_url"], "https://www.pixiv.net/users/77/artworks")

    def test_danbooru_and_eh_candidates(self):
        danbooru = [
            [
                2,
                {
                    "id": 123,
                    "tags_artist": ["artist_name"],
                    "tags_copyright": ["clover_days"],
                    "preview_file_url": "https://cdn.donmai.us/preview.jpg",
                    "source": "https://www.pixiv.net/artworks/9988",
                    "rating": "g",
                },
            ]
        ]
        candidates, authors = parse_discovery_output(
            "danbooru",
            json.dumps(danbooru),
            source_url="https://danbooru.donmai.us/posts?tags=clover_days",
            limit=20,
        )
        self.assertEqual(candidates[0]["url"], "https://danbooru.donmai.us/posts/123")
        self.assertEqual(candidates[0]["source_url"], "https://www.pixiv.net/artworks/9988")
        self.assertIn("tags=artist_name", authors[0]["works_url"])

        artist_queue = [
            [
                6,
                "https://danbooru.donmai.us/posts?tags=artist_name",
                {
                    "id": 55,
                    "name": "artist_name",
                    "other_names": ["Artist Name"],
                    "group_name": "Circle",
                },
            ]
        ]
        candidates, authors = parse_discovery_output(
            "danbooru",
            json.dumps(artist_queue),
            source_url="https://danbooru.donmai.us/artists?search=x",
            limit=20,
        )
        self.assertEqual(candidates, [])
        self.assertEqual(authors[0]["id"], "55")
        self.assertEqual(authors[0]["other_names"], ["Artist Name"])

        eh = [
            [
                6,
                "https://e-hentai.org/g/1531036/91cbde3481/",
                {"gallery_id": 1531036, "gallery_token": "91cbde3481"},
            ]
        ]
        candidates, authors = parse_discovery_output(
            "exhentai",
            json.dumps(eh),
            source_url="https://e-hentai.org/?f_search=clover",
            limit=20,
        )
        self.assertEqual(candidates[0]["kind"], "gallery")
        self.assertEqual(candidates[0]["id"], "1531036")
        self.assertEqual(authors, [])
        galleries = discovery_addresses(
            "exhentai",
            {"candidates": candidates, "authors": authors},
            keyword="clover",
            limit=20,
        )
        self.assertEqual(galleries[0]["confidence"], "site_search")
        self.assertEqual(galleries[0]["evidence_reasons"], ["keyword_gallery_search"])

    def test_selectable_addresses_are_accounts_tags_and_galleries(self):
        twitter = discovery_addresses(
            "twitter",
            {
                "candidates": [
                    {
                        "author": {"id": "42"},
                        "thumbnail_url": "https://pbs.twimg.com/a.jpg",
                    }
                ],
                "authors": [
                    {
                        "id": "42",
                        "name": "artist",
                        "url": "https://x.com/artist",
                        "works_url": "https://x.com/artist/media",
                    }
                ],
            },
            keyword="artist",
            limit=20,
        )
        self.assertEqual([item["address_type"] for item in twitter], ["account"])
        self.assertEqual(twitter[0]["url"], "https://x.com/artist/media")
        self.assertEqual(twitter[0]["confidence"], "verified")
        self.assertEqual(
            twitter[0]["evidence_reasons"],
            ["site_search_work_evidence", "account_name_exact_match"],
        )

        danbooru = discovery_addresses(
            "danbooru",
            {
                "candidates": [
                    {
                        "metadata": {
                            "artists": ["artist_name"],
                            "characters": ["character_name"],
                        }
                    },
                ],
                "authors": [
                    {
                        "name": "artist_name",
                        "url": "https://danbooru.donmai.us/artists?search[name]=artist_name",
                        "works_url": "https://danbooru.donmai.us/posts?tags=artist_name",
                    }
                ],
            },
            keyword="artist_name",
            limit=20,
        )
        self.assertEqual([item["address_type"] for item in danbooru], ["artist_tag"])
        self.assertEqual(danbooru[0]["tag"], "artist_name")
        self.assertEqual(danbooru[0]["confidence"], "verified")

        character = discovery_addresses(
            "danbooru",
            {
                "candidates": [
                    {
                        "metadata": {
                            "artists": ["unrelated_artist"],
                            "characters": ["Character_Name", "unrelated_character"],
                        }
                    }
                ],
                "authors": [],
            },
            keyword="character name",
            limit=20,
        )
        self.assertEqual([item["address_type"] for item in character], ["character_tag"])
        self.assertEqual(character[0]["tag"], "Character_Name")
        self.assertEqual(character[0]["evidence_reasons"], ["character_tag_exact_match"])

    def test_danbooru_directory_drops_unrelated_artist_results(self):
        addresses = discovery_addresses(
            "danbooru",
            {
                "candidates": [],
                "authors": [
                    {
                        "name": "other_artist",
                        "other_names": ["Someone Else"],
                        "origin": "danbooru_artist_directory",
                        "works_url": "https://danbooru.donmai.us/posts?tags=other_artist",
                    }
                ],
            },
            keyword="target_artist",
            limit=20,
        )
        self.assertEqual(addresses, [])

    def test_danbooru_directory_prefers_primary_name_over_conflicting_alias(self):
        addresses = discovery_addresses(
            "danbooru",
            {
                "candidates": [],
                "authors": [
                    {
                        "name": "rurudo",
                        "other_names": ["kajuu_aisu"],
                        "origin": "danbooru_artist_directory",
                        "works_url": "https://danbooru.donmai.us/posts?tags=rurudo",
                    },
                    {
                        "name": "kajuu_aisu",
                        "other_names": ["rurudo"],
                        "origin": "danbooru_artist_directory",
                        "works_url": "https://danbooru.donmai.us/posts?tags=kajuu_aisu",
                    },
                ],
            },
            keyword="rurudo",
            limit=20,
        )
        self.assertEqual([item["tag"] for item in addresses], ["rurudo"])
        self.assertEqual(addresses[0]["confidence"], "verified")
        self.assertEqual(
            addresses[0]["evidence_reasons"],
            ["danbooru_artist_directory_match"],
        )

    def test_danbooru_directory_alias_only_match_is_weak_evidence(self):
        addresses = discovery_addresses(
            "danbooru",
            {
                "candidates": [],
                "authors": [
                    {
                        "name": "canonical_artist",
                        "other_names": ["old_artist_name"],
                        "origin": "danbooru_artist_directory",
                        "works_url": "https://danbooru.donmai.us/posts?tags=canonical_artist",
                    }
                ],
            },
            keyword="old_artist_name",
            limit=20,
        )
        self.assertEqual([item["tag"] for item in addresses], ["canonical_artist"])
        self.assertEqual(addresses[0]["confidence"], "weak_evidence")
        self.assertEqual(
            addresses[0]["evidence_reasons"],
            ["danbooru_artist_directory_alias_match"],
        )

    def test_account_samples_require_a_real_author_identity(self):
        addresses = discovery_addresses(
            "twitter",
            {
                "candidates": [
                    {
                        "author": {"name": "first_artist"},
                        "thumbnail_url": "https://pbs.twimg.com/first.jpg",
                    },
                    {
                        "author": {"name": "second_artist"},
                        "thumbnail_url": "https://pbs.twimg.com/second.jpg",
                    },
                    {"author": {}, "thumbnail_url": "https://pbs.twimg.com/unknown.jpg"},
                ],
                "authors": [
                    {"name": "first_artist", "works_url": "https://x.com/First_Artist/media"},
                    {"name": "second_artist", "works_url": "https://x.com/Second_Artist/media"},
                ],
            },
            keyword="artist",
            limit=20,
        )
        self.assertEqual([item["matched_items"] for item in addresses], [1, 1])
        self.assertEqual([item["confidence"] for item in addresses], ["weak_evidence"] * 2)
        self.assertTrue(
            all("account_identity_unverified" in item["evidence_reasons"] for item in addresses)
        )
        self.assertEqual(len({item["id"] for item in addresses}), 2)
        self.assertTrue(all(item["id"].removeprefix("twitter:account:") for item in addresses))
        self.assertEqual(
            [item["sample_thumbnails"] for item in addresses],
            [["https://pbs.twimg.com/first.jpg"], ["https://pbs.twimg.com/second.jpg"]],
        )

    def test_danbooru_external_profile_normalization(self):
        twitter = classify_external_profile("https://x.com/artist_name")
        self.assertEqual(twitter["crawl_site"], "twitter")
        self.assertEqual(twitter["crawl_url"], "https://x.com/artist_name/media")
        numeric_twitter = classify_external_profile("https://x.com/i/user/123")
        self.assertIsNone(numeric_twitter["crawl_url"])
        pixiv = classify_external_profile("https://www.pixiv.net/users/77")
        self.assertEqual(pixiv["crawl_site"], "pixiv")
        self.assertEqual(pixiv["crawl_url"], "https://www.pixiv.net/users/77/artworks")
        self.assertEqual(
            canonical_gallery_address("x", "https://twitter.com/artist/"),
            "https://x.com/artist/media",
        )
        self.assertEqual(
            canonical_gallery_address("x", "https://X.com/Artist_Name/MEDIA"),
            "https://x.com/artist_name/media",
        )
        self.assertEqual(
            classify_external_profile("https://x.com/Artist_Name")["crawl_url"],
            "https://x.com/artist_name/media",
        )
        self.assertEqual(
            canonical_gallery_address("pixiv", "https://pixiv.net/users/77/?ref=x"),
            "https://www.pixiv.net/users/77/artworks",
        )

    def test_danbooru_artist_profile_reads_artist_urls_endpoint(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url.endswith("artists.json"):
                    return Response(
                        [
                            {
                                "id": 55,
                                "name": "artist_name",
                                "other_names": ["Artist Name"],
                                "group_name": "Circle",
                            }
                        ]
                    )
                return Response(
                    [
                        {
                            "id": 99,
                            "artist_id": 55,
                            "url": "https://x.com/artist_name",
                            "is_active": True,
                        }
                    ]
                )

        session = Session()
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(_FakeGallery("[]"), _FakeProxy(), Path(temporary))
            with patch("gdl_backend.discovery.requests.Session", return_value=session):
                profiles, errors = asyncio.run(
                    service.danbooru_artist_profiles(
                        ["artist_name"],
                        policy=SitePolicy(proxy_mode="direct", retry_limit=0),
                        proxy_mode="direct",
                    )
                )
        self.assertEqual(errors, [])
        self.assertEqual(profiles[0]["id"], "55")
        self.assertEqual(
            profiles[0]["related_profiles"][0]["crawl_url"],
            "https://x.com/artist_name/media",
        )
        self.assertTrue(session.calls[1][0].endswith("artist_urls.json"))

    def test_danbooru_artist_profile_rejects_non_exact_api_fallback(self):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return [
                    {
                        "id": 74349,
                        "name": "kajuu_aisu",
                        "other_names": ["rurudo"],
                    }
                ]

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if url.endswith("artist_urls.json"):
                    raise AssertionError("non-exact artist must not load artist_urls")
                return Response()

        session = Session()
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(_FakeGallery("[]"), _FakeProxy(), Path(temporary))
            with patch("gdl_backend.discovery.requests.Session", return_value=session):
                profiles, errors = asyncio.run(
                    service.danbooru_artist_profiles(
                        ["rurudo"],
                        policy=SitePolicy(proxy_mode="direct", retry_limit=0),
                        proxy_mode="direct",
                    )
                )

        self.assertEqual(profiles, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(session.calls), 1)

    def test_exhentai_gdata_enriches_search_titles_and_covers(self):
        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "gmetadata": [
                        {
                            "gid": 1531036,
                            "token": "91cbde3481",
                            "title": "Artist &amp; Character Collection",
                            "title_jpn": "画集",
                            "thumb": "https://ehgt.org/w/cover.webp",
                            "filecount": "42",
                            "category": "Non-H",
                            "uploader": "sample_uploader",
                            "rating": "4.50",
                            "tags": ["artist:ogipote", "character:sample"],
                        }
                    ]
                }

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        result = {
            "search_url": "https://e-hentai.org/?f_search=ogipote",
            "candidate_count": 1,
            "author_count": 0,
            "candidates": [
                {
                    "id": "1531036",
                    "site": "exhentai",
                    "kind": "gallery",
                    "title": "Gallery 1531036",
                    "url": "https://e-hentai.org/g/1531036/91cbde3481/",
                    "download_url": "https://e-hentai.org/g/1531036/91cbde3481/",
                    "thumbnail_url": None,
                    "media_count": None,
                    "author": None,
                    "metadata": {"gallery_token": "91cbde3481"},
                }
            ],
            "authors": [],
        }
        session = Session()
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(_FakeGallery("[]"), _FakeProxy(), Path(temporary))
            with patch("gdl_backend.discovery.requests.Session", return_value=session):
                enriched = asyncio.run(
                    service.enrich_exhentai_previews(
                        result,
                        policy=SitePolicy(
                            proxy_mode="direct",
                            retry_limit=0,
                            http_timeout=10,
                        ),
                        proxy_mode="direct",
                        timeout_seconds=30,
                    )
                )

        candidate = enriched["candidates"][0]
        self.assertEqual(candidate["title"], "Artist & Character Collection")
        self.assertEqual(candidate["thumbnail_url"], "https://ehgt.org/w/cover.webp")
        self.assertEqual(candidate["media_count"], 42)
        self.assertEqual(candidate["metadata"]["tags"], ["artist:ogipote", "character:sample"])
        self.assertEqual(enriched["preview_count"], 1)
        self.assertEqual(enriched["preview_missing_count"], 0)
        self.assertEqual(
            session.calls[0][1]["json"]["gidlist"],
            [[1531036, "91cbde3481"]],
        )

    def test_exhentai_gdata_retries_transient_server_error(self):
        class Response:
            def __init__(self, status_code):
                self.status_code = status_code

            def raise_for_status(self):
                if self.status_code >= 400:
                    response = requests.Response()
                    response.status_code = self.status_code
                    raise requests.HTTPError(
                        f"{self.status_code} Server Error",
                        response=response,
                    )

            def json(self):
                return {
                    "gmetadata": [
                        {
                            "gid": 1531036,
                            "token": "91cbde3481",
                            "title": "Retried gallery",
                            "thumb": "https://ehgt.org/w/retried.webp",
                            "filecount": "1",
                            "tags": [],
                        }
                    ]
                }

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = 0

            def post(self, _url, **_kwargs):
                self.calls += 1
                return Response(500 if self.calls == 1 else 200)

        result = {
            "search_url": "https://e-hentai.org/?f_search=ogipote",
            "candidate_count": 1,
            "author_count": 0,
            "candidates": [
                {
                    "id": "1531036",
                    "site": "exhentai",
                    "kind": "gallery",
                    "title": "Gallery 1531036",
                    "url": "https://e-hentai.org/g/1531036/91cbde3481/",
                    "download_url": "https://e-hentai.org/g/1531036/91cbde3481/",
                    "metadata": {"gallery_token": "91cbde3481"},
                }
            ],
            "authors": [],
        }
        session = Session()
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(_FakeGallery("[]"), _FakeProxy(), Path(temporary))
            with patch("gdl_backend.discovery.requests.Session", return_value=session):
                enriched = asyncio.run(
                    service.enrich_exhentai_previews(
                        result,
                        policy=SitePolicy(
                            proxy_mode="direct",
                            retry_limit=1,
                            backoff_base_seconds=0,
                        ),
                        proxy_mode="direct",
                        timeout_seconds=30,
                    )
                )

        self.assertEqual(session.calls, 2)
        self.assertEqual(enriched["preview_count"], 1)
        self.assertEqual(enriched["candidates"][0]["title"], "Retried gallery")

    def test_protocol_errors_and_managed_args(self):
        with self.assertRaises(DiscoveryError):
            parse_discovery_output(
                "pixiv",
                json.dumps([[-1, {"error": "Auth", "message": "token expired"}]]),
                source_url="https://www.pixiv.net/en/tags/a/artworks",
                limit=20,
            )
        with self.assertRaises(ValueError):
            validate_discovery_args(["--dump-json"])
        self.assertEqual(validate_discovery_args(["--filter", "rating == 'g'"]), ["--filter", "rating == 'g'"])

    def test_search_uses_proxy_pool_lease(self):
        stdout = json.dumps(
            [
                [
                    2,
                    {
                        "id": 123,
                        "tags_artist": ["artist"],
                        "tags_copyright": ["tag"],
                    },
                ]
            ]
        )
        gallery = _FakeGallery(stdout)
        proxy = _FakeProxy()
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(gallery, proxy, Path(temporary))
            result = asyncio.run(
                service.search(
                    site="danbooru",
                    keyword="tag",
                    limit=1,
                    policy=SitePolicy(
                        proxy_mode="required",
                        retry_limit=0,
                        node_tags=["jp"],
                    ),
                    proxy_mode="required",
                    credentials_ref=None,
                    cookies_file=None,
                    config_file=None,
                    extra_args=[],
                    timeout_seconds=30,
                )
            )
        self.assertTrue(result["proxy"]["used"])
        self.assertEqual(result["proxy"]["node_id"], "node-1")
        self.assertEqual(gallery.calls[0][1]["proxy_url"], "http://127.0.0.1:29001")
        self.assertEqual(proxy.acquired[0][1]["node_tags"], ["jp"])
        self.assertFalse(proxy.released[0][1]["proxy_fault"])

    def test_pixiv_discovery_emits_only_the_first_media_per_work(self):
        stdout = json.dumps(
            [
                [
                    2,
                    {
                        "id": 123,
                        "title": "multi-page",
                        "count": 53,
                        "user": {"id": 77, "account": "artist", "name": "Artist"},
                    },
                ],
                [3, "https://i.pximg.net/example_p0.png", {"id": 123, "num": 0}],
            ]
        )
        gallery = _FakeGallery(stdout)
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(gallery, _FakeProxy(), Path(temporary))
            result = asyncio.run(
                service.discover_url(
                    site="pixiv",
                    url="https://www.pixiv.net/users/77/artworks",
                    keyword=None,
                    limit=5,
                    range_kind=None,
                    policy=SitePolicy(proxy_mode="direct", retry_limit=0),
                    proxy_mode="direct",
                    credentials_ref=None,
                    cookies_file=None,
                    config_file=None,
                    extra_args=["--filter", "rating == 'g'"],
                    timeout_seconds=30,
                )
            )
        self.assertEqual(result["candidates"][0]["media_count"], 53)
        self.assertEqual(
            gallery.calls[0][1]["extra_args"],
            [
                "--dump-json",
                "--post-range",
                "1-5",
                "--filter",
                "(rating == 'g') and (num == 0)",
            ],
        )

    def test_cloudflare_parse_error_retries_and_reports_attempts(self):
        gallery = _FakeGallery(
            json.dumps(
                [
                    [
                        -1,
                        {
                            "error": "HttpError",
                            "message": "Cloudflare challenge (403 Forbidden) for https://x.com/account/access",
                        },
                    ]
                ]
            )
        )
        proxy = _FakeProxy()
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(gallery, proxy, Path(temporary))
            with self.assertRaises(DiscoveryError) as caught:
                asyncio.run(
                    service.search(
                        site="twitter",
                        keyword="rurudo",
                        limit=20,
                        policy=SitePolicy(
                            proxy_mode="required",
                            retry_limit=1,
                            backoff_base_seconds=0,
                        ),
                        proxy_mode="required",
                        credentials_ref=None,
                        cookies_file=None,
                        config_file=None,
                        extra_args=[],
                        timeout_seconds=30,
                    )
                )

        self.assertEqual(caught.exception.code, "discovery_failed")
        self.assertEqual(caught.exception.details["attempts"], 2)
        self.assertEqual(len(gallery.calls), 2)
        self.assertTrue(all(item[1]["proxy_fault"] for item in proxy.released))

    def test_empty_danbooru_gallery_result_falls_back_to_api_transport(self):
        gallery = _FakeGallery("[]")
        api_posts = [
            {
                "id": 123,
                "created_at": "2026-07-01T00:00:00Z",
                "rating": "s",
                "score": 5,
                "image_width": 1000,
                "image_height": 1200,
                "source": "https://example.test/preview.png",
                "tag_string_artist": "rurudo",
                "tag_string_character": "sample_character",
                "tag_string_copyright": "original",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(gallery, _FakeProxy(), Path(temporary))
            with patch(
                "gdl_backend.discovery._danbooru_json_request",
                return_value=api_posts,
            ) as request:
                result = asyncio.run(
                    service.search(
                        site="danbooru",
                        keyword="rurudo",
                        limit=20,
                        policy=SitePolicy(proxy_mode="direct", retry_limit=0),
                        proxy_mode="direct",
                        credentials_ref=None,
                        cookies_file=None,
                        config_file=None,
                        extra_args=[],
                        timeout_seconds=30,
                    )
                )

        self.assertEqual(result["transport"], "danbooru_api_browser_fingerprint")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["authors"][0]["name"], "rurudo")
        self.assertEqual(
            result["candidates"][0]["metadata"]["characters"],
            ["sample_character"],
        )
        self.assertEqual(request.call_args.kwargs["params"]["tags"], "rurudo")

    def test_empty_danbooru_artist_directory_falls_back_without_outer_wildcards(self):
        gallery = _FakeGallery("[]")
        api_artists = [
            {
                "id": 153992,
                "name": "rurudo",
                "other_names": ["kajuu_aisu"],
                "group_name": "",
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(gallery, _FakeProxy(), Path(temporary))
            with patch(
                "gdl_backend.discovery._danbooru_json_request",
                return_value=api_artists,
            ) as request:
                result = asyncio.run(
                    service.search_danbooru_artists(
                        keyword="rurudo",
                        limit=20,
                        policy=SitePolicy(proxy_mode="direct", retry_limit=0),
                        proxy_mode="direct",
                        credentials_ref=None,
                        cookies_file=None,
                        config_file=None,
                        timeout_seconds=30,
                    )
                )

        self.assertEqual(result["authors"][0]["name"], "rurudo")
        self.assertEqual(result["authors"][0]["other_names"], ["kajuu_aisu"])
        self.assertEqual(
            request.call_args.kwargs["params"]["search[any_name_matches]"],
            "rurudo",
        )

    def test_danbooru_artist_directory_search_uses_child_range(self):
        stdout = json.dumps(
            [
                [
                    6,
                    "https://danbooru.donmai.us/posts?tags=eijunesound",
                    {"id": 559075, "name": "eijunesound", "other_names": ["EijuneSound"]},
                ]
            ]
        )
        gallery = _FakeGallery(stdout)
        with tempfile.TemporaryDirectory() as temporary:
            service = DiscoveryService(gallery, _FakeProxy(), Path(temporary))
            result = asyncio.run(
                service.search_danbooru_artists(
                    keyword="eijune sound",
                    limit=3,
                    policy=SitePolicy(proxy_mode="direct", retry_limit=0),
                    proxy_mode="direct",
                    credentials_ref=None,
                    cookies_file=None,
                    config_file=None,
                    timeout_seconds=30,
                )
            )
        self.assertEqual(result["authors"][0]["name"], "eijunesound")
        call = gallery.calls[0][1]
        self.assertIn("search%5Bany_name_matches%5D=eijune%2Asound", call["url"])
        self.assertEqual(call["extra_args"][:3], ["--dump-json", "--child-range", "1-3"])

    def test_missing_credentials_reference_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                GalleryRunner._credentials("missing-profile")

    def test_capture_stops_at_streaming_output_limit(self):
        class HangingPipeProcess(_FakeProcess):
            def __init__(self):
                super().__init__(b"x" * 1024)
                self.stderr = asyncio.StreamReader()

        async def create_process(*args, **kwargs):
            return HangingPipeProcess()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            runner = GalleryRunner(settings.gallery, settings.project_dir)

            async def execute():
                with patch(
                    "gdl_backend.gallery.asyncio.create_subprocess_exec",
                    new=create_process,
                ):
                    await asyncio.wait_for(
                        runner.capture(
                            "limited",
                            url="https://danbooru.donmai.us/posts/1",
                            output_dir=str(root / "capture"),
                            proxy_url=None,
                            http_timeout=10,
                            gallery_retries=0,
                            task_timeout=30,
                            cookies_file=None,
                            config_file=None,
                            credentials_ref=None,
                            extra_args=["--dump-json"],
                            max_output_bytes=100,
                        ),
                        timeout=1,
                    )

            with self.assertRaises(ValueError):
                asyncio.run(execute())


if __name__ == "__main__":
    unittest.main()
