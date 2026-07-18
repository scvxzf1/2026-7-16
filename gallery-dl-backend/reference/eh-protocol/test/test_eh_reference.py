#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

import importlib.util
import os
import sys
import tempfile
import unittest
import zipfile

from pathlib import Path


SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "eh_reference.py",
)
SPEC = importlib.util.spec_from_file_location("eh_reference_test_target",
                                              SCRIPT_PATH)
ehr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ehr
SPEC.loader.exec_module(ehr)


class FakeResponse:

    def __init__(self, text="", data=None, url="https://example.test/",
                 content=None, headers=None):
        if content is None:
            content = text.encode()
        self.content = content
        self.text = text or content.decode(errors="replace")
        self.data = data
        self.url = url
        self.status_code = 200
        self.headers = {"Cache-Control": "private"}
        if headers:
            self.headers.update(headers)
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        pass

    def json(self):
        return self.data

    def iter_content(self, chunk_size=1):
        for position in range(0, len(self.content), chunk_size):
            yield self.content[position:position + chunk_size]


class FakeSession:

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.cookies = ehr.requests.cookies.RequestsCookieJar()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class EHReferenceParserTest(unittest.TestCase):

    def test_artist_search_term(self):
        self.assertEqual(ehr.artist_search_term("foo"), "artist:foo$")
        self.assertEqual(
            ehr.artist_search_term("foo  bar"),
            'artist:"foo bar$"',
        )

    def test_gallery_site_mapping(self):
        self.assertEqual(
            ehr.site_from_gallery_url(
                "https://g.e-hentai.org/g/123/abcdef0123/"
            ),
            "eh",
        )
        self.assertEqual(
            ehr.site_from_gallery_url(
                "https://exhentai.org/mpv/123/abcdef0123/#page2"
            ),
            "exh",
        )

    def test_parse_standard_page(self):
        page = """
            <div id="i3"><a onclick="return load_image('NEXT')">
            <img id="img" src="https://ehgt.org/a/one.jpg">
            <a onclick="return nl('NLKEY')">reload</a>
            <a href="https://e-hentai.org/fullimg.php?gid=1&amp;page=1">
            Download original</a>
            <script>var startkey="START"; var showkey="SHOW";</script>
        """
        image = ehr._parse_standard_page(page, "https://e-hentai.org")
        self.assertEqual(image.next_key, "NEXT")
        self.assertEqual(image.start_key, "START")
        self.assertEqual(image.show_key, "SHOW")
        self.assertEqual(image.nl, "NLKEY")
        self.assertEqual(image.resample_url, "https://ehgt.org/a/one.jpg")
        self.assertEqual(
            image.original_url,
            "https://e-hentai.org/fullimg.php?gid=1&page=1",
        )

    def test_parse_showpage(self):
        data = {
            "i3": (
                "<a onclick=\"return load_image('NEXT2')\">"
                "<img id=\"img\" src=\"https://ehgt.org/a/two.jpg\">"
                "<a onclick=\"return nl('NL2')\">reload</a>"
            ),
            "i6": (
                '<a href="https://exhentai.org/fullimg.php?gid=2&amp;p=2">'
                "Download original</a>"
            ),
        }
        image = ehr._parse_showpage(data, "https://exhentai.org")
        self.assertEqual(image.next_key, "NEXT2")
        self.assertEqual(image.nl, "NL2")
        self.assertEqual(image.resample_url, "https://ehgt.org/a/two.jpg")
        self.assertEqual(
            image.original_url,
            "https://exhentai.org/fullimg.php?gid=2&p=2",
        )

    def test_parse_mpv_page(self):
        page = """
            <script>
            var imagelist = [{"k":"A","name":"one.jpg"}];
            var mpvkey = "MPVKEY";
            </script>
        """
        images, key = ehr._parse_mpv_page(page)
        self.assertEqual(key, "MPVKEY")
        self.assertEqual(images, [{"k": "A", "name": "one.jpg"}])


