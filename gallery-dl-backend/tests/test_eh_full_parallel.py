from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.eh_full_gallery_parallel import (
    _extract_gallery_queue,
    _inventory_images,
    _parse_gallery_page,
)


class EhFullParallelTests(unittest.TestCase):
    def test_extracts_first_gallery_queue_from_dump_json(self):
        data = [[6, "https://e-hentai.org/g/1531036/91cbde3481/", {"gallery_id": 1531036}]]
        self.assertEqual(
            _extract_gallery_queue(json.loads(json.dumps(data))),
            ("https://e-hentai.org/g/1531036/91cbde3481/", 1531036),
        )

    def test_parses_gallery_count_title_and_image_pages(self):
        page = """
        <h1 id="gn">[ALcot]Clover Day&#039;s ARTWORK</h1>
        <td>Length:</td><td class="gdt2">145 pages</td>
        <a href="https://e-hentai.org/s/a91083c133/1531036-1">one</a>
        <a href="https://e-hentai.org/s/3340fa59ab/1531036-2">two</a>
        """
        title, count, links = _parse_gallery_page(
            page,
            "https://e-hentai.org/g/1531036/91cbde3481/",
            1531036,
        )
        self.assertEqual(title, "[ALcot]Clover Day's ARTWORK")
        self.assertEqual(count, 145)
        self.assertEqual(sorted(links), [1, 2])

    def test_inventory_uses_gid_and_page_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "1531036_0001_token_a.webp.webp").write_bytes(b"one")
            (root / "1531036_0002_token_b.webp.jpg").write_bytes(b"two")
            (root / "other.jpg").write_bytes(b"other")
            rows, pages = _inventory_images(root, 1531036)
            self.assertEqual(pages, [1, 2])
            self.assertEqual([row["page"] for row in rows], [1, 2])


if __name__ == "__main__":
    unittest.main()
