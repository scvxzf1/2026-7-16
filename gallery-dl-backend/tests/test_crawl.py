from __future__ import annotations

import asyncio
import unittest

from gdl_backend.crawl import CrawlPlanError, CrawlPlanner, _parse_eh_index
from gdl_backend.schemas import SitePolicy


class CrawlPlannerTests(unittest.TestCase):
    def test_media_planning_is_the_only_address_execution_mode(self):
        planner = CrawlPlanner(object())  # Proxy is only used by EH gallery planning.
        policy = SitePolicy(proxy_mode="direct")
        items = [
            {
                "id": "pixiv-1",
                "site": "pixiv",
                "kind": "work",
                "url": "https://www.pixiv.net/artworks/1",
                "media_count": 3,
            },
            {
                "id": "danbooru-2",
                "site": "danbooru",
                "kind": "post",
                "url": "https://danbooru.donmai.us/posts/2",
                "source_url": "https://twitter.com/artist/status/22",
                "media_count": 1,
            },
        ]
        units, proxies = asyncio.run(
            planner.plan_media(
                items,
                policy=policy,
                proxy_mode="direct",
                cookies_file=None,
                max_tasks=10,
            )
        )
        self.assertEqual(len(units), 4)
        self.assertEqual(units[0].extra_args, ["--range", "1"])
        self.assertEqual(units[2].extra_args, ["--range", "3"])
        self.assertEqual(units[3].extra_args, [])
        self.assertEqual([unit.source_key for unit in units[:3]], ["pixiv:1"] * 3)
        self.assertEqual(units[3].source_key, "twitter:22")
        self.assertEqual(units[3].source_url, "https://twitter.com/artist/status/22")
        self.assertEqual(proxies, [])

        with self.assertRaises(CrawlPlanError):
            asyncio.run(
                planner.plan_media(
                    items,
                    policy=policy,
                    proxy_mode="direct",
                    cookies_file=None,
                    max_tasks=2,
                )
            )

    def test_twitter_media_urls_bypass_status_page_ranges(self):
        planner = CrawlPlanner(object())
        media_urls = [
            "https://pbs.twimg.com/media/sample?format=jpg&name=orig",
            "https://video.twimg.com/ext_tw_video/123/pu/vid/1280x720/sample.mp4?tag=12",
            "https://pbs.twimg.com/media/unexpected?format=jpg&name=orig",
        ]
        units, _proxies = asyncio.run(
            planner.plan_media(
                [
                    {
                        "id": "123",
                        "site": "twitter",
                        "kind": "work",
                        "download_url": "https://x.com/example/status/123",
                        "media_count": 2,
                        "media_urls": media_urls,
                        "extra_args": ["--sleep", "0"],
                    }
                ],
                policy=SitePolicy(proxy_mode="direct"),
                proxy_mode="direct",
                cookies_file=None,
                max_tasks=10,
            )
        )
        self.assertEqual([unit.url for unit in units], media_urls[:2])
        self.assertEqual([unit.source_id for unit in units], ["123:1", "123:2"])
        self.assertEqual([unit.extra_args for unit in units], [["--sleep", "0"]] * 2)
        self.assertTrue(all("--range" not in unit.extra_args for unit in units))

        fallback, _proxies = asyncio.run(
            planner.plan_media(
                [
                    {
                        "id": "partial",
                        "site": "twitter",
                        "url": "https://x.com/example/status/456",
                        "media_count": 2,
                        "media_urls": media_urls[:1],
                    }
                ],
                policy=SitePolicy(proxy_mode="direct"),
                proxy_mode="direct",
                cookies_file=None,
                max_tasks=10,
            )
        )
        self.assertEqual(
            [unit.extra_args for unit in fallback],
            [["--range", "1"], ["--range", "2"]],
        )

    def test_eh_index_parser(self):
        page = """
        <h1 id="gn">A &amp; B</h1>
        <table><tr><td>Length:</td><td class="gdt2">2 pages</td></tr></table>
        <a href="https://e-hentai.org/s/aaaaaaaaaa/123-1">1</a>
        <a href="https://e-hentai.org/s/bbbbbbbbbb/123-2">2</a>
        """
        title, total, links = _parse_eh_index(
            page,
            "https://e-hentai.org/g/123/cccccccccc/",
            123,
        )
        self.assertEqual(title, "A & B")
        self.assertEqual(total, 2)
        self.assertEqual(sorted(links), [1, 2])


if __name__ == "__main__":
    unittest.main()
