from __future__ import annotations

import asyncio
import html
import http.cookiejar
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

import requests

from .classifier import classify_result
from .proxy import ProxyLease, ProxyPoolAdapter
from .redaction import redact_text
from .schemas import ProxyMode, SitePolicy


_EH_GALLERY_RE = re.compile(
    r"https?://(?P<host>(?:e-|ex)hentai\.org)/g/(?P<gid>\d+)/(?P<token>[0-9a-f]{10})/?",
    re.I,
)
_EH_IMAGE_RE = re.compile(
    r"https?://(?:e-|ex)hentai\.org/s/[0-9a-f]{10}/(?P<gid>\d+)-(?P<num>\d+)",
    re.I,
)


class CrawlPlanError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(slots=True)
class CrawlUnit:
    url: str
    site: str
    kind: str
    source_id: str
    extra_args: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "site": self.site,
            "kind": self.kind,
            "source_id": self.source_id,
            "extra_args": list(self.extra_args),
        }


def _load_cookies(session: requests.Session, cookies_file: str | None) -> None:
    if not cookies_file:
        return
    jar = http.cookiejar.MozillaCookieJar(cookies_file)
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise CrawlPlanError("cookies_error", f"读取 Cookie 文件失败: {exc}") from exc
    session.cookies.update(jar)


def _parse_eh_index(page: str, gallery_url: str, gid: int) -> tuple[str, int, dict[int, str]]:
    title_match = re.search(r'<h1\s+id="gn">(.*?)</h1>', page, re.I | re.S)
    length_match = re.search(
        r">Length:</td><td[^>]*>\s*(\d+)\s+(?:pages?|files?)",
        page,
        re.I,
    )
    title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))).strip() if title_match else ""
    total = int(length_match.group(1)) if length_match else 0
    links: dict[int, str] = {}
    for raw in re.findall(r'href=["\']([^"\']+/s/[0-9a-f]{10}/\d+-\d+)["\']', page, re.I):
        url = urljoin(gallery_url, html.unescape(raw))
        match = _EH_IMAGE_RE.match(url)
        if match and int(match.group("gid")) == gid:
            links[int(match.group("num"))] = url
    return title, total, links


