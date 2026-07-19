from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import requests
from curl_cffi import requests as curl_requests

from .classifier import FailureDecision, classify_result
from .gallery import GalleryRunner
from .proxy import ProxyLease, ProxyPoolAdapter
from .redaction import redact_text
from .schemas import ProxyMode, SitePolicy


@dataclass(frozen=True, slots=True)
class SearchSiteSpec:
    site: str
    aliases: tuple[str, ...]
    candidate_kind: str
    search_range: str
    authentication: str

    def search_url(self, keyword: str) -> str:
        if self.site == "twitter":
            return "https://x.com/search?" + urlencode({"q": keyword, "f": "live"})
        if self.site == "pixiv":
            return "https://www.pixiv.net/users/?" + urlencode({"nick": keyword})
        if self.site == "danbooru":
            return "https://danbooru.donmai.us/posts?" + urlencode({"tags": keyword})
        if self.site == "exhentai":
            return "https://e-hentai.org/?" + urlencode({"f_search": keyword})
        raise ValueError(f"未注册关键词搜索站点: {self.site}")


SITE_SPECS: tuple[SearchSiteSpec, ...] = (
    SearchSiteSpec(
        "twitter",
        ("twitter", "x"),
        "work",
        "post",
        "auth_token Cookie",
    ),
    SearchSiteSpec(
        "pixiv",
        ("pixiv",),
        "work",
        "post",
        "refresh token 或 PHPSESSID Cookie",
    ),
    SearchSiteSpec(
        "danbooru",
        ("danbooru",),
        "post",
        "post",
        "公开搜索可直接使用；账号/API key 可选",
    ),
    SearchSiteSpec(
        "exhentai",
        ("exhentai", "eh", "e-hentai", "e_hentai"),
        "gallery",
        "child",
        "E-Hentai 公开搜索可直接使用；ExHentai 使用 Cookie",
    ),
)

_SITE_INDEX = {alias: spec for spec in SITE_SPECS for alias in spec.aliases}
EXHENTAI_TAG_NAMESPACES: tuple[dict[str, Any], ...] = (
    {"namespace": "artist", "label": "艺术家 / 作者", "aliases": ["a"]},
    {"namespace": "character", "label": "角色", "aliases": ["c", "char"]},
    {"namespace": "cosplayer", "label": "角色扮演者", "aliases": ["cos"]},
    {"namespace": "female", "label": "女性", "aliases": ["f"]},
    {"namespace": "group", "label": "社团 / 团体", "aliases": ["g", "circle"]},
    {"namespace": "language", "label": "语言", "aliases": ["l", "lang"]},
    {"namespace": "location", "label": "地点", "aliases": ["loc"]},
    {"namespace": "male", "label": "男性", "aliases": ["m"]},
    {"namespace": "mixed", "label": "混合", "aliases": ["x"]},
    {"namespace": "other", "label": "其他", "aliases": ["o"]},
    {"namespace": "parody", "label": "原作 / 系列", "aliases": ["p", "series"]},
    {"namespace": "reclass", "label": "重分类", "aliases": ["r"]},
    {"namespace": "temp", "label": "临时标签", "aliases": []},
)
_EXHENTAI_TAG_NAMESPACE_INDEX = {
    alias: str(item["namespace"])
    for item in EXHENTAI_TAG_NAMESPACES
    for alias in (str(item["namespace"]), *item["aliases"])
}
_EXHENTAI_TAG_NAMESPACE_LABELS = {
    str(item["namespace"]): str(item["label"])
    for item in EXHENTAI_TAG_NAMESPACES
}
_DISCOVERY_MANAGED_ARGS = {
    "-g",
    "-G",
    "-j",
    "-J",
    "--get-urls",
    "--resolve-urls",
    "--dump-json",
    "--resolve-json",
    "--range",
    "--file-range",
    "--image-range",
    "--post-range",
    "--child-range",
    "--chapter-range",
}


class DiscoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class _DanbooruApiRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        proxy_fault: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.proxy_fault = proxy_fault


def _is_cloudflare_challenge(status_code: int | None, body: str) -> bool:
    lower = str(body or "").lower()
    return bool(
        status_code == 403
        and (
            "cloudflare" in lower
            or "just a moment" in lower
            or "cf-mitigated" in lower
            or "正在进行安全验证" in lower
        )
    )


def _danbooru_json_request(
    url: str,
    *,
    params: dict[str, Any],
    proxy_url: str | None,
    timeout: float,
) -> Any:
    """Read Danbooru JSON with a browser TLS fingerprint.

    Danbooru may put its JSON endpoints behind a Cloudflare browser check.  The
    ordinary gallery-dl/requests path remains the primary path; this request is
    the backend-owned fallback for that check and still uses the selected pool
    lease when one is present.
    """

    session = curl_requests.Session(trust_env=False)
    try:
        response = session.get(
            url,
            params=params,
            proxy=proxy_url,
            timeout=max(1.0, float(timeout)),
            allow_redirects=False,
            impersonate="chrome",
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status_code < 400:
            raise _DanbooruApiRequestError(
                f"Danbooru API 返回了未接受的重定向 (HTTP {status_code})",
                status_code=status_code,
            )
        if status_code >= 400:
            body = str(getattr(response, "text", "") or "")[:4000]
            challenge = _is_cloudflare_challenge(status_code, body)
            retryable = challenge or status_code in {408, 425, 429} or status_code >= 500
            raise _DanbooruApiRequestError(
                (
                    f"Danbooru API 遇到 Cloudflare 检查 (HTTP {status_code})"
                    if challenge
                    else f"Danbooru API 返回 HTTP {status_code}"
                ),
                status_code=status_code,
                retryable=retryable,
                proxy_fault=challenge or status_code == 429,
            )
        try:
            return response.json()
        except Exception as exc:
            body = str(getattr(response, "text", "") or "")[:4000]
            challenge = _is_cloudflare_challenge(status_code, body)
            raise _DanbooruApiRequestError(
                "Danbooru API 返回了无效 JSON",
                status_code=status_code,
                retryable=challenge,
                proxy_fault=challenge,
            ) from exc
    finally:
        session.close()


def search_site(site: str) -> SearchSiteSpec:
    key = str(site).strip().lower()
    try:
        return _SITE_INDEX[key]
    except KeyError as exc:
        raise ValueError(
            "site 当前支持 x/twitter、pixiv、danbooru、eh/exhentai"
        ) from exc


def search_site_catalog() -> list[dict[str, Any]]:
    catalog = []
    for spec in SITE_SPECS:
        item = {
            "site": spec.site,
            "aliases": list(spec.aliases),
            "candidate_kind": spec.candidate_kind,
            "authentication": spec.authentication,
        }
        if spec.site == "exhentai":
            item["tag_namespaces"] = [
                {
                    "namespace": namespace["namespace"],
                    "label": namespace["label"],
                    "aliases": list(namespace["aliases"]),
                }
                for namespace in EXHENTAI_TAG_NAMESPACES
            ]
        catalog.append(item)
    return catalog


def validate_discovery_args(args: list[str]) -> list[str]:
    values = [str(value) for value in args]
    for value in values:
        option = value.split("=", 1)[0]
        if option in _DISCOVERY_MANAGED_ARGS:
            raise ValueError(f"搜索输出协议参数由后端管理: {option}")
    return values


def _pixiv_first_media_args(args: list[str]) -> list[str]:
    values = list(args)
    for index in range(len(values) - 1, -1, -1):
        value = values[index]
        if value == "--filter":
            if index + 1 >= len(values):
                raise ValueError("--filter 缺少表达式")
            values[index + 1] = f"({values[index + 1]}) and (num == 0)"
            return values
        if value.startswith("--filter="):
            expression = value.split("=", 1)[1]
            values[index] = f"--filter=({expression}) and (num == 0)"
            return values
    values.extend(["--filter", "num == 0"])
    return values


def _text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit]


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _list(value: Any, limit: int = 50) -> list[str]:
    if isinstance(value, str):
        values = value.split()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return []
    return [str(item)[:200] for item in values if str(item).strip()][:limit]


