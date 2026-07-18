#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Small, integration-oriented E-Hentai/ExHentai reference client.

This module extracts the relevant protocol flow from
``gallery_dl.extractor.exhentai`` and only depends on ``requests``.  See
``docs/eh-reference/README.md`` for integration and rate-limit notes.
It intentionally exposes small methods that can be copied into another
application:

``search_artist()``
    Discover gallery IDs and tokens with an exact ``artist:`` search.

``open_gallery()`` and ``iter_gallery_images()``
    Resolve a gallery and enumerate either resampled images or per-image
    originals.  Normal and MPV viewers are selected internally.

``download_resampled()``
    Save the images shown by the normal viewer to a directory.

``download_original_zip()``
    Download every per-image original into a temporary directory and create
    a local ZIP only after all images have completed.

This is not an Archive Download client.  The ``original`` path follows each
viewer's ``fullimg``/``lf`` URL and may therefore require GP.  A Netscape
cookies.txt export is the most reliable way to provide an authenticated
session, especially for ExHentai.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import random
import re
import sys
import tempfile
import time
import zipfile

from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional
from typing import Sequence, Tuple
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
from urllib.parse import urlunsplit

import requests


__all__ = (
    "EHClient",
    "GalleryLink",
    "Gallery",
    "Image",
    "EHError",
    "AuthenticationError",
    "ParseError",
    "GPRequiredError",
    "ImageLimitError",
    "RequestError",
    "artist_search_term",
    "site_from_gallery_url",
)

ROOTS = {
    "eh": "https://e-hentai.org",
    "exh": "https://exhentai.org",
}

GALLERY_URL_RE = re.compile(
    r"^(?:https?://)?"
    r"(?P<host>e-hentai\.org|exhentai\.org|g\.e-hentai\.org)"
    r"/(?P<viewer>g|mpv)/(?P<gid>\d+)/"
    r"(?P<token>[0-9a-f]{10})/?"
    r"(?:#page(?P<page>\d+))?",
    re.IGNORECASE,
)

GALLERY_RESULT_RE = re.compile(
    r"https?://(?:e-hentai\.org|exhentai\.org|g\.e-hentai\.org)"
    r"/g/(?P<gid>\d+)/(?P<token>[0-9a-f]{10})",
    re.IGNORECASE,
)

IMAGE_PAGE_RE = re.compile(
    r"https?://(?:e-hentai\.org|exhentai\.org|g\.e-hentai\.org)"
    r"/s/(?P<token>[0-9a-f]{10})/(?P<gid>\d+)-(?P<num>\d+)",
    re.IGNORECASE,
)

FULLIMG_RE = re.compile(
    r"https?://(?:e-hentai\.org|exhentai\.org)/fullimg"
    r"[^\"'<>{}\s]*",
    re.IGNORECASE,
)

RELATIVE_FULLIMG_RE = re.compile(
    r"(?:\"|')(?P<url>/fullimg[^\"']+)(?:\"|')",
    re.IGNORECASE,
)

INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *("COM{}".format(index) for index in range(1, 10)),
    *("LPT{}".format(index) for index in range(1, 10)),
}


class EHError(Exception):
    """Base exception for this reference client."""


class AuthenticationError(EHError):
    """The selected site did not return an authenticated page."""


class ParseError(EHError):
    """A required value was missing from an EH page or API response."""


class GPRequiredError(EHError):
    """EH returned its GP confirmation/payment page for an original."""


class ImageLimitError(EHError):
    """EH returned its image-limit placeholder."""


class RequestError(EHError):
    """A request failed after the configured retries."""


@dataclass
class GalleryLink:
    """A gallery discovered by search."""

    site: str
    gid: int
    token: str
    url: str


@dataclass
class Gallery:
    """Resolved gallery state used by :meth:`iter_gallery_images`."""

    site: str
    root: str
    gid: int
    token: str
    url: str
    title: str
    filecount: int
    api_url: str
    mpv: bool
    start_page: int = 1
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)
    page: str = field(default="", repr=False)


@dataclass
class Image:
    """A single selected image URL plus data needed to refresh it."""

    gid: int
    num: int
    image_token: str
    url: str
    resample_url: str
    original_url: Optional[str]
    filename: str
    mode: str
    viewer: str
    nl: str = ""
    api_url: str = ""
    refresh_payload: Optional[Dict[str, object]] = field(
        default=None,
        repr=False,
    )

    @property
    def is_original(self) -> bool:
        return self.mode == "original"


