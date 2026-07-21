from __future__ import annotations

import unittest

from gdl_backend.source_keys import candidate_source_key, source_key_from_url


class SourceKeyTests(unittest.TestCase):
    def test_pixiv_work_urls_share_one_stable_key(self):
        urls = [
            "https://www.pixiv.net/artworks/12345",
            "https://www.pixiv.net/en/artworks/12345?foo=bar",
            "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=12345",
            "https://pixiv.net/i/12345",
            "https://i.pximg.net/img-original/img/2026/01/02/03/04/05/12345_p0.png",
            "https://i.pximg.net/img-original/img/2026/01/02/03/04/05/12345.jpg",
            "https://i.pximg.net/img-zip-ugoira/img/2026/01/02/03/04/05/12345_ugoira1920x1080.zip",
            "https://img18.pixiv.net/img/artist/12345.jpg",
        ]
        self.assertEqual(
            {source_key_from_url(url) for url in urls},
            {"pixiv:12345"},
        )

    def test_twitter_variants_share_one_stable_key(self):
        urls = [
            "https://x.com/artist/status/987654321/photo/1",
            "https://twitter.com/artist/status/987654321?ref_src=twsrc",
            "https://mobile.twitter.com/artist/statuses/987654321",
            "https://fxtwitter.com/artist/status/987654321/video/1",
            "x.com/i/web/status/987654321",
        ]
        self.assertEqual(
            {source_key_from_url(url) for url in urls},
            {"twitter:987654321"},
        )

    def test_candidate_identity_precedes_url_and_unrelated_urls_are_ignored(self):
        self.assertEqual(
            candidate_source_key("x", "42", "https://pbs.twimg.com/media/sample.jpg"),
            "twitter:42",
        )
        self.assertEqual(candidate_source_key("pixiv", "77", ""), "pixiv:77")
        self.assertIsNone(source_key_from_url("https://i.pximg.net/user-profile/img/123.jpg"))
        self.assertIsNone(source_key_from_url("https://example.com/status/123"))


if __name__ == "__main__":
    unittest.main()