def _exhentai_tag_parts(value: Any) -> tuple[str, str, str] | None:
    text = _text(value, 300)
    if not text:
        return None
    if ":" in text:
        prefix, tag_value = text.split(":", 1)
        raw_namespace = prefix.strip().lower()
        tag_value = tag_value.strip()
        namespace = _EXHENTAI_TAG_NAMESPACE_INDEX.get(raw_namespace, "unknown")
    else:
        namespace = "temp"
        tag_value = text
    if not tag_value:
        return None
    key = f"{namespace}:{tag_value.lower()}"
    return namespace, tag_value, key


def exhentai_tag_facets(addresses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group EH gallery tags by the official E-Hentai namespace taxonomy."""
    counts: dict[str, dict[str, dict[str, Any]]] = {}
    galleries: dict[str, set[int]] = {}
    for address_index, address in enumerate(addresses):
        metadata = address.get("metadata") if isinstance(address.get("metadata"), dict) else {}
        raw_tags = metadata.get("tags")
        if isinstance(raw_tags, str):
            tag_values = [raw_tags]
        elif isinstance(raw_tags, (list, tuple, set)):
            tag_values = raw_tags
        else:
            tag_values = []
        seen: set[str] = set()
        for raw_tag in tag_values:
            parts = _exhentai_tag_parts(raw_tag)
            if parts is None:
                continue
            namespace, value, key = parts
            if key in seen:
                continue
            seen.add(key)
            namespace_counts = counts.setdefault(namespace, {})
            item = namespace_counts.setdefault(
                key,
                {
                    "tag": f"{namespace}:{value}",
                    "value": value,
                    "count": 0,
                },
            )
            item["count"] += 1
            galleries.setdefault(namespace, set()).add(address_index)

    namespace_order = [
        str(item["namespace"]) for item in EXHENTAI_TAG_NAMESPACES
    ]
    namespace_order.extend(
        namespace for namespace in counts if namespace not in namespace_order
    )
    result: list[dict[str, Any]] = []
    for namespace in namespace_order:
        namespace_counts = counts.get(namespace)
        if not namespace_counts:
            continue
        tags = sorted(
            namespace_counts.values(),
            key=lambda item: (-int(item["count"]), str(item["value"]).lower()),
        )
        result.append(
            {
                "namespace": namespace,
                "label": _EXHENTAI_TAG_NAMESPACE_LABELS.get(namespace, "未识别命名空间"),
                "tag_count": len(tags),
                "gallery_count": len(galleries.get(namespace, set())),
                "tags": tags,
            }
        )
    return result


def _author(
    site: str,
    *,
    author_id: Any,
    name: Any,
    display_name: Any,
    profile_url: str | None,
    works_url: str | None,
) -> dict[str, Any] | None:
    name_text = _text(name, 200)
    display = _text(display_name, 200)
    if not (name_text or display or author_id is not None):
        return None
    return {
        "id": _text(author_id, 200),
        "site": site,
        "kind": "author",
        "name": name_text,
        "display_name": display,
        "url": profile_url,
        "works_url": works_url or profile_url,
    }


def _twitter_candidate(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    work_id = _integer(data.get("tweet_id"))
    if work_id is None:
        return None, []
    user = data.get("author") if isinstance(data.get("author"), dict) else {}
    username = _text(user.get("name"), 200)
    profile = f"https://x.com/{username}" if username else None
    author = _author(
        "twitter",
        author_id=user.get("id"),
        name=username,
        display_name=user.get("nick"),
        profile_url=profile,
        works_url=f"{profile}/media" if profile else None,
    )
    work_url = f"{profile}/status/{work_id}" if profile else f"https://x.com/i/web/status/{work_id}"
    candidate = {
        "id": str(work_id),
        "site": "twitter",
        "kind": "work",
        "title": _text(data.get("content"), 500) or f"Post {work_id}",
        "url": work_url,
        "download_url": work_url,
        "thumbnail_url": None,
        "media_count": _integer(data.get("count")) or 1,
        "author": author,
        "metadata": {
            "date": _text(data.get("date"), 100),
            "language": _text(data.get("lang"), 50),
            "favorite_count": _integer(data.get("favorite_count")),
            "retweet_count": _integer(data.get("retweet_count")),
            "reply_count": _integer(data.get("reply_count")),
            "view_count": _integer(data.get("view_count")),
            "hashtags": _list(data.get("hashtags")),
            "sensitive": data.get("sensitive"),
        },
    }
    return candidate, [author] if author else []


def _pixiv_candidate(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    work_id = _integer(data.get("id"))
    if work_id is None:
        return None, []
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    author_id = _integer(user.get("id"))
    profile = f"https://www.pixiv.net/users/{author_id}" if author_id is not None else None
    author = _author(
        "pixiv",
        author_id=author_id,
        name=user.get("account"),
        display_name=user.get("name"),
        profile_url=profile,
        works_url=f"{profile}/artworks" if profile else None,
    )
    work_url = f"https://www.pixiv.net/artworks/{work_id}"
    tags = data.get("tags")
    if isinstance(tags, list):
        tag_names = [
            str(item.get("name") or item.get("translated_name") or "")
            if isinstance(item, dict)
            else str(item)
            for item in tags
        ]
    else:
        tag_names = []
    candidate = {
        "id": str(work_id),
        "site": "pixiv",
        "kind": "work",
        "title": _text(data.get("title"), 500) or f"Artwork {work_id}",
        "url": work_url,
        "download_url": work_url,
        "thumbnail_url": None,
        "media_count": _integer(data.get("count")) or _integer(data.get("page_count")) or 1,
        "author": author,
        "metadata": {
            "date": _text(data.get("date") or data.get("create_date"), 100),
            "rating": _text(data.get("rating"), 50),
            "type": _text(data.get("type"), 50),
            "tags": [tag for tag in tag_names if tag][:50],
            "total_bookmarks": _integer(data.get("total_bookmarks")),
            "total_view": _integer(data.get("total_view")),
        },
    }
    return candidate, [author] if author else []


def _pixiv_user_queue(data: dict[str, Any]) -> tuple[None, list[dict[str, Any]]]:
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    author_id = _integer(user.get("id"))
    if author_id is None:
        return None, []
    profile = f"https://www.pixiv.net/users/{author_id}"
    author = _author(
        "pixiv",
        author_id=author_id,
        name=user.get("account"),
        display_name=user.get("name"),
        profile_url=profile,
        works_url=f"{profile}/artworks",
    )
    return None, [author] if author else []


def _danbooru_candidate(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    post_id = _integer(data.get("id"))
    if post_id is None:
        return None, []
    artists = _list(data.get("tags_artist") or data.get("tag_string_artist"), 20)
    authors: list[dict[str, Any]] = []
    for artist in artists:
        works_url = "https://danbooru.donmai.us/posts?" + urlencode({"tags": artist})
        item = _author(
            "danbooru",
            author_id=None,
            name=artist,
            display_name=artist.replace("_", " "),
            profile_url="https://danbooru.donmai.us/artists?" + urlencode({"search[name]": artist}),
            works_url=works_url,
        )
        if item:
            authors.append(item)
    work_url = f"https://danbooru.donmai.us/posts/{post_id}"
    copyrights = _list(data.get("tags_copyright") or data.get("tag_string_copyright"), 10)
    candidate = {
        "id": str(post_id),
        "site": "danbooru",
        "kind": "post",
        "title": " / ".join(copyrights) or f"Post {post_id}",
        "url": work_url,
        "download_url": work_url,
        "thumbnail_url": _text(
            data.get("preview_file_url")
            or data.get("large_file_url")
            or data.get("file_url")
            or data.get("source"),
            8192,
        ),
        "media_count": 1,
        "author": authors[0] if authors else None,
        "metadata": {
            "date": _text(data.get("date") or data.get("created_at"), 100),
            "rating": _text(data.get("rating"), 50),
            "score": _integer(data.get("score")),
            "width": _integer(data.get("image_width")),
            "height": _integer(data.get("image_height")),
            "artists": artists,
            "characters": _list(
                data.get("tags_character") or data.get("tag_string_character"),
                30,
            ),
            "copyrights": copyrights,
        },
    }
    return candidate, authors


def _danbooru_artist_queue(
    url: str,
    data: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    name = _text(data.get("name"), 200)
    if not name:
        return None, []
    artist_id = _integer(data.get("id"))
    author = _author(
        "danbooru",
        author_id=artist_id,
        name=name,
        display_name=name.replace("_", " "),
        profile_url=(
            f"https://danbooru.donmai.us/artists/{artist_id}"
            if artist_id is not None
            else "https://danbooru.donmai.us/artists?" + urlencode({"search[name]": name})
        ),
        works_url=url,
    )
    if author is not None:
        author["other_names"] = [str(value) for value in data.get("other_names") or []][:50]
        author["group_name"] = _text(data.get("group_name"), 200)
        author["origin"] = "danbooru_artist_directory"
    return None, [author] if author else []


def _exhentai_directory_candidate(
    data: dict[str, Any], source_url: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    gallery_id = _integer(data.get("gid") or data.get("gallery_id"))
    token = _text(data.get("token") or data.get("gallery_token"), 100)
    if gallery_id is None:
        return None, []
    if token:
        host = "exhentai.org" if "exhentai.org" in source_url else "e-hentai.org"
        url = f"https://{host}/g/{gallery_id}/{token}/"
    else:
        url = source_url
    uploader = _text(data.get("uploader"), 200)
    author = None
    if uploader:
        works_url = "https://e-hentai.org/?" + urlencode({"f_search": f'uploader:"{uploader}$"'})
        author = _author(
            "exhentai",
            author_id=None,
            name=uploader,
            display_name=uploader,
            profile_url=works_url,
            works_url=works_url,
        )
    candidate = {
        "id": str(gallery_id),
        "site": "exhentai",
        "kind": "gallery",
        "title": _text(data.get("title"), 500) or f"Gallery {gallery_id}",
        "url": url,
        "download_url": url,
        "thumbnail_url": _text(data.get("thumb"), 8192),
        "media_count": _integer(data.get("filecount")),
        "author": author,
        "metadata": {
            "title_jpn": _text(data.get("title_jpn"), 500),
            "category": _text(data.get("eh_category") or data.get("category"), 100),
            "language": _text(data.get("language"), 100),
            "rating": _text(data.get("rating"), 50),
            "tags": _list(data.get("tags"), 50),
        },
    }
    return candidate, [author] if author else []


def _exhentai_queue_candidate(
    url: str, data: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    gallery_id = _integer(data.get("gallery_id"))
    if gallery_id is None:
        match = re.search(r"/g/(\d+)/", url)
        gallery_id = int(match.group(1)) if match else None
    if gallery_id is None:
        return None, []
    normalized = url if url.endswith("/") else url + "/"
    return {
        "id": str(gallery_id),
        "site": "exhentai",
        "kind": "gallery",
        "title": f"Gallery {gallery_id}",
        "url": normalized,
        "download_url": normalized,
        "thumbnail_url": None,
        "media_count": None,
        "author": None,
        "metadata": {"gallery_token": _text(data.get("gallery_token"), 100)},
    }, []


def parse_discovery_output(
    site: str,
    stdout: str,
    *,
    source_url: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DiscoveryError(
            "invalid_gallery_output",
            "gallery-dl 搜索输出不是有效 JSON",
            details={"line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(payload, list):
        raise DiscoveryError("invalid_gallery_output", "gallery-dl 搜索输出结构无效")

    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    candidate_by_id: dict[str, dict[str, Any]] = {}
    authors: list[dict[str, Any]] = []
    author_keys: set[str] = set()

    def add(candidate: dict[str, Any] | None, related: list[dict[str, Any]]) -> None:
        if candidate is not None:
            key = str(candidate["id"])
            if key not in candidate_by_id and len(candidates) < limit:
                candidate_by_id[key] = candidate
                candidates.append(candidate)
        for author in related:
            author_site = str(author.get("site") or site)
            author_id = str(author.get("id") or "").strip()
            raw_key = str(author.get("works_url") or author.get("url") or author.get("name") or "")
            if author_id:
                key = f"{author_site}:id:{author_id}"
            elif "://" in raw_key:
                try:
                    key = canonical_gallery_address(author_site, raw_key)
                except ValueError:
                    key = raw_key
            else:
                key = raw_key.casefold()
            if key and key not in author_keys:
                author_keys.add(key)
                authors.append(author)

    for message in payload:
        if not isinstance(message, list) or not message:
            continue
        message_type = message[0]
        if message_type == -1 and len(message) > 1 and isinstance(message[-1], dict):
            errors.append(str(message[-1].get("message") or message[-1].get("error") or "extractor error"))
            continue
        if message_type == 2 and len(message) >= 2 and isinstance(message[-1], dict):
            data = message[-1]
            if site == "twitter":
                add(*_twitter_candidate(data))
            elif site == "pixiv":
                add(*_pixiv_candidate(data))
            elif site == "danbooru":
                add(*_danbooru_candidate(data))
            elif site == "exhentai":
                add(*_exhentai_directory_candidate(data, source_url))
        elif message_type == 6 and len(message) >= 3 and isinstance(message[-1], dict):
            if site == "exhentai":
                add(*_exhentai_queue_candidate(str(message[1]), message[-1]))
            elif site == "danbooru":
                add(*_danbooru_artist_queue(str(message[1]), message[-1]))
            elif site == "pixiv":
                add(*_pixiv_user_queue(message[-1]))
        elif message_type == 3 and len(message) >= 3 and isinstance(message[-1], dict):
            data = message[-1]
            item_id = data.get("tweet_id") if site == "twitter" else data.get("id")
            if site == "exhentai":
                item_id = data.get("gid") or data.get("gallery_id")
            candidate = candidate_by_id.get(str(item_id))
            if candidate is not None:
                media_url = _text(message[1], 8192)
                if site == "twitter" and media_url:
                    media_urls = candidate.setdefault("media_urls", [])
                    if media_url not in media_urls:
                        media_urls.append(media_url)
                if media_url and not candidate.get("thumbnail_url"):
                    candidate["thumbnail_url"] = media_url

    if errors:
        raise DiscoveryError(
            "extractor_error",
            redact_text(errors[0], limit=1000),
            details={"errors": [redact_text(item, limit=500) for item in errors[:5]]},
        )
    return candidates, authors


def _tag_key(value: str) -> str:
    return re.sub(r"[\s_-]+", "_", str(value).strip().lower()).strip("_")


def _identity_key(value: Any) -> str:
    return _tag_key(str(value or "").strip().lstrip("@"))


def canonical_gallery_address(site: str, url: str) -> str:
    spec = search_site(site)
    text = str(url).strip()
    parsed = urlsplit(text)
    if not parsed.hostname:
        return text
    host = (parsed.hostname or "").lower()
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/")
    query = parsed.query
    if spec.site == "twitter" and host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        host = "x.com"
        segments = [segment for segment in path.split("/") if segment]
        reserved = {"i", "home", "search", "intent", "share", "hashtag", "explore"}
        if len(segments) == 1 and segments[0].lower() not in reserved:
            path = f"/{segments[0].lower()}/media"
        elif (
            len(segments) == 2
            and segments[0].lower() not in reserved
            and segments[1].lower() == "media"
        ):
            path = f"/{segments[0].lower()}/media"
        query = ""
    elif spec.site == "pixiv" and host in {"pixiv.net", "www.pixiv.net"}:
        host = "www.pixiv.net"
        match = re.match(r"^/users/(\d+)(?:/artworks)?$", path)
        if match:
            path = f"/users/{match.group(1)}/artworks"
        query = ""
    elif spec.site == "danbooru" and host in {"danbooru.donmai.us", "www.danbooru.donmai.us"}:
        host = "danbooru.donmai.us"
        tags = (parse_qs(parsed.query).get("tags") or [""])[0]
        if path == "/posts" and tags:
            query = urlencode({"tags": tags})
    elif spec.site == "exhentai" and re.match(r"^/g/\d+/[0-9a-f]{10}$", path, re.I):
        path += "/"
    return urlunsplit(("https", host, path or "/", query, ""))


def discovery_addresses(
    site: str,
    result: dict[str, Any],
    *,
    keyword: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Convert extractor evidence into selectable account/tag/gallery addresses."""
    spec = search_site(site)
    addresses: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        raw_url = str(item.get("url") or "").strip()
        if not raw_url:
            return
        url = canonical_gallery_address(spec.site, raw_url)
        if url and url not in seen and len(addresses) < limit:
            item["url"] = url
            item["source"] = spec.site
            item.setdefault("origin", "site_search")
            addresses.append(item)
            seen.add(url)

    candidates = [item for item in result.get("candidates") or [] if isinstance(item, dict)]
    authors = [item for item in result.get("authors") or [] if isinstance(item, dict)]
    if spec.site in {"twitter", "pixiv"}:
        address_type = "account"
        keyword_key = _identity_key(keyword)
        for author in authors:
            url = str(author.get("works_url") or author.get("url") or "").strip()
            if not url:
                continue
            author_id = str(author.get("id") or "").strip()
            author_name = _tag_key(author.get("name") or "")
            address_identity = author_id or uuid.uuid5(
                uuid.NAMESPACE_URL,
                canonical_gallery_address(spec.site, url),
            ).hex
            samples = []
            for item in candidates:
                candidate_author = (
                    item.get("author") if isinstance(item.get("author"), dict) else {}
                )
                candidate_id = str(candidate_author.get("id") or "").strip()
                candidate_name = _tag_key(candidate_author.get("name") or "")
                same_id = bool(author_id and candidate_id and author_id == candidate_id)
                same_name = bool(author_name and candidate_name and author_name == candidate_name)
                if same_id or same_name:
                    samples.append(item)
            identity_keys = {
                key
                for key in (
                    _identity_key(author.get("name")),
                    _identity_key(author.get("display_name")),
                )
                if key
            }
            exact_identity = bool(keyword_key and keyword_key in identity_keys)
            verified = bool(exact_identity and samples)
            evidence_reasons = ["site_search_work_evidence"] if samples else []
            if exact_identity:
                evidence_reasons.append("account_name_exact_match")
            if not verified:
                evidence_reasons.append("account_identity_unverified")
            add(
                {
                    "id": f"{spec.site}:account:{address_identity}",
                    "address_type": address_type,
                    "label": author.get("display_name") or author.get("name") or address_identity,
                    "url": url,
                    "profile_url": author.get("url"),
                    "matched_items": len(samples),
                    "confidence": "verified" if verified else "weak_evidence",
                    "evidence_reasons": evidence_reasons,
                    "sample_thumbnails": [
                        item["thumbnail_url"]
                        for item in samples
                        if item.get("thumbnail_url")
                    ][:5],
                }
            )
        return addresses

    if spec.site == "danbooru":
        keyword_key = _identity_key(keyword)
        artist_counts: dict[str, int] = {}
        artist_names: dict[str, str] = {}
        character_counts: dict[str, int] = {}
        character_names: dict[str, str] = {}
        author_by_key: dict[str, dict[str, Any]] = {}
        directory_authors: list[dict[str, Any]] = []

        for candidate in candidates:
            metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
            for artist in metadata.get("artists") or []:
                name = str(artist).strip()
                key = _identity_key(name)
                if key:
                    artist_names.setdefault(key, name)
                    artist_counts[key] = artist_counts.get(key, 0) + 1
            for character in metadata.get("characters") or []:
                name = str(character).strip()
                key = _identity_key(name)
                if key and key == keyword_key:
                    character_names.setdefault(key, name)
                    character_counts[key] = character_counts.get(key, 0) + 1

        for author in authors:
            name = str(author.get("name") or "").strip()
            key = _identity_key(name)
            if not key:
                continue
            author_by_key.setdefault(key, author)
            if str(author.get("origin") or "") == "danbooru_artist_directory":
                directory_authors.append(author)

        ordered_artists: list[tuple[str, dict[str, Any], str, str]] = []
        emitted_artist_keys: set[str] = set()
        primary_directory_authors = [
            author
            for author in directory_authors
            if keyword_key
            and _identity_key(author.get("name")) == keyword_key
        ]
        alias_directory_authors = [
            author
            for author in directory_authors
            if keyword_key
            and any(
                _identity_key(value) == keyword_key
                for value in author.get("other_names") or []
            )
        ]
        if primary_directory_authors:
            selected_directory_authors = primary_directory_authors
            directory_reason = "danbooru_artist_directory_match"
            directory_confidence = "verified"
        else:
            selected_directory_authors = alias_directory_authors
            directory_reason = "danbooru_artist_directory_alias_match"
            directory_confidence = "weak_evidence"

        for author in selected_directory_authors:
            name = str(author.get("name") or "").strip()
            key = _identity_key(name)
            if not key or key in emitted_artist_keys:
                continue
            emitted_artist_keys.add(key)
            ordered_artists.append(
                (name, author, directory_reason, directory_confidence)
            )
        if keyword_key in artist_names and keyword_key not in emitted_artist_keys:
            name = artist_names[keyword_key]
            emitted_artist_keys.add(keyword_key)
            ordered_artists.append(
                (
                    name,
                    author_by_key.get(keyword_key, {"name": name}),
                    "artist_tag_exact_match",
                    "verified",
                )
            )

        for name, author, reason, confidence in ordered_artists[:limit]:
            url = canonical_gallery_address(
                "danbooru",
                "https://danbooru.donmai.us/posts?" + urlencode({"tags": name}),
            )
            if url in seen:
                continue
            addresses.append(
                {
                    "id": f"danbooru:artist_tag:{name}",
                    "source": spec.site,
                    "origin": str(author.get("origin") or "site_search"),
                    "address_type": "artist_tag",
                    "label": name.replace("_", " "),
                    "url": url,
                    "profile_url": author.get("url"),
                    "matched_items": max(1, artist_counts.get(_identity_key(name), 0)),
                    "tag": name,
                    "confidence": confidence,
                    "evidence_reasons": [reason],
                    "related_profiles": [],
                }
            )
            seen.add(url)

        if keyword_key in character_names:
            name = character_names[keyword_key]
            url = canonical_gallery_address(
                "danbooru",
                "https://danbooru.donmai.us/posts?" + urlencode({"tags": name}),
            )
            if url not in seen:
                addresses.append(
                    {
                        "id": f"danbooru:character_tag:{name}",
                        "source": spec.site,
                        "origin": "site_search",
                        "address_type": "character_tag",
                        "label": name.replace("_", " "),
                        "url": url,
                        "profile_url": None,
                        "matched_items": character_counts[keyword_key],
                        "tag": name,
                        "confidence": "verified",
                        "evidence_reasons": ["character_tag_exact_match"],
                        "related_profiles": [],
                    }
                )
                seen.add(url)
        return addresses

    for candidate in candidates:
        url = str(candidate.get("download_url") or candidate.get("url") or "").strip()
        if not url:
            continue
        add(
            {
                "id": f"exhentai:gallery:{candidate.get('id') or url}",
                "address_type": "gallery",
                "label": candidate.get("title") or f"Gallery {candidate.get('id')}",
                "url": url,
                "thumbnail_url": candidate.get("thumbnail_url"),
                "media_count": candidate.get("media_count"),
                "metadata": candidate.get("metadata") or {},
                "confidence": "site_search",
                "evidence_reasons": ["keyword_gallery_search"],
            }
        )
    return addresses


def classify_external_profile(url: str) -> dict[str, Any]:
    """Classify a Danbooru-maintained artist URL and derive a crawlable account URL."""
    text = str(url).strip()
    parsed = urlsplit(text)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    platform = host or "external"
    crawl_site: str | None = None
    crawl_url: str | None = None

    if host in {"x.com", "twitter.com"}:
        platform = "twitter"
        reserved = {"i", "home", "search", "intent", "share", "hashtag", "explore"}
        if len(segments) == 1 and segments[0].lower() not in reserved:
            crawl_site = "twitter"
            crawl_url = f"https://x.com/{segments[0].lower()}/media"
    elif host in {"pixiv.net", "www.pixiv.net"}:
        platform = "pixiv"
        match = re.match(r"^/users/(\d+)", path)
        if not match and path.endswith("/member.php"):
            user_id = (parse_qs(parsed.query).get("id") or [""])[0]
            match = re.match(r"^(\d+)$", user_id)
        if match:
            crawl_site = "pixiv"
            crawl_url = f"https://www.pixiv.net/users/{match.group(1)}/artworks"
    elif host.endswith("fanbox.cc") or "pixiv.net/fanbox" in text:
        platform = "fanbox"
    elif host == "skeb.jp":
        platform = "skeb"

    return {
        "url": text,
        "platform": platform,
        "crawl_site": crawl_site,
        "crawl_url": crawl_url,
    }


class DiscoveryService:
    def __init__(
        self,
        gallery: GalleryRunner,
        proxy: ProxyPoolAdapter,
        runtime_dir: Path,
        *,
        auth_failure_callback: Callable[[str, str | None, str], Awaitable[bool]] | None = None,
    ) -> None:
        self.gallery = gallery
        self.proxy = proxy
        self.runtime_dir = (runtime_dir / "discovery").resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.auth_failure_callback = auth_failure_callback

    def _danbooru_node_tags(self, policy: SitePolicy) -> list[str]:
        if policy.node_tags:
            return list(policy.node_tags)
        try:
            nodes = self.proxy.status().get("nodes") or []
        except Exception:
            return []
        # Cloudflare currently admits the US exits in common airport pools more
        # consistently.  This is only a preference discovered from the pool's
        # normalized region tags; if none exist, acquisition falls back to the
        # whole pool below.
        if any(
            isinstance(node, dict)
            and node.get("healthy")
            and "us" in (node.get("tags") or [])
            for node in nodes
        ):
            return ["us"]
        return []

    async def _danbooru_api_json(
        self,
        url: str,
        *,
        params: dict[str, Any],
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
    ) -> tuple[Any, dict[str, Any], int]:
        mode: ProxyMode = proxy_mode or policy.proxy_mode
        attempts = max(1, policy.retry_limit + 1)
        tried: set[str] = set()
        preferred_tags = self._danbooru_node_tags(policy)
        last_error = "Danbooru API 请求失败"
        last_status: int | None = None

        for attempt in range(1, attempts + 1):
            operation_id = f"dan-api-{uuid.uuid4().hex}"
            lease: ProxyLease | None = None
            proxy_fault = False
            retryable = True
            try:
                if mode != "direct":
                    try:
                        lease = await asyncio.to_thread(
                            self.proxy.acquire,
                            operation_id,
                            node_tags=preferred_tags,
                            exclude_ids=tried,
                            allowed_ids=getattr(policy, "allowed_proxy_ids", None),
                            probe_before_use=policy.probe_before_use,
                            probe_url=policy.probe_url,
                        )
                        if (
                            lease is None
                            and preferred_tags
                            and not policy.node_tags
                        ):
                            lease = await asyncio.to_thread(
                                self.proxy.acquire,
                                operation_id,
                                node_tags=[],
                                exclude_ids=tried,
                                allowed_ids=getattr(policy, "allowed_proxy_ids", None),
                                probe_before_use=policy.probe_before_use,
                                probe_url=policy.probe_url,
                            )
                    except Exception as exc:
                        last_error = redact_text(exc, limit=1000)
                        if mode == "required":
                            raise DiscoveryError(
                                "proxy_unavailable",
                                last_error,
                                details={"attempts": attempt},
                            ) from exc
                    if lease is None and mode == "required":
                        raise DiscoveryError(
                            "proxy_unavailable",
                            "当前没有符合 Danbooru API 查询策略的健康代理节点",
                            details={"attempts": attempt},
                        )

                payload = await asyncio.to_thread(
                    _danbooru_json_request,
                    url,
                    params=params,
                    proxy_url=lease.endpoint if lease else None,
                    timeout=policy.http_timeout,
                )
                return (
                    payload,
                    {
                        "mode": mode,
                        "used": lease is not None,
                        "node_id": lease.node_id if lease else None,
                        "node_name": lease.name if lease else None,
                        "protocol": lease.protocol if lease else None,
                    },
                    attempt,
                )
            except DiscoveryError:
                raise
            except _DanbooruApiRequestError as exc:
                last_error = redact_text(exc, limit=1000)
                last_status = exc.status_code
                retryable = exc.retryable
                proxy_fault = bool(lease is not None and exc.proxy_fault)
            except Exception as exc:
                last_error = redact_text(exc, limit=1000)
                retryable = True
                proxy_fault = lease is not None
            finally:
                if lease is not None:
                    if proxy_fault:
                        tried.add(lease.node_id)
                    try:
                        await asyncio.to_thread(
                            self.proxy.release,
                            operation_id,
                            proxy_fault=proxy_fault,
                            reason=last_error if proxy_fault else "",
                        )
                    except Exception:
                        pass

            if not retryable or attempt >= attempts:
                break
            if policy.backoff_base_seconds:
                await asyncio.sleep(
                    min(policy.backoff_base_seconds * (2 ** (attempt - 1)), 10.0)
                )

        raise DiscoveryError(
            "danbooru_api_failed",
            last_error,
            details={"attempts": attempts, "status_code": last_status},
        )

    async def _search_danbooru_posts_api(
        self,
        *,
        keyword: str,
        limit: int,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
    ) -> dict[str, Any]:
        search_url = search_site("danbooru").search_url(keyword)
        payload, proxy_info, attempts = await self._danbooru_api_json(
            "https://danbooru.donmai.us/posts.json",
            params={"tags": keyword, "limit": min(max(1, int(limit)), 200)},
            policy=policy,
            proxy_mode=proxy_mode,
        )
        if not isinstance(payload, list):
            raise DiscoveryError(
                "danbooru_api_protocol",
                "Danbooru posts API 返回结构无效",
            )
        candidates: list[dict[str, Any]] = []
        authors: list[dict[str, Any]] = []
        author_keys: set[str] = set()
        for row in payload[:limit]:
            if not isinstance(row, dict):
                continue
            candidate, row_authors = _danbooru_candidate(row)
            if candidate is not None:
                candidates.append(candidate)
            for author in row_authors:
                key = str(author.get("works_url") or author.get("name") or "")
                if key and key not in author_keys:
                    author_keys.add(key)
                    authors.append(author)
        return {
            "site": "danbooru",
            "keyword": keyword,
            "search_url": search_url,
            "candidate_count": len(candidates),
            "author_count": len(authors),
            "candidates": candidates,
            "authors": authors,
            "proxy": proxy_info,
            "attempts": attempts,
            "transport": "danbooru_api_browser_fingerprint",
        }

    async def _search_danbooru_artists_api(
        self,
        *,
        keyword: str,
        limit: int,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
    ) -> dict[str, Any]:
        parts = [part for part in re.split(r"\s+", keyword.strip()) if part]
        pattern = "*".join(parts)
        search_url = "https://danbooru.donmai.us/artists?" + urlencode(
            {"search[any_name_matches]": pattern}
        )
        payload, proxy_info, attempts = await self._danbooru_api_json(
            "https://danbooru.donmai.us/artists.json",
            params={"search[any_name_matches]": pattern, "limit": min(max(1, int(limit)), 200)},
            policy=policy,
            proxy_mode=proxy_mode,
        )
        if not isinstance(payload, list):
            raise DiscoveryError(
                "danbooru_api_protocol",
                "Danbooru artists API 返回结构无效",
            )
        authors: list[dict[str, Any]] = []
        for row in payload[:limit]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            works_url = "https://danbooru.donmai.us/posts?" + urlencode({"tags": name})
            _candidate, row_authors = _danbooru_artist_queue(works_url, row)
            authors.extend(row_authors)
        return {
            "site": "danbooru",
            "keyword": keyword,
            "search_url": search_url,
            "candidate_count": 0,
            "author_count": len(authors),
            "candidates": [],
            "authors": authors,
            "proxy": proxy_info,
            "attempts": attempts,
            "transport": "danbooru_api_browser_fingerprint",
        }

    async def search(
        self,
        *,
        site: str,
        keyword: str,
        limit: int,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
        credentials_ref: str | None,
        cookies_file: str | None,
        config_file: str | None,
        extra_args: list[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        spec = search_site(site)
        url = spec.search_url(keyword)
        try:
            result = await self.discover_url(
                site=spec.site,
                url=url,
                keyword=keyword,
                limit=limit,
                range_kind=spec.search_range,
                policy=policy,
                proxy_mode=proxy_mode,
                credentials_ref=credentials_ref,
                cookies_file=cookies_file,
                config_file=config_file,
                extra_args=extra_args,
                timeout_seconds=timeout_seconds,
            )
        except DiscoveryError:
            if spec.site != "danbooru":
                raise
            return await self._search_danbooru_posts_api(
                keyword=keyword,
                limit=limit,
                policy=policy,
                proxy_mode=proxy_mode,
            )
        if spec.site == "danbooru" and not result.get("candidate_count"):
            return await self._search_danbooru_posts_api(
                keyword=keyword,
                limit=limit,
                policy=policy,
                proxy_mode=proxy_mode,
            )
        return result

    async def enrich_exhentai_previews(
        self,
        result: dict[str, Any],
        *,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Fill EH search candidates with batched gdata titles and cover URLs."""
        candidates = [
            item for item in result.get("candidates") or [] if isinstance(item, dict)
        ]
        pairs: list[tuple[int, str]] = []
        seen: set[int] = set()
        for candidate in candidates:
            gallery_id = _integer(candidate.get("id"))
            metadata = (
                candidate.get("metadata")
                if isinstance(candidate.get("metadata"), dict)
                else {}
            )
            token = _text(metadata.get("gallery_token"), 100)
            if gallery_id is None or not token or gallery_id in seen:
                continue
            pairs.append((gallery_id, token))
            seen.add(gallery_id)
        if not pairs:
            result["preview_count"] = 0
            result["preview_missing_count"] = len(candidates)
            return result

        mode: ProxyMode = proxy_mode or policy.proxy_mode
        attempts = max(1, policy.retry_limit + 1)
        tried: set[str] = set()
        last_error = "EH 画廊预览资料读取失败"
        rows: list[dict[str, Any]] | None = None

        for attempt in range(1, attempts + 1):
            operation_id = f"eh-preview-{uuid.uuid4().hex}"
            lease: ProxyLease | None = None
            proxy_fault = False
            try:
                if mode != "direct":
                    try:
                        lease = await asyncio.to_thread(
                            self.proxy.acquire,
                            operation_id,
                            node_tags=policy.node_tags,
                            exclude_ids=tried,
                            allowed_ids=getattr(policy, "allowed_proxy_ids", None),
                            probe_before_use=policy.probe_before_use,
                            probe_url=policy.probe_url,
                        )
                    except Exception as exc:
                        if mode == "required":
                            raise DiscoveryError(
                                "proxy_unavailable", redact_text(exc, limit=1000)
                            ) from exc
                    if lease is None and mode == "required":
                        raise DiscoveryError(
                            "proxy_unavailable",
                            "当前没有符合 EH 预览查询策略的健康代理节点",
                        )

                def request_previews() -> list[dict[str, Any]]:
                    session = requests.Session()
                    session.headers["User-Agent"] = "gallery-dl-backend/0.3"
                    proxies = (
                        {"http": lease.endpoint, "https": lease.endpoint}
                        if lease
                        else None
                    )
                    request_timeout = max(
                        1.0,
                        min(float(policy.http_timeout), float(timeout_seconds)),
                    )
                    items: list[dict[str, Any]] = []
                    for offset in range(0, len(pairs), 25):
                        response = session.post(
                            "https://api.e-hentai.org/api.php",
                            json={
                                "method": "gdata",
                                "gidlist": [list(pair) for pair in pairs[offset : offset + 25]],
                                "namespace": 1,
                            },
                            proxies=proxies,
                            timeout=request_timeout,
                            allow_redirects=False,
                        )
                        if 300 <= int(getattr(response, "status_code", 200)) < 400:
                            raise ValueError("EH gdata API 返回了未接受的重定向")
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            raise ValueError("EH gdata API 返回结构无效")
                        if payload.get("error"):
                            raise ValueError(str(payload["error"]))
                        metadata = payload.get("gmetadata")
                        if not isinstance(metadata, list):
                            raise ValueError("EH gdata API 缺少 gmetadata")
                        items.extend(item for item in metadata if isinstance(item, dict))
                    return items

                rows = await asyncio.to_thread(request_previews)
                break
            except DiscoveryError:
                raise
            except Exception as exc:
                last_error = redact_text(exc, limit=1000)
                decision = classify_result(1, last_error)
                response = getattr(exc, "response", None)
                status_code = _integer(getattr(response, "status_code", None))
                transient_request = isinstance(
                    exc,
                    (requests.Timeout, requests.ConnectionError),
                ) or bool(
                    status_code
                    and (status_code in {408, 425, 429} or status_code >= 500)
                )
                proxy_fault = decision.proxy_fault
                if lease is not None and proxy_fault:
                    tried.add(lease.node_id)
                if not decision.retryable and not proxy_fault and not transient_request:
                    break
            finally:
                if lease is not None:
                    try:
                        await asyncio.to_thread(
                            self.proxy.release,
                            operation_id,
                            proxy_fault=proxy_fault,
                            reason=last_error if proxy_fault else "",
                        )
                    except Exception:
                        pass
            if attempt < attempts and policy.backoff_base_seconds:
                await asyncio.sleep(
                    min(policy.backoff_base_seconds * (2 ** (attempt - 1)), 10.0)
                )

        if rows is None:
            raise DiscoveryError(
                "exhentai_preview_lookup_failed",
                last_error,
                details={"attempts": attempts},
            )

        row_by_id = {
            str(item.get("gid")): item
            for item in rows
            if _integer(item.get("gid")) is not None and not item.get("error")
        }
        enriched_candidates: list[dict[str, Any]] = []
        merged_authors = list(result.get("authors") or [])
        author_keys = {
            str(author.get("works_url") or author.get("url") or author.get("name"))
            for author in merged_authors
            if isinstance(author, dict)
        }
        preview_count = 0
        for candidate in candidates:
            row = row_by_id.get(str(candidate.get("id")))
            if row is None:
                enriched_candidates.append(candidate)
                continue
            normalized = dict(row)
            for field in ("title", "title_jpn", "uploader"):
                if normalized.get(field) is not None:
                    normalized[field] = html.unescape(str(normalized[field]))
            if isinstance(normalized.get("tags"), list):
                normalized["tags"] = [
                    html.unescape(str(value)) for value in normalized["tags"]
                ]
            enriched, authors = _exhentai_directory_candidate(
                normalized,
                str(candidate.get("url") or result.get("search_url") or ""),
            )
            if enriched is None:
                enriched_candidates.append(candidate)
                continue
            candidate_metadata = (
                candidate.get("metadata")
                if isinstance(candidate.get("metadata"), dict)
                else {}
            )
            enriched["metadata"]["gallery_token"] = candidate_metadata.get(
                "gallery_token"
            )
            enriched_candidates.append(enriched)
            preview_count += 1
            for author in authors:
                key = str(
                    author.get("works_url") or author.get("url") or author.get("name")
                )
                if key not in author_keys:
                    author_keys.add(key)
                    merged_authors.append(author)

        result["candidates"] = enriched_candidates
        result["authors"] = merged_authors
        result["candidate_count"] = len(enriched_candidates)
        result["author_count"] = len(merged_authors)
        result["preview_count"] = preview_count
        result["preview_missing_count"] = len(enriched_candidates) - preview_count
        return result

    async def search_danbooru_artists(
        self,
        *,
        keyword: str,
        limit: int,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
        credentials_ref: str | None,
        cookies_file: str | None,
        config_file: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        parts = [part for part in re.split(r"\s+", keyword.strip()) if part]
        # Leading and trailing wildcards make Danbooru's artist directory scan
        # the entire table and currently trigger a database timeout.  Keeping
        # only inter-word wildcards retains multi-word matching without that
        # pathological query.
        pattern = "*".join(parts)
        url = "https://danbooru.donmai.us/artists?" + urlencode(
            {"search[any_name_matches]": pattern}
        )
        try:
            result = await self.discover_url(
                site="danbooru",
                url=url,
                keyword=keyword,
                limit=limit,
                range_kind="child",
                policy=policy,
                proxy_mode=proxy_mode,
                credentials_ref=credentials_ref,
                cookies_file=cookies_file,
                config_file=config_file,
                extra_args=[],
                timeout_seconds=timeout_seconds,
            )
        except DiscoveryError:
            return await self._search_danbooru_artists_api(
                keyword=keyword,
                limit=limit,
                policy=policy,
                proxy_mode=proxy_mode,
            )
        if not result.get("author_count"):
            return await self._search_danbooru_artists_api(
                keyword=keyword,
                limit=limit,
                policy=policy,
                proxy_mode=proxy_mode,
            )
        return result

    async def danbooru_artist_profiles(
        self,
        names: list[str],
        *,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Read Danbooru's manually maintained artist URLs for matched artist tags."""
        unique = list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))[:limit]
        semaphore = asyncio.Semaphore(4)

        async def fetch(name: str):
            async with semaphore:
                try:
                    return await self._danbooru_artist_profile(
                        name,
                        policy=policy,
                        proxy_mode=proxy_mode,
                    )
                except Exception as exc:
                    return {"_error": redact_text(exc, limit=1000), "name": name}

        raw = await asyncio.gather(*(fetch(name) for name in unique))
        profiles: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for name, item in zip(unique, raw):
            if item is None:
                continue
            if item.get("_error"):
                errors.append({"artist": name, "message": str(item["_error"])})
            else:
                profiles.append(item)
        return profiles, errors

    async def _danbooru_artist_profile(
        self,
        name: str,
        *,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
    ) -> dict[str, Any] | None:
        mode: ProxyMode = proxy_mode or policy.proxy_mode
        attempts = max(1, policy.retry_limit + 1)
        tried: set[str] = set()
        last_error = "Danbooru 画师资料读取失败"
        preferred_tags = self._danbooru_node_tags(policy)

        for attempt in range(1, attempts + 1):
            operation_id = f"dan-artist-{uuid.uuid4().hex}"
            lease: ProxyLease | None = None
            proxy_fault = False
            try:
                if mode != "direct":
                    try:
                        lease = await asyncio.to_thread(
                            self.proxy.acquire,
                            operation_id,
                            node_tags=preferred_tags,
                            exclude_ids=tried,
                            allowed_ids=getattr(policy, "allowed_proxy_ids", None),
                            probe_before_use=policy.probe_before_use,
                            probe_url=policy.probe_url,
                        )
                        if (
                            lease is None
                            and preferred_tags
                            and not policy.node_tags
                        ):
                            lease = await asyncio.to_thread(
                                self.proxy.acquire,
                                operation_id,
                                node_tags=[],
                                exclude_ids=tried,
                                allowed_ids=getattr(policy, "allowed_proxy_ids", None),
                                probe_before_use=policy.probe_before_use,
                                probe_url=policy.probe_url,
                            )
                    except Exception as exc:
                        if mode == "required":
                            raise DiscoveryError("proxy_unavailable", redact_text(exc, limit=1000)) from exc
                    if lease is None and mode == "required":
                        raise DiscoveryError(
                            "proxy_unavailable",
                            "当前没有符合 Danbooru 资料查询策略的健康代理节点",
                        )

                def request_profile() -> dict[str, Any] | None:
                    session = requests.Session()
                    session.headers["User-Agent"] = "gallery-dl-backend/0.3"
                    proxies = {"http": lease.endpoint, "https": lease.endpoint} if lease else None

                    def request_json(url: str, params: dict[str, Any]) -> Any:
                        response = session.get(
                            url,
                            params=params,
                            proxies=proxies,
                            timeout=policy.http_timeout,
                            allow_redirects=False,
                        )
                        status_code = int(getattr(response, "status_code", 200))
                        if 300 <= status_code < 400:
                            raise ValueError("Danbooru API 返回了未接受的重定向")
                        body = str(getattr(response, "text", "") or "")
                        if _is_cloudflare_challenge(status_code, body):
                            return _danbooru_json_request(
                                url,
                                params=params,
                                proxy_url=lease.endpoint if lease else None,
                                timeout=policy.http_timeout,
                            )
                        response.raise_for_status()
                        return response.json()

                    payload = request_json(
                        "https://danbooru.donmai.us/artists.json",
                        {"search[name]": name, "limit": 20},
                    )
                    if not isinstance(payload, list) or not payload:
                        return None
                    exact = next(
                        (
                            item
                            for item in payload
                            if _tag_key(item.get("name", "")) == _tag_key(name)
                        ),
                        None,
                    )
                    if exact is None:
                        return None
                    artist_id = int(exact["id"])
                    url_payload = request_json(
                        "https://danbooru.donmai.us/artist_urls.json",
                        {"search[artist_id]": artist_id, "limit": 100},
                    )
                    related: list[dict[str, Any]] = []
                    if isinstance(url_payload, list):
                        for row in url_payload:
                            if not isinstance(row, dict) or not row.get("url"):
                                continue
                            classified = classify_external_profile(str(row["url"]))
                            classified.update(
                                {
                                    "id": row.get("id"),
                                    "active": bool(row.get("is_active", True)),
                                }
                            )
                            related.append(classified)
                    return {
                        "id": str(artist_id),
                        "name": str(exact.get("name") or name),
                        "other_names": [str(value) for value in exact.get("other_names") or []],
                        "group_name": exact.get("group_name"),
                        "profile_url": f"https://danbooru.donmai.us/artists/{artist_id}",
                        "works_url": "https://danbooru.donmai.us/posts?" + urlencode({"tags": exact.get("name") or name}),
                        "related_profiles": related,
                        "proxy": {
                            "mode": mode,
                            "used": lease is not None,
                            "node_id": lease.node_id if lease else None,
                        },
                    }

                return await asyncio.to_thread(request_profile)
            except DiscoveryError:
                raise
            except Exception as exc:
                last_error = redact_text(exc, limit=1000)
                decision = classify_result(1, last_error)
                response = getattr(exc, "response", None)
                status_code = _integer(getattr(response, "status_code", None))
                response_body = str(getattr(response, "text", "") or "")
                challenge = _is_cloudflare_challenge(status_code, response_body)
                transient_request = isinstance(
                    exc,
                    (requests.Timeout, requests.ConnectionError),
                ) or bool(
                    status_code
                    and (status_code in {408, 425, 429} or status_code >= 500)
                )
                if isinstance(exc, _DanbooruApiRequestError):
                    transient_request = exc.retryable
                    challenge = exc.proxy_fault
                proxy_fault = bool(
                    decision.proxy_fault
                    or (lease is not None and (challenge or transient_request))
                )
                if lease is not None and proxy_fault:
                    tried.add(lease.node_id)
                if not decision.retryable and not proxy_fault and not transient_request:
                    break
            finally:
                if lease is not None:
                    try:
                        await asyncio.to_thread(
                            self.proxy.release,
                            operation_id,
                            proxy_fault=proxy_fault,
                            reason=last_error if proxy_fault else "",
                        )
                    except Exception:
                        pass
            if attempt < attempts and policy.backoff_base_seconds:
                await asyncio.sleep(min(policy.backoff_base_seconds * (2 ** (attempt - 1)), 10.0))

        raise DiscoveryError(
            "danbooru_artist_lookup_failed",
            last_error,
            details={"artist": name, "attempts": attempts},
        )

    async def discover_url(
        self,
        *,
        site: str,
        url: str,
        keyword: str | None,
        limit: int,
        range_kind: str | None,
        policy: SitePolicy,
        proxy_mode: ProxyMode | None,
        credentials_ref: str | None,
        cookies_file: str | None,
        config_file: str | None,
        extra_args: list[str],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        spec = search_site(site)
        values = validate_discovery_args(extra_args)
        if spec.site == "pixiv":
            values = _pixiv_first_media_args(values)
        if range_kind is None:
            if spec.site == "exhentai" and re.search(r"/(?:g|s|mpv)/", url):
                range_kind = "file"
                limit = 1
            else:
                range_kind = spec.search_range
        range_option = {
            "file": "--range",
            "post": "--post-range",
            "child": "--child-range",
        }[range_kind]
        protocol_args = ["--dump-json", range_option, f"1-{limit}", *values]
        mode: ProxyMode = proxy_mode or policy.proxy_mode
        attempts = max(1, policy.retry_limit + 1)
        tried: set[str] = set()
        last_message = "搜索任务没有返回结果"
        last_auth_context = ""

        for attempt in range(1, attempts + 1):
            operation_id = f"discover-{uuid.uuid4().hex}"
            operation_dir = self.runtime_dir / operation_id
            lease: ProxyLease | None = None
            decision = FailureDecision("backend_error", False, False, last_message)
            try:
                if mode != "direct":
                    try:
                        lease = await asyncio.to_thread(
                            self.proxy.acquire,
                            operation_id,
                            node_tags=policy.node_tags,
                            exclude_ids=tried,
                            allowed_ids=getattr(policy, "allowed_proxy_ids", None),
                            probe_before_use=policy.probe_before_use,
                            probe_url=policy.probe_url,
                        )
                    except Exception as exc:
                        if mode == "required":
                            raise DiscoveryError(
                                "proxy_unavailable",
                                redact_text(exc, limit=1000),
                            ) from exc
                    if lease is None and mode == "required":
                        raise DiscoveryError(
                            "proxy_unavailable",
                            "当前没有符合站点策略的健康代理节点",
                        )

                result = await self.gallery.capture(
                    operation_id,
                    url=url,
                    output_dir=str(operation_dir),
                    proxy_url=lease.endpoint if lease else None,
                    http_timeout=policy.http_timeout,
                    gallery_retries=policy.gallery_retries,
                    task_timeout=timeout_seconds,
                    cookies_file=cookies_file,
                    config_file=config_file,
                    credentials_ref=credentials_ref,
                    extra_args=protocol_args,
                )
                decision = classify_result(
                    result.exit_code,
                    result.stderr,
                    cancelled=False,
                    timed_out=result.timed_out,
                )
                last_auth_context = result.stderr
                if decision.error_class == "success":
                    try:
                        candidates, authors = parse_discovery_output(
                            spec.site,
                            result.stdout,
                            source_url=url,
                            limit=limit,
                        )
                    except DiscoveryError as exc:
                        decision = classify_result(1, f"{result.stderr}\n{exc.message}")
                        last_message = exc.message
                        if not decision.retryable:
                            raise
                    else:
                        proxy_info = {
                            "mode": mode,
                            "used": lease is not None,
                            "node_id": lease.node_id if lease else None,
                            "node_name": lease.name if lease else None,
                            "protocol": lease.protocol if lease else None,
                        }
                        return {
                            "site": spec.site,
                            "keyword": keyword,
                            "search_url": url,
                            "candidate_count": len(candidates),
                            "author_count": len(authors),
                            "candidates": candidates,
                            "authors": authors,
                            "proxy": proxy_info,
                            "attempts": attempt,
                        }
                else:
                    last_message = decision.message
            except DiscoveryError:
                raise
            except (FileNotFoundError, ValueError) as exc:
                raise DiscoveryError("discovery_configuration", redact_text(exc, limit=1000)) from exc
            except Exception as exc:
                last_message = redact_text(exc, limit=1000)
                decision = classify_result(1, last_message)
            finally:
                if lease is not None:
                    if decision.proxy_fault:
                        tried.add(lease.node_id)
                    try:
                        await asyncio.to_thread(
                            self.proxy.release,
                            operation_id,
                            proxy_fault=decision.proxy_fault,
                            reason=decision.message,
                        )
                    except Exception:
                        pass
                if operation_dir.parent == self.runtime_dir and operation_dir.name.startswith("discover-"):
                    shutil.rmtree(operation_dir, ignore_errors=True)

            if not decision.retryable or attempt >= attempts:
                break
            delay = policy.backoff_base_seconds * (2 ** max(0, attempt - 1))
            if delay:
                await asyncio.sleep(min(delay, 10.0))

        if decision.error_class == "authentication" and self.auth_failure_callback is not None:
            try:
                await self.auth_failure_callback(
                    spec.site,
                    cookies_file,
                    last_auth_context or last_message,
                )
            except Exception:
                pass
        raise DiscoveryError(
            "authentication" if decision.error_class == "authentication" else "discovery_failed",
            redact_text(last_message, limit=1000),
            details={"attempts": attempts},
        )