@dataclass
class _StandardPage:
    next_key: str
    start_key: str
    show_key: str
    resample_url: str
    original_url: Optional[str]
    nl: str


@dataclass
class _ShowPage:
    next_key: str
    resample_url: str
    original_url: Optional[str]
    nl: str


def artist_search_term(name: str) -> str:
    """Return an exact EH ``artist:`` namespace query."""
    name = " ".join(name.strip().split()).replace('"', "")
    if not name:
        raise ValueError("artist name is empty")
    if " " in name:
        return 'artist:"{}$"'.format(name)
    return "artist:{}$".format(name)


def site_from_host(host: str) -> str:
    """Map an EH hostname to this module's short site name."""
    return "exh" if host.lower() == "exhentai.org" else "eh"


def site_from_gallery_url(url: str) -> str:
    """Return ``eh`` or ``exh`` for a supported gallery URL."""
    match = GALLERY_URL_RE.match(url)
    if not match:
        raise ValueError("unsupported gallery URL: {}".format(url))
    return site_from_host(match.group("host"))


def _find(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1) if match else default


def _required(pattern: str, text: str, label: str) -> str:
    value = _find(pattern, text)
    if not value:
        raise ParseError("failed to extract {}".format(label))
    return value


def _fullimg_url(text: str, root: str) -> Optional[str]:
    match = FULLIMG_RE.search(text)
    if match:
        return html.unescape(match.group(0))
    match = RELATIVE_FULLIMG_RE.search(text)
    if match:
        return urljoin(root + "/", html.unescape(match.group("url")))
    return None


def _parse_standard_page(page: str, root: str) -> _StandardPage:
    marker = '<div id="i3"'
    position = page.find(marker)
    if position < 0:
        raise ParseError("failed to locate image viewer block")
    block = page[position:]

    next_key = _find(r"load_image\(\s*['\"]([^'\"]*)", block)
    if not next_key:
        next_key = _find(r"'([^']+)'", block)
    image_tag = _required(
        r'(<img\b[^>]*\bid=["\']img["\'][^>]*>)',
        block,
        "image element",
    )
    resample = _required(
        r'\bsrc=["\']([^"\']+)',
        image_tag,
        "resampled image URL",
    )
    nl = _find(r"\bnl\(([^)]*)\)", block).strip("\"'")
    start_key = _required(
        r'var\s+startkey\s*=\s*["\']([^"\']*)',
        block,
        "startkey",
    )
    show_key = _required(
        r'var\s+showkey\s*=\s*["\']([^"\']*)',
        block,
        "showkey",
    )
    return _StandardPage(
        next_key=next_key,
        start_key=start_key,
        show_key=show_key,
        resample_url=html.unescape(resample),
        original_url=_fullimg_url(block, root),
        nl=nl,
    )


def _parse_showpage(data: Mapping[str, object], root: str) -> _ShowPage:
    i3 = str(data.get("i3") or "")
    i6 = str(data.get("i6") or "")
    if not i3:
        raise ParseError("showpage response has no i3 field")

    next_key = _find(r"load_image\(\s*['\"]([^'\"]*)", i3)
    if not next_key:
        next_key = _find(r"'([^']+)'", i3)
    image_tag = _required(
        r'(<img\b[^>]*\bid=["\']img["\'][^>]*>)',
        i3,
        "showpage image element",
    )
    resample = _required(
        r'\bsrc=["\']([^"\']+)',
        image_tag,
        "showpage resampled image URL",
    )
    nl = _find(r"\bnl\(([^)]*)\)", i3).strip("\"'")
    return _ShowPage(
        next_key=next_key,
        resample_url=html.unescape(resample),
        original_url=_fullimg_url(i6, root),
        nl=nl,
    )


def _parse_mpv_page(page: str) -> Tuple[List[Dict[str, object]], str]:
    raw = _required(
        r"var imagelist\s*=\s*(\[.*?\]);",
        page,
        "MPV imagelist",
    )
    key = _required(
        r'var mpvkey\s*=\s*["\']([^"\']+)',
        page,
        "MPV key",
    )
    try:
        images = json.loads(raw)
    except ValueError as exc:
        raise ParseError("invalid MPV imagelist: {}".format(exc)) from exc
    if not isinstance(images, list):
        raise ParseError("MPV imagelist is not a list")
    return images, key