class EHReferenceSearchTest(unittest.TestCase):

    def test_search_artist_pagination_and_deduplication(self):
        first = FakeResponse("""
            <a href="https://e-hentai.org/g/10/0123456789/">one</a>
            <a href="https://e-hentai.org/g/10/0123456789/">one</a>
            <script>nexturl="https://e-hentai.org/?next=TOKEN";</script>
        """)
        second = FakeResponse("""
            <a href="https://e-hentai.org/g/20/abcdef0123/">two</a>
            <div class="ptdd">&gt;</div>
        """)
        session = FakeSession((first, second))
        client = ehr.EHClient("eh", session=session, interval=0)

        galleries = list(client.search_artist("foo bar"))

        self.assertEqual([item.gid for item in galleries], [10, 20])
        self.assertEqual(
            session.calls[0][2]["params"]["f_search"],
            'artist:"foo bar$"',
        )
        self.assertIsNone(session.calls[1][2]["params"])


class EHReferenceGalleryTest(unittest.TestCase):

    GALLERY_PAGE = """
        <h1 id="gn">Example Gallery</h1>
        <table><tr><td>Length:</td><td class="gdt2">2 pages</td></tr></table>
        <a href="https://e-hentai.org/s/0123456789/123-1">first</a>
    """
    IMAGE_PAGE = """
        <div id="i3"><a onclick="return load_image('NEXT')">
        <img id="img" src="https://ehgt.org/a/one.jpg">
        <a onclick="return nl('NL1')">reload</a>
        <a href="https://e-hentai.org/fullimg.php?gid=123&amp;p=1">
        Download original</a>
        <script>var startkey="START"; var showkey="SHOW";</script>
    """

    def test_standard_gallery_original_enumeration(self):
        showpage = {
            "i3": (
                "<a onclick=\"return load_image('END')\">"
                "<img id=\"img\" src=\"https://ehgt.org/a/two.jpg\">"
                "<a onclick=\"return nl('NL2')\">reload</a>"
            ),
            "i6": (
                '<a href="https://e-hentai.org/fullimg.php?gid=123&amp;p=2">'
                "Download original</a>"
            ),
        }
        session = FakeSession((
            FakeResponse(self.GALLERY_PAGE),
            FakeResponse(self.IMAGE_PAGE),
            FakeResponse(data=showpage),
        ))
        client = ehr.EHClient("eh", session=session, interval=0)
        gallery = client.open_gallery(
            "https://e-hentai.org/g/123/abcdef0123/"
        )

        images = list(client.iter_gallery_images(gallery, "original"))

        self.assertEqual(gallery.filecount, 2)
        self.assertEqual([image.num for image in images], [1, 2])
        self.assertTrue(all(image.is_original for image in images))
        self.assertEqual(images[0].image_token, "START")
        self.assertEqual(images[1].image_token, "NEXT")
        payload = session.calls[2][2]["json"]
        self.assertEqual(payload["method"], "showpage")
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["imgkey"], "NEXT")
        self.assertEqual(payload["showkey"], "SHOW")

    def test_standard_gallery_start_page(self):
        showpage = {
            "i3": (
                "<a onclick=\"return load_image('END')\">"
                "<img id=\"img\" src=\"https://ehgt.org/a/two.jpg\">"
                "<a onclick=\"return nl('NL2')\">reload</a>"
            ),
            "i6": "",
        }
        session = FakeSession((
            FakeResponse(self.GALLERY_PAGE),
            FakeResponse(self.IMAGE_PAGE),
            FakeResponse(data=showpage),
        ))
        client = ehr.EHClient("eh", session=session, interval=0)
        gallery = client.open_gallery(
            "https://e-hentai.org/g/123/abcdef0123/#page2"
        )

        images = list(client.iter_gallery_images(gallery, "resample"))

        self.assertEqual(gallery.start_page, 2)
        self.assertEqual([image.num for image in images], [2])

    def test_mpv_resample_and_original_urls(self):
        mpv_page = """
            var imagelist = [{"k":"KEY","name":"named.jpg"}];
            var mpvkey = "MPV";
        """
        info = {
            "i": "https://ehgt.org/a/resampled.jpg",
            "o": "x y 100 x 200 1 MB z",
            "lf": "fullimg.php?gid=123&p=1",
            "s": "NL",
        }
        session = FakeSession((
            FakeResponse(mpv_page),
            FakeResponse(data=info),
        ))
        client = ehr.EHClient("exh", session=session, interval=0)
        gallery = ehr.Gallery(
            site="exh",
            root=client.root,
            gid=123,
            token="abcdef0123",
            url=client.root + "/g/123/abcdef0123/",
            title="Example",
            filecount=1,
            api_url=client.api_url,
            mpv=True,
        )

        image = next(client.iter_gallery_images(gallery, "original"))

        self.assertEqual(image.filename, "named.jpg")
        self.assertEqual(
            image.original_url,
            "https://exhentai.org/fullimg.php?gid=123&p=1",
        )
        self.assertEqual(image.resample_url, info["i"])
        payload = session.calls[1][2]["json"]
        self.assertEqual(payload["method"], "imagedispatch")
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["imgkey"], "KEY")