class CrawlPlanner:
    def __init__(
        self,
        proxy: ProxyPoolAdapter,
        *,
        auth_failure_callback: Callable[[str, str | None, str], Awaitable[bool]] | None = None,
    ) -> None:
        self.proxy = proxy
        self.auth_failure_callback = auth_failure_callback

    async def plan_media(
        self,
        items: list[dict[str, Any]],
        *,
        policy: SitePolicy,
        proxy_mode: ProxyMode,
        cookies_file: str | None,
        max_tasks: int,
    ) -> tuple[list[CrawlUnit], list[dict[str, Any]]]:
        units: list[CrawlUnit] = []
        planner_proxies: list[dict[str, Any]] = []
        for item in items:
            site = str(item.get("site") or "").lower()
            url = str(item.get("download_url") or item.get("works_url") or item.get("url") or "").strip()
            source_id = str(item.get("id") or uuid.uuid5(uuid.NAMESPACE_URL, url))
            kind = str(item.get("kind") or "candidate")
            item_args = [str(value) for value in item.get("extra_args") or []]
            if not url:
                raise CrawlPlanError("invalid_crawl_item", "爬取候选缺少 URL")

            if site == "exhentai" and _EH_GALLERY_RE.match(url):
                remaining = max_tasks - len(units)
                if remaining <= 0:
                    raise CrawlPlanError(
                        "crawl_plan_too_large",
                        f"规划任务数超过 max_tasks={max_tasks}",
                    )
                gallery_units, proxy_info = await self._plan_eh_gallery(
                    url,
                    policy=policy,
                    proxy_mode=proxy_mode,
                    cookies_file=cookies_file,
                    max_tasks=remaining,
                )
                for unit in gallery_units:
                    unit.extra_args.extend(item_args)
                units.extend(gallery_units)
                if proxy_info:
                    planner_proxies.append(proxy_info)
            else:
                count = max(1, int(item.get("media_count") or 1))
                if count == 1:
                    units.append(CrawlUnit(url, site, kind, source_id, item_args))
                else:
                    for index in range(1, count + 1):
                        units.append(
                            CrawlUnit(
                                url,
                                site,
                                "media",
                                f"{source_id}:{index}",
                                [*item_args, "--range", str(index)],
                            )
                        )
            if len(units) > max_tasks:
                raise CrawlPlanError(
                    "crawl_plan_too_large",
                    f"规划任务数超过 max_tasks={max_tasks}",
                    details={"planned": len(units), "max_tasks": max_tasks},
                )
        return units, planner_proxies

    async def _plan_eh_gallery(
        self,
        gallery_url: str,
        *,
        policy: SitePolicy,
        proxy_mode: ProxyMode,
        cookies_file: str | None,
        max_tasks: int,
    ) -> tuple[list[CrawlUnit], dict[str, Any] | None]:
        match = _EH_GALLERY_RE.match(gallery_url)
        if not match:
            raise CrawlPlanError("invalid_eh_gallery", "EH 画廊 URL 格式无效")
        normalized = gallery_url if gallery_url.endswith("/") else gallery_url + "/"
        gid = int(match.group("gid"))
        attempts = max(1, policy.retry_limit + 1)
        tried: set[str] = set()
        last_error = "EH 画廊索引读取失败"
        decision = classify_result(1, last_error)

        for attempt in range(1, attempts + 1):
            lease_id = f"eh-plan-{uuid.uuid4().hex}"
            lease: ProxyLease | None = None
            proxy_fault = False
            try:
                if proxy_mode != "direct":
                    try:
                        lease = await asyncio.to_thread(
                            self.proxy.acquire,
                            lease_id,
                            node_tags=policy.node_tags,
                            exclude_ids=tried,
                            probe_before_use=policy.probe_before_use,
                            probe_url=policy.probe_url,
                        )
                    except Exception as exc:
                        if proxy_mode == "required":
                            raise CrawlPlanError(
                                "proxy_unavailable",
                                redact_text(exc, limit=1000),
                            ) from exc
                        lease = None
                    if lease is None and proxy_mode == "required":
                        raise CrawlPlanError(
                            "proxy_unavailable",
                            "当前没有可用于 EH 索引规划的健康代理节点",
                        )

                def collect() -> tuple[str, int, dict[int, str]]:
                    session = requests.Session()
                    session.headers["User-Agent"] = "gallery-dl-backend/crawl-planner"
                    session.cookies.set("nw", "1", domain=".e-hentai.org")
                    session.cookies.set("nw", "1", domain=".exhentai.org")
                    _load_cookies(session, cookies_file)
                    proxies = {"http": lease.endpoint, "https": lease.endpoint} if lease else None
                    title = ""
                    expected = 0
                    links: dict[int, str] = {}
                    page_index = 0
                    empty_pages = 0
                    while page_index < 1000:
                        page_url = normalized if page_index == 0 else f"{normalized}?p={page_index}"
                        response = session.get(
                            page_url,
                            proxies=proxies,
                            timeout=policy.http_timeout,
                            allow_redirects=False,
                        )
                        if 300 <= response.status_code < 400:
                            raise ValueError("AuthenticationError: EH 登录状态失效，画廊索引发生重定向")
                        response.raise_for_status()
                        page_title, page_total, page_links = _parse_eh_index(
                            response.text,
                            normalized,
                            gid,
                        )
                        if page_index == 0:
                            title = page_title
                            expected = page_total
                            if not title or expected <= 0:
                                raise ValueError("EH 画廊首页缺少标题或图片总数")
                            if expected > max_tasks:
                                raise CrawlPlanError(
                                    "crawl_plan_too_large",
                                    f"EH 画廊图片数 {expected} 超过剩余 max_tasks={max_tasks}",
                                    details={"gallery_id": str(gid), "planned": expected, "max_tasks": max_tasks},
                                )
                        before = len(links)
                        links.update(page_links)
                        empty_pages = empty_pages + 1 if len(links) == before else 0
                        if expected and len(links) >= expected:
                            break
                        if empty_pages >= 2:
                            raise ValueError(
                                f"EH 画廊索引提前结束: collected={len(links)}, expected={expected}"
                            )
                        page_index += 1
                    missing = sorted(set(range(1, expected + 1)).difference(links))
                    if missing:
                        raise ValueError(f"EH 画廊索引缺少 {len(missing)} 页，首个缺页={missing[0]}")
                    return title, expected, links

                title, expected, links = await asyncio.to_thread(collect)
                units = [
                    CrawlUnit(
                        links[index],
                        "exhentai",
                        "media",
                        f"{gid}:{index}",
                        ["--range", "1"],
                    )
                    for index in range(1, expected + 1)
                ]
                proxy_info = (
                    {
                        "gallery_id": str(gid),
                        "title": title,
                        "node_id": lease.node_id,
                        "node_name": lease.name,
                        "protocol": lease.protocol,
                    }
                    if lease
                    else {"gallery_id": str(gid), "title": title, "node_id": None}
                )
                return units, proxy_info
            except CrawlPlanError:
                raise
            except Exception as exc:
                last_error = redact_text(exc, limit=1000)
                decision = classify_result(1, last_error)
                proxy_fault = decision.proxy_fault
                if lease is not None and proxy_fault:
                    tried.add(lease.node_id)
                if not decision.retryable and not proxy_fault:
                    break
            finally:
                if lease is not None:
                    try:
                        await asyncio.to_thread(
                            self.proxy.release,
                            lease_id,
                            proxy_fault=proxy_fault,
                            reason=last_error if proxy_fault else "",
                        )
                    except Exception:
                        pass
            if attempt < attempts and policy.backoff_base_seconds:
                await asyncio.sleep(min(policy.backoff_base_seconds * (2 ** (attempt - 1)), 10.0))

        if decision.error_class == "authentication" and self.auth_failure_callback is not None:
            try:
                await self.auth_failure_callback("exhentai", cookies_file, last_error)
            except Exception:
                pass
        raise CrawlPlanError(
            "authentication" if decision.error_class == "authentication" else "eh_gallery_plan_failed",
            last_error,
            details={"gallery_id": str(gid), "attempts": attempts},
        )