def _filename_from_url(url: str) -> str:
    name = unquote(Path(urlsplit(url).path).name)
    return name or "image"


def _filename_from_content_disposition(value: str) -> str:
    match = re.search(
        r"filename\*=(?:[^']*)''([^;]+)",
        value,
        re.IGNORECASE,
    )
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(
        r'filename\s*=\s*(?:"([^"]+)"|([^;]+))',
        value,
        re.IGNORECASE,
    )
    if match:
        return (match.group(1) or match.group(2)).strip()
    return ""


def _safe_component(value: str, fallback: str = "item") -> str:
    value = INVALID_FILENAME_RE.sub("_", value).strip(" .")
    if not value:
        value = fallback
    stem = value.partition(".")[0].upper()
    if stem in WINDOWS_RESERVED:
        value = "_" + value
    return value[:180].rstrip(" .")


def _set_query(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query[name] = value
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        parts.fragment,
    ))


class EHClient:
    """Reference EH/EHX transport, discovery, and image enumerator."""

    def __init__(
            self,
            site: str = "eh",
            *,
            cookie_file: Optional[os.PathLike] = None,
            cookies: Optional[Mapping[str, str]] = None,
            session: Optional[requests.Session] = None,
            interval: float = 3.0,
            interval_max: Optional[float] = None,
            timeout: float = 30.0,
            retries: int = 2,
            fallback_retries: int = 2):
        if site not in ROOTS:
            raise ValueError("site must be 'eh' or 'exh'")
        self.site = site
        self.root = ROOTS[site]
        self.api_url = self.root + "/api.php"
        self.cookies_domain = "." + urlsplit(self.root).hostname
        self.session = session or requests.Session()
        self.interval = max(0.0, float(interval))
        self.interval_max = max(
            self.interval,
            float(interval_max if interval_max is not None else interval),
        )
        self.timeout = timeout
        self.retries = max(0, retries)
        self.fallback_retries = max(0, fallback_retries)
        self._last_request = 0.0

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; "
                "rv:128.0) Gecko/20100101 Firefox/128.0"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": self.root + "/",
        })
        if cookie_file:
            self.load_cookie_file(cookie_file)
        if cookies:
            for name, value in cookies.items():
                self.session.cookies.set(
                    name,
                    value,
                    domain=self.cookies_domain,
                )
        if site == "eh":
            self.session.cookies.set(
                "nw",
                "1",
                domain=self.cookies_domain,
            )

    @classmethod
    def from_gallery_url(cls, url: str, **kwargs) -> "EHClient":
        """Build a client whose root matches *url*."""
        return cls(site_from_gallery_url(url), **kwargs)

    def load_cookie_file(self, path: os.PathLike) -> None:
        """Load a Netscape/Mozilla cookies.txt export."""
        jar = MozillaCookieJar(str(path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, ValueError) as exc:
            raise AuthenticationError(
                "failed to load cookie file {}: {}".format(path, exc)
            ) from exc
        for cookie in jar:
            self.session.cookies.set_cookie(cookie)

    def _wait(self) -> None:
        interval = random.uniform(self.interval, self.interval_max)
        remaining = interval - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _request(
            self,
            method: str,
            url: str,
            *,
            check_blank: bool = True,
            **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        error = None

        for attempt in range(self.retries + 1):
            self._wait()
            try:
                response = self.session.request(method, url, **kwargs)
                self._last_request = time.monotonic()
            except requests.RequestException as exc:
                error = exc
                if attempt < self.retries:
                    continue
                break

            if response.status_code in {429, 500, 502, 503, 504}:
                error = RequestError(
                    "{} returned HTTP {}".format(url, response.status_code)
                )
                response.close()
                if attempt < self.retries:
                    continue
                break

            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                response.close()
                raise RequestError("request failed for {}: {}".format(
                    url, exc)) from exc

            if check_blank and not response.content and \
                    "Cache-Control" not in response.headers:
                response.close()
                raise AuthenticationError(
                    "blank response from {}; check site cookies".format(url)
                )
            return response

        raise RequestError("request failed for {}: {}".format(url, error))

    def _request_json(
            self,
            url: str,
            payload: Mapping[str, object]) -> Dict[str, object]:
        response = self._request("POST", url, json=payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise ParseError("invalid JSON response from {}".format(
                url)) from exc
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ParseError("JSON response from {} is not an object".format(
                url))
        if data.get("error"):
            raise EHError(str(data["error"]))
        return data

    def search_artist(
            self,
            artist: str,
            *,
            max_pages: Optional[int] = None) -> Iterator[GalleryLink]:
        """Yield galleries carrying the exact ``artist:<name>`` tag."""
        url = self.root + "/"
        params = {
            "f_search": artist_search_term(artist),
            "page": 0,
        }
        seen = set()
        page_count = 0

        while max_pages is None or page_count < max_pages:
            response = self._request("GET", url, params=params)
            page = response.text
            response.close()
            page_count += 1

            for match in GALLERY_RESULT_RE.finditer(page):
                gid = int(match.group("gid"))
                token = match.group("token")
                key = (gid, token)
                if key in seen:
                    continue
                seen.add(key)
                yield GalleryLink(
                    site=self.site,
                    gid=gid,
                    token=token,
                    url="{}/g/{}/{}/".format(self.root, gid, token),
                )

            next_url = re.search(r'nexturl="([^"]*)"', page)
            if next_url:
                value = html.unescape(next_url.group(1))
                if not value:
                    return
                url = urljoin(self.root + "/", value)
                params = None
            elif ">No hits found</p>" in page or \
                    'class="ptdd">&gt;<' in page:
                return
            else:
                params["page"] += 1

    def gallery_metadata(
            self,
            gid: int,
            token: str,
            api_url: Optional[str] = None) -> Dict[str, object]:
        """Fetch the optional ``gdata`` metadata record."""
        data = self._request_json(api_url or self.api_url, {
            "method": "gdata",
            "gidlist": [[gid, token]],
            "namespace": 1,
        })
        records = data.get("gmetadata")
        if not isinstance(records, list) or not records:
            raise ParseError("gdata response has no gallery metadata")
        record = records[0]
        if not isinstance(record, dict):
            raise ParseError("gdata gallery metadata is not an object")
        return record

    def open_gallery(self, url: str) -> Gallery:
        """Resolve a ``/g/`` or ``/mpv/`` URL into crawler state."""
        match = GALLERY_URL_RE.match(url)
        if not match:
            raise ValueError("unsupported gallery URL: {}".format(url))
        site = site_from_host(match.group("host"))
        if site != self.site:
            raise ValueError(
                "gallery belongs to {}, client is configured for {}".format(
                    site, self.site)
            )

        gid = int(match.group("gid"))
        token = match.group("token")
        start_page = int(match.group("page") or 1)
        gallery_url = "{}/g/{}/{}/".format(self.root, gid, token)
        response = self._request("GET", gallery_url)
        page = response.text
        response.close()

        if page.startswith(("Key missing", "Gallery not found")):
            raise ParseError("gallery is missing or its token is invalid")

        api_url = html.unescape(_find(
            r'var api_url\s*=\s*"([^"]+)',
            page,
            self.api_url,
        ))
        title = html.unescape(_find(
            r'<h1 id="gn">(.*?)</h1>',
            page,
            "{}".format(gid),
        )).strip()
        filecount_text = _find(
            r'>Length:</td>\s*<td class="gdt2">\s*(\d+)',
            page,
        )
        metadata: Dict[str, object] = {}
        if filecount_text:
            filecount = int(filecount_text)
        else:
            metadata = self.gallery_metadata(gid, token, api_url)
            filecount = int(metadata.get("filecount") or 0)
            title = html.unescape(str(metadata.get("title") or title))
        if filecount < 1:
            raise ParseError("gallery has no usable file count")
        if start_page > filecount:
            raise ValueError(
                "start page {} exceeds gallery length {}".format(
                    start_page,
                    filecount,
                )
            )

        tags = [
            unquote(value).replace("+", " ")
            for value in re.findall(r'hentai\.org/tag/([^"?#]+)', page)
        ]
        force_mpv = match.group("viewer").lower() == "mpv"
        mpv = force_mpv or page.count("hentai.org/mpv/") > 1
        return Gallery(
            site=self.site,
            root=self.root,
            gid=gid,
            token=token,
            url=gallery_url,
            title=title,
            filecount=filecount,
            api_url=api_url,
            mpv=mpv,
            start_page=start_page,
            tags=tags,
            metadata=metadata,
            page=page,
        )

    def iter_gallery_images(
            self,
            gallery: Gallery,
            mode: str = "resample") -> Iterator[Image]:
        """Yield selected image URLs for a resolved gallery.

        ``mode`` is either ``resample`` or ``original``.  Original mode is
        strict: a page without a per-image original raises ``ParseError``
        instead of silently switching quality.
        """
        if mode not in {"resample", "original"}:
            raise ValueError("mode must be 'resample' or 'original'")
        if gallery.site != self.site:
            raise ValueError("gallery and client sites do not match")
        if gallery.mpv:
            yield from self._iter_mpv_images(gallery, mode)
        else:
            yield from self._iter_standard_images(gallery, mode)

    def _make_image(
            self,
            gallery: Gallery,
            num: int,
            image_token: str,
            resample_url: str,
            original_url: Optional[str],
            nl: str,
            mode: str,
            viewer: str,
            filename: Optional[str] = None,
            refresh_payload: Optional[Dict[str, object]] = None) -> Image:
        if mode == "original":
            if not original_url:
                raise ParseError(
                    "image {} has no per-image original URL".format(num)
                )
            url = original_url
        else:
            url = resample_url
        if not filename:
            filename = _filename_from_url(url)
        return Image(
            gid=gallery.gid,
            num=num,
            image_token=image_token,
            url=url,
            resample_url=resample_url,
            original_url=original_url,
            filename=_safe_component(filename, "image"),
            mode=mode,
            viewer=viewer,
            nl=nl,
            api_url=gallery.api_url,
            refresh_payload=refresh_payload,
        )

    def _iter_standard_images(
            self,
            gallery: Gallery,
            mode: str) -> Iterator[Image]:
        match = None
        for candidate in IMAGE_PAGE_RE.finditer(gallery.page):
            if int(candidate.group("gid")) == gallery.gid:
                match = candidate
                break
        if match is None:
            raise ParseError("gallery page has no initial /s/ image URL")

        image_token = match.group("token")
        image_num = int(match.group("num"))
        image_url = "{}/s/{}/{}-{}".format(
            self.root,
            image_token,
            gallery.gid,
            image_num,
        )
        response = self._request("GET", image_url)
        page = response.text
        response.close()
        first = _parse_standard_page(page, self.root)
        first_image = self._make_image(
            gallery,
            image_num,
            first.start_key,
            first.resample_url,
            first.original_url,
            first.nl,
            mode,
            "standard",
        )
        if image_num >= gallery.start_page:
            yield first_image

        request = {
            "method": "showpage",
            "gid": gallery.gid,
            "page": 0,
            "imgkey": first.next_key,
            "showkey": first.show_key,
        }
        if gallery.filecount > image_num and not first.next_key:
            raise ParseError("first image page has no next image key")
        for num in range(image_num + 1, gallery.filecount + 1):
            current_key = str(request["imgkey"])
            request["page"] = num
            data = self._request_json(gallery.api_url, dict(request))
            parsed = _parse_showpage(data, self.root)
            image = self._make_image(
                gallery,
                num,
                current_key,
                parsed.resample_url,
                parsed.original_url,
                parsed.nl,
                mode,
                "standard",
            )
            if num >= gallery.start_page:
                yield image
            if num < gallery.filecount and not parsed.next_key:
                raise ParseError(
                    "showpage response for image {} has no next key".format(
                        num,
                    )
                )
            request["imgkey"] = parsed.next_key

    def _iter_mpv_images(
            self,
            gallery: Gallery,
            mode: str) -> Iterator[Image]:
        mpv_url = "{}/mpv/{}/{}/".format(
            self.root,
            gallery.gid,
            gallery.token,
        )
        response = self._request("GET", mpv_url)
        page = response.text
        response.close()
        if page.strip() == "eeenope":
            raise AuthenticationError(
                "MPV is unavailable for the current session"
            )
        images, mpvkey = _parse_mpv_page(page)

        start = max(0, gallery.start_page - 1)
        for index, entry in enumerate(images[start:], start=start):
            if not isinstance(entry, dict) or not entry.get("k"):
                raise ParseError("invalid MPV image record")
            num = index + 1
            image_token = str(entry["k"])
            payload: Dict[str, object] = {
                "method": "imagedispatch",
                "gid": gallery.gid,
                "page": num,
                "imgkey": image_token,
                "mpvkey": mpvkey,
            }
            info = self._request_json(gallery.api_url, payload)
            resample_url = str(info.get("i") or "")
            if not resample_url:
                raise ParseError("imagedispatch response has no image URL")
            original_url = None
            if info.get("o") and " " in str(info["o"]) and info.get("lf"):
                original_url = urljoin(
                    self.root + "/",
                    str(info["lf"]),
                )
            yield self._make_image(
                gallery,
                num,
                image_token,
                resample_url,
                original_url,
                str(info.get("s") or ""),
                mode,
                "mpv",
                str(entry.get("name") or "") or None,
                dict(payload),
            )

    def _refresh_image_url(self, image: Image) -> str:
        if image.is_original:
            return _set_query(image.url, "nl", image.nl)

        if image.viewer == "standard":
            url = "{}/s/{}/{}-{}?{}".format(
                self.root,
                image.image_token,
                image.gid,
                image.num,
                urlencode({"nl": image.nl}),
            )
            response = self._request("GET", url)
            page = response.text
            response.close()
            parsed = _parse_standard_page(page, self.root)
            image.nl = parsed.nl
            image.resample_url = parsed.resample_url
            return parsed.resample_url

        if image.viewer == "mpv" and image.refresh_payload:
            payload = dict(image.refresh_payload)
            payload["nl"] = image.nl
            data = self._request_json(image.api_url or self.api_url, payload)
            url = str(data.get("i") or "")
            if not url:
                raise ParseError("refreshed MPV response has no image URL")
            image.nl = str(data.get("s") or image.nl)
            image.resample_url = url
            return url

        raise ParseError("image has no refresh path")

    @staticmethod
    def _check_limit_url(url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path.lower()
        limited = (
            host.endswith("hentai.org") and path.endswith("/img/509.gif")
        ) or (
            host.endswith("ehgt.org") and path.endswith("/g/509.gif")
        )
        if limited:
            raise ImageLimitError("EH image limit reached")

    def _download_once(
            self,
            url: str,
            target: Path,
            overwrite: bool) -> Path:
        self._check_limit_url(url)
        response = self._request(
            "GET",
            url,
            check_blank=False,
            stream=True,
        )
        try:
            self._check_limit_url(getattr(response, "url", url) or url)
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith("text/html"):
                page = response.text
                if " requires GP" in page:
                    raise GPRequiredError(
                        "original image requires GP or confirmation"
                    )
                if " temporarily banned " in page:
                    raise AuthenticationError("EH reports a temporary ban")
                raise RequestError("image URL returned an HTML page")

            disposition_name = _filename_from_content_disposition(
                response.headers.get("Content-Disposition", "")
            )
            if disposition_name:
                prefix = target.name.partition("_")[0] + "_"
                target = target.with_name(prefix + _safe_component(
                    disposition_name,
                    "image",
                ))
            media_type = content_type.partition(";")[0]
            extension = mimetypes.guess_extension(media_type) or ""
            if extension and target.suffix.lower() in {"", ".php"}:
                target = target.with_suffix(extension)
            if target.exists() and not overwrite:
                return target

            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".part")
            chunks = response.iter_content(chunk_size=128 * 1024)
            first = next(chunks, b"")
            if not first:
                raise RequestError("image response is empty")
            signature = first[:32].lstrip().lower()
            if signature.startswith((b"<!doctype html", b"<html")):
                raise RequestError("image URL returned HTML content")
            if not any(first[:16]):
                raise RequestError("image response has an invalid signature")

            try:
                with temporary.open("wb") as output:
                    output.write(first)
                    for chunk in chunks:
                        if chunk:
                            output.write(chunk)
                os.replace(str(temporary), str(target))
            finally:
                if temporary.exists():
                    temporary.unlink()
            return target
        finally:
            response.close()

    def download_image(
            self,
            image: Image,
            directory: os.PathLike,
            *,
            overwrite: bool = False) -> Path:
        """Download one image, refreshing its URL after failed attempts."""
        name = "{:04d}_{}".format(image.num, image.filename)
        target = Path(directory) / _safe_component(name, "image")
        url = image.url
        last_error = None

        for attempt in range(self.fallback_retries + 1):
            try:
                return self._download_once(url, target, overwrite)
            except (GPRequiredError, ImageLimitError, AuthenticationError):
                raise
            except (EHError, requests.RequestException, OSError) as exc:
                last_error = exc
                if attempt >= self.fallback_retries:
                    break
                url = self._refresh_image_url(image)
        raise RequestError(
            "failed to download image {}: {}".format(image.num, last_error)
        )

    def download_resampled(
            self,
            gallery_url: str,
            output: os.PathLike,
            *,
            overwrite: bool = False) -> List[Path]:
        """Download viewer-sized images into a gallery directory."""
        gallery = self.open_gallery(gallery_url)
        directory = Path(output) / _safe_component(
            "{} {}".format(gallery.gid, gallery.title),
            str(gallery.gid),
        )
        files = []
        for image in self.iter_gallery_images(gallery, "resample"):
            files.append(self.download_image(
                image,
                directory,
                overwrite=overwrite,
            ))
        return files

    def download_original_zip(
            self,
            gallery_url: str,
            output: os.PathLike,
            *,
            overwrite: bool = False,
            compression: int = zipfile.ZIP_STORED) -> Path:
        """Download per-image originals, then atomically create a local ZIP."""
        gallery = self.open_gallery(gallery_url)
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        archive_name = _safe_component(
            "{} {}".format(gallery.gid, gallery.title),
            str(gallery.gid),
        ) + ".zip"
        archive = output / archive_name
        if archive.exists() and not overwrite:
            raise FileExistsError(str(archive))

        with tempfile.TemporaryDirectory(
                prefix=".eh-{}-".format(gallery.gid),
                dir=str(output)) as temporary_name:
            temporary = Path(temporary_name)
            image_dir = temporary / "images"
            files = []
            for image in self.iter_gallery_images(gallery, "original"):
                files.append(self.download_image(
                    image,
                    image_dir,
                    overwrite=True,
                ))
            if not files:
                raise ParseError("gallery yielded no original images")

            temporary_archive = temporary / "gallery.zip"
            with zipfile.ZipFile(
                    temporary_archive,
                    "w",
                    compression=compression,
                    allowZip64=True) as output_zip:
                for path in files:
                    output_zip.write(path, path.name)
            os.replace(str(temporary_archive), str(archive))
        return archive


def _cookie_values(values: Iterable[str]) -> Dict[str, str]:
    result = {}
    for value in values:
        name, separator, cookie = value.partition("=")
        if not separator or not name:
            raise ValueError("cookie must use NAME=VALUE syntax")
        result[name] = cookie
    return result


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cookie-file",
        help="Netscape cookies.txt export",
    )
    parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set one site cookie; may be repeated",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="minimum seconds between requests (default: 3)",
    )
    parser.add_argument(
        "--interval-max",
        type=float,
        default=6.0,
        help="maximum randomized request interval (default: 6)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser(
        "search-artist",
        help="find galleries by exact artist tag",
    )
    search.add_argument("artist")
    search.add_argument("--site", choices=("eh", "exh", "both"),
                        default="eh")
    search.add_argument("--max-pages", type=int)
    _add_common_options(search)

    download = commands.add_parser(
        "download",
        help="download one gallery",
    )
    download.add_argument("gallery_url")
    download.add_argument(
        "--mode",
        choices=("resample", "original-zip"),
        default="resample",
    )
    download.add_argument("--output", "-o", default="downloads")
    download.add_argument("--overwrite", action="store_true")
    download.add_argument("--fallback-retries", type=int, default=2)
    _add_common_options(download)
    return parser


def _client_options(args) -> Dict[str, object]:
    return {
        "cookie_file": args.cookie_file,
        "cookies": _cookie_values(args.cookie),
        "interval": args.interval,
        "interval_max": args.interval_max,
        "timeout": args.timeout,
        "retries": args.retries,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    options = _client_options(args)

    try:
        if args.command == "search-artist":
            sites = ("eh", "exh") if args.site == "both" else (args.site,)
            seen = set()
            for site in sites:
                client = EHClient(site, **options)
                for gallery in client.search_artist(
                        args.artist,
                        max_pages=args.max_pages):
                    key = (gallery.gid, gallery.token)
                    if key not in seen:
                        seen.add(key)
                        print(gallery.url)
            return 0

        options["fallback_retries"] = args.fallback_retries
        client = EHClient.from_gallery_url(args.gallery_url, **options)
        if args.mode == "resample":
            files = client.download_resampled(
                args.gallery_url,
                args.output,
                overwrite=args.overwrite,
            )
            for path in files:
                print(path)
        else:
            path = client.download_original_zip(
                args.gallery_url,
                args.output,
                overwrite=args.overwrite,
            )
            print(path)
        return 0
    except (EHError, OSError, ValueError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