class LocalZipClient(ehr.EHClient):

    def __init__(self):
        super().__init__("eh", interval=0)
        self.gallery = ehr.Gallery(
            site="eh",
            root=self.root,
            gid=123,
            token="abcdef0123",
            url=self.root + "/g/123/abcdef0123/",
            title="Local Test",
            filecount=2,
            api_url=self.api_url,
            mpv=False,
        )

    def open_gallery(self, url):
        return self.gallery

    def iter_gallery_images(self, gallery, mode="resample"):
        if mode != "original":
            raise AssertionError(mode)
        for num in (1, 2):
            yield ehr.Image(
                gid=gallery.gid,
                num=num,
                image_token="TOKEN{}".format(num),
                url="https://example.test/{}.jpg".format(num),
                resample_url="",
                original_url="https://example.test/{}.jpg".format(num),
                filename="{}.jpg".format(num),
                mode="original",
                viewer="standard",
            )

    def download_image(self, image, directory, *, overwrite=False):
        path = Path(directory) / "{:04d}_{}".format(
            image.num,
            image.filename,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes("image{}".format(image.num).encode())
        return path


class EHReferenceZipTest(unittest.TestCase):

    def test_originals_are_packed_after_download(self):
        client = LocalZipClient()
        with tempfile.TemporaryDirectory() as directory:
            archive = client.download_original_zip(
                client.gallery.url,
                directory,
            )
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as file:
                self.assertEqual(
                    file.namelist(),
                    ["0001_1.jpg", "0002_2.jpg"],
                )
                self.assertEqual(file.read("0002_2.jpg"), b"image2")
            temporary = list(Path(directory).glob(".eh-*-"))
            self.assertFalse(temporary)


class EHReferenceDownloadTest(unittest.TestCase):

    @staticmethod
    def image(url="https://e-hentai.org/fullimg.php?gid=1"):
        return ehr.Image(
            gid=1,
            num=1,
            image_token="TOKEN",
            url=url,
            resample_url="https://ehgt.org/a/one.jpg",
            original_url=url,
            filename="fullimg.php",
            mode="original",
            viewer="standard",
        )

    def test_content_disposition_controls_download_name(self):
        response = FakeResponse(
            content=b"\xff\xd8image",
            headers={
                "Content-Type": "image/jpeg",
                "Content-Disposition": 'attachment; filename="original.jpg"',
            },
        )
        client = ehr.EHClient(
            "eh",
            session=FakeSession((response,)),
            interval=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = client.download_image(self.image(), directory)
            self.assertEqual(path.name, "0001_original.jpg")
            self.assertEqual(path.read_bytes(), b"\xff\xd8image")

    def test_gp_html_is_reported(self):
        response = FakeResponse(
            text="This original image requires GP",
            headers={"Content-Type": "text/html; charset=UTF-8"},
        )
        client = ehr.EHClient(
            "eh",
            session=FakeSession((response,)),
            interval=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ehr.GPRequiredError):
                client.download_image(self.image(), directory)

    def test_limit_url_with_query_is_reported(self):
        with self.assertRaises(ehr.ImageLimitError):
            ehr.EHClient._check_limit_url(
                "https://ehgt.org/g/509.gif?nl=TOKEN"
            )


if __name__ == "__main__":
    unittest.main()
