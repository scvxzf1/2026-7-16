from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit


_PIXIV_PAGE_RE = re.compile(r"^/(?:en/)?artworks/(\d+)(?:/|$)", re.I)
_PIXIV_LEGACY_RE = re.compile(r"^/i/(\d+)(?:/|$)", re.I)
_PIXIV_MEDIA_RE = re.compile(r"^(\d+)_(?:p\d+|ugoira\d*)", re.I)
_PIXIV_OLD_MEDIA_RE = re.compile(r"^(\d+)(?:[_.]|$)", re.I)
_TWITTER_STATUS_RE = re.compile(r"/(?:status|statuses)/(\d+)(?:/|$)", re.I)
_TWITTER_HOSTS = {
    "twitter.com",
    "fxtwitter.com",
    "vxtwitter.com",
    "x.com",
    "fixupx.com",
    "fixvx.com",
}
_PIXIV_MEDIA_PATH_MARKERS = {"img-original", "img-master", "img-zip-ugoira"}


def _parsed_url(value: Any):
    text = html.unescape(str(value or "").strip())
    if not text:
        return None
    if "://" not in text and not text.startswith("//"):
        text = "https://" + text
    elif text.startswith("//"):
        text = "https:" + text
    try:
        return urlsplit(text)
    except ValueError:
        return None


def source_key_from_url(value: Any) -> str | None:
    """Return a stable work key for a Pixiv or X/Twitter source URL."""
    parsed = _parsed_url(value)
    if parsed is None:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or "/"

    if host == "pixiv.net" or host.endswith(".pixiv.net"):
        match = _PIXIV_PAGE_RE.match(path) or _PIXIV_LEGACY_RE.match(path)
        if match:
            return f"pixiv:{match.group(1)}"
        illust_id = (parse_qs(parsed.query).get("illust_id") or [""])[0]
        if path.lower().endswith("/member_illust.php") and str(illust_id).isdigit():
            return f"pixiv:{illust_id}"

    if host == "pximg.net" or host.endswith(".pximg.net") or host.endswith(".pixiv.net"):
        segments = [segment for segment in path.split("/") if segment]
        if _PIXIV_MEDIA_PATH_MARKERS.intersection(segment.lower() for segment in segments):
            filename = segments[-1] if segments else ""
            match = _PIXIV_MEDIA_RE.match(filename) or _PIXIV_OLD_MEDIA_RE.match(filename)
            if match:
                return f"pixiv:{match.group(1)}"
        if re.match(r"^(?:i\d+|img\d*)\.pixiv\.net$", host, re.I) and "/img/" in path.lower():
            filename = segments[-1] if segments else ""
            match = _PIXIV_OLD_MEDIA_RE.match(filename)
            if match:
                return f"pixiv:{match.group(1)}"

    base_host = host.removeprefix("www.").removeprefix("mobile.")
    if base_host in _TWITTER_HOSTS:
        match = _TWITTER_STATUS_RE.search(path)
        if match:
            return f"twitter:{match.group(1)}"
    return None


def candidate_source_key(site: Any, source_id: Any, url: Any) -> str | None:
    """Build a key from extractor identity, falling back to its canonical URL."""
    normalized = str(site or "").strip().lower()
    if normalized == "x":
        normalized = "twitter"
    value = str(source_id or "").strip()
    if value.isdigit() and normalized in {"pixiv", "twitter"}:
        return f"{normalized}:{value}"
    return source_key_from_url(url)
