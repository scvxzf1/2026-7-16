from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from .crawl import CrawlPlanError, CrawlPlanner, CrawlUnit
from .database import Database
from .discovery import DiscoveryError, DiscoveryService
from .proxy import ProxyPoolAdapter
from .redaction import redact_text
from .scheduler import TaskScheduler
from .schemas import SitePolicy, TaskCreate, TaskPolicy
from .source_keys import candidate_source_key


EnqueueTask = Callable[
    [TaskCreate, str, int],
    Awaitable[tuple[dict, bool]],
]
PolicyProvider = Callable[[str], SitePolicy]
_TWITTER_MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com"}


def _is_twitter_media_url(site: str, url: str) -> bool:
    return site == "twitter" and (urlsplit(url).hostname or "").lower() in _TWITTER_MEDIA_HOSTS


class OrderedCrawlManager:
    """Run one selected address at a time and fan that address out to media tasks."""

    def __init__(
        self,
        database: Database,
        discovery: DiscoveryService,
        planner: CrawlPlanner,
        scheduler: TaskScheduler,
        proxy: ProxyPoolAdapter,
        policy_for: PolicyProvider,
        *,
        poll_interval: float = 0.25,
    ) -> None:
        self.db = database
        self.discovery = discovery
        self.planner = planner
        self.scheduler = scheduler
        self.proxy = proxy
        self.policy_for = policy_for
        self.poll_interval = max(0.05, float(poll_interval))
        self._enqueue: EnqueueTask | None = None
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    def set_enqueue(self, callback: EnqueueTask) -> None:
        self._enqueue = callback

    def notify(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        if self._enqueue is None:
            raise RuntimeError("顺序爬取管理器尚未绑定任务入队器")
        self.db.recover_ordered_crawls()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name="ordered-crawl-manager")
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    def status(self) -> dict:
        return {
            "running": self._loop_task is not None and not self._stopping,
            "active_batches": len(self.db.active_crawl_batch_ids()),
            "execution_order": "source_then_address",
            "address_parallelism": "media_tasks",
        }

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                self._wake.clear()
                await self.run_once()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(self.poll_interval)

    async def run_once(self) -> None:
        for batch_id in self.db.active_crawl_batch_ids():
            await self._tick_batch(batch_id)

    async def _tick_batch(self, batch_id: str) -> None:
        batch = self.db.get_crawl_batch(batch_id)
        if batch is None:
            return
        address = self.db.next_crawl_address(batch_id)
        if batch["cancel_requested"]:
            if address and address["status"] == "running":
                for task in self.db.crawl_address_tasks(address["id"]):
                    if task["status"] not in {"succeeded", "failed", "cancelled"}:
                        await self.scheduler.cancel(task["id"])
                self.db.finish_crawl_address_if_terminal(address["id"])
            self.db.finish_crawl_batch_if_ready(batch_id)
            return
        if address is None:
            self.db.finish_crawl_batch_if_ready(batch_id)
            return
        if address["status"] == "pending":
            await self._activate_address(batch, address)
            return
        if address["status"] == "running":
            if self.db.finish_crawl_address_if_terminal(address["id"]):
                self.db.finish_crawl_batch_if_ready(batch_id)

    async def _activate_address(self, batch: dict, address: dict) -> None:
        if self._enqueue is None:
            raise RuntimeError("顺序爬取管理器尚未绑定任务入队器")
        if not self.db.begin_crawl_address_planning(address["id"]):
            return
        linked_tasks: list[str] = []

        async def cancel_linked() -> None:
            await asyncio.gather(
                *(
                    self.scheduler.cancel(task_id)
                    for task_id in dict.fromkeys(linked_tasks)
                ),
                return_exceptions=True,
            )

        try:
            remaining = int(batch["max_tasks"]) - self.db.crawl_batch_task_count(batch["id"])
            if remaining <= 0:
                raise CrawlPlanError(
                    "crawl_plan_too_large",
                    f"批次媒体任务达到 max_tasks={batch['max_tasks']}",
                )
            policy = await self._probe_address_policy(
                address,
                self.policy_for(address["site"]),
            )
            latest = self.db.get_crawl_batch(batch["id"])
            if latest is None or latest["cancel_requested"]:
                return
            units, skipped_count = await self._plan_address(
                address,
                batch_id=batch["id"],
                policy=policy,
                max_tasks=remaining,
            )
            self.db.set_crawl_address_pre_dedup_skipped_count(
                address["id"],
                skipped_count,
            )
            deduplicated = self._deduplicate(units)
            if not deduplicated:
                if skipped_count and self.db.finish_crawl_address_as_pre_deduplicated(
                    address["id"],
                    skipped_count,
                ):
                    self.db.finish_crawl_batch_if_ready(batch["id"])
                    self.notify()
                    return
                raise CrawlPlanError("empty_crawl_plan", "该地址没有发现可下载图片")
            if len(deduplicated) > remaining:
                raise CrawlPlanError(
                    "crawl_plan_too_large",
                    f"该地址媒体数超过批次剩余额度 {remaining}",
                )

            latest = self.db.get_crawl_batch(batch["id"])
            if latest is None or latest["cancel_requested"]:
                return
            address_output = (
                Path(batch["output_dir"])
                / f"{int(address['source_order']):02d}-{address['site']}"
                / f"{int(address['address_order']):04d}"
            )
            for sequence_no, (unit, digest) in enumerate(deduplicated, start=1):
                latest = self.db.get_crawl_batch(batch["id"])
                if latest is None or latest["cancel_requested"]:
                    await cancel_linked()
                    return
                unit_site = unit.site or address["site"]
                direct_twitter_media = _is_twitter_media_url(unit_site, unit.url)
                task_body = TaskCreate(
                    url=unit.url,
                    site=unit_site,
                    output_dir=str(address_output),
                    proxy_mode=address["proxy_mode"],
                    max_attempts=address["max_attempts"],
                    priority=address["priority"],
                    credentials_ref=address.get("credentials_ref"),
                    cookies_file=None if direct_twitter_media else address.get("cookies_file"),
                    config_file=address.get("config_file"),
                    eh_download=(address.get("download_options") or {}).get("eh"),
                    extra_args=[*address.get("extra_args", []), *unit.extra_args],
                )
                task_body._policy_override = policy
                task_body._skip_managed_credentials = direct_twitter_media
                key = f"crawl:{batch['id']}:{address['id']}:{digest[:48]}"
                task, _created = await self._enqueue(
                    task_body,
                    key,
                    int(batch["concurrency"]),
                )
                linked_tasks.append(task["id"])
                self.db.link_crawl_task(
                    address["id"],
                    task["id"],
                    sequence_no,
                    source_key=unit.source_key,
                    source_url=unit.source_url,
                )
            latest = self.db.get_crawl_batch(batch["id"])
            if latest is None or latest["cancel_requested"]:
                await cancel_linked()
                return
            if not self.db.mark_crawl_address_running(address["id"]):
                latest = self.db.get_crawl_batch(batch["id"])
                if latest is not None and not latest["cancel_requested"]:
                    raise RuntimeError("媒体任务已创建，但地址状态切换失败")
            self.scheduler.notify()
        except asyncio.CancelledError:
            await asyncio.shield(cancel_linked())
            error = "顺序管理器停止，已创建的媒体任务已取消"
            if linked_tasks and self.db.mark_crawl_address_running(
                address["id"],
                last_error=error,
            ):
                # Keep linked tasks attached to this address. On the next start the
                # scheduler recovers them and strict sequencing drains this address
                # before another one is planned.
                self.scheduler.notify()
            else:
                self.db.reset_crawl_address_planning(
                    address["id"],
                    "顺序管理器停止，地址等待重新规划",
                )
            raise
        except Exception as exc:
            await cancel_linked()
            latest = self.db.get_crawl_batch(batch["id"])
            if latest is not None and not latest["cancel_requested"]:
                error = redact_text(exc, limit=2000)
                if linked_tasks and self.db.mark_crawl_address_running(
                    address["id"],
                    last_error=error,
                ):
                    # Keep the address active until every partially-created task reaches
                    # a terminal state. This preserves strict address sequencing.
                    self.scheduler.notify()
                    return
                self.db.fail_crawl_address(address["id"], error)
                self.db.finish_crawl_batch_if_ready(batch["id"])

    @staticmethod
    def _probe_target(address: dict, policy: SitePolicy) -> str:
        if policy.probe_url:
            return policy.probe_url
        parsed = urlsplit(str(address["url"]))
        host = parsed.hostname
        if not host:
            raise CrawlPlanError("invalid_probe_target", "图站地址缺少可探活的主机名")
        authority = f"[{host}]" if ":" in host else host
        if parsed.port and parsed.port != 443:
            authority = f"{authority}:{parsed.port}"
        return f"https://{authority}/"

    async def _probe_address_policy(
        self,
        address: dict,
        policy: SitePolicy,
    ) -> TaskPolicy:
        scoped = TaskPolicy.model_validate(policy.model_dump())
        if address["proxy_mode"] == "direct":
            return scoped

        target = self._probe_target(address, policy)
        try:
            result = await asyncio.to_thread(self.proxy.probe, target_url=target)
            healthy_node_ids = sorted(
                {
                    str(item["id"])
                    for item in result.get("results", [])
                    if item.get("healthy") and item.get("id")
                }
            )
            self.db.save_crawl_address_proxy_probe(
                address["id"],
                target_url=target,
                total_count=int(result.get("total") or 0),
                healthy_node_ids=healthy_node_ids,
            )
        except Exception as exc:
            error = redact_text(exc, limit=1000)
            healthy_node_ids = []
            self.db.save_crawl_address_proxy_probe(
                address["id"],
                target_url=target,
                total_count=0,
                healthy_node_ids=[],
                error=error,
            )
            if address["proxy_mode"] == "required":
                raise CrawlPlanError(
                    "proxy_probe_failed",
                    f"{address['site']} 代理探活失败: {error}",
                ) from exc

        if address["proxy_mode"] == "required" and not healthy_node_ids:
            raise CrawlPlanError(
                "proxy_unavailable",
                f"{address['site']} 图站探活后没有可用代理节点",
            )
        return scoped.model_copy(
            update={
                "probe_url": target,
                "proxy_probe_scope": address["id"],
                "allowed_proxy_ids": healthy_node_ids,
            }
        )

    async def _plan_address(
        self,
        address: dict,
        *,
        batch_id: str,
        policy: SitePolicy,
        max_tasks: int,
    ) -> tuple[list[CrawlUnit], int]:
        site = str(address["site"])
        mode = address["proxy_mode"]
        if site == "exhentai":
            candidates = [
                {
                    "id": address["id"],
                    "site": site,
                    "kind": "gallery",
                    "url": address["url"],
                }
            ]
        else:
            dedup_scan_allowance = self.db.succeeded_danbooru_source_key_count(
                batch_id,
                site,
            )
            result = await self.discovery.discover_url(
                site=site,
                url=address["url"],
                keyword=None,
                # Ask for one extra post/work so the task ceiling becomes an explicit
                # error instead of a silently truncated account or tag crawl.
                limit=max_tasks + dedup_scan_allowance + 1,
                range_kind=None,
                policy=policy,
                proxy_mode=mode,
                credentials_ref=address.get("credentials_ref"),
                cookies_file=address.get("cookies_file"),
                config_file=address.get("config_file"),
                extra_args=address.get("discovery_args", []),
                timeout_seconds=float(address.get("timeout_seconds") or 180.0),
            )
            candidates = result.get("candidates") or []
            if not candidates:
                raise DiscoveryError(
                    "address_empty",
                    f"该地址没有发现作品: {address['url']}",
                )
        candidates, skipped_count = self._filter_previously_downloaded_danbooru_sources(
            batch_id,
            site,
            candidates,
        )
        units, _planner_proxies = await self.planner.plan_media(
            candidates,
            policy=policy,
            proxy_mode=mode,
            cookies_file=address.get("cookies_file"),
            max_tasks=max_tasks,
        )
        return units, skipped_count

    def _filter_previously_downloaded_danbooru_sources(
        self,
        batch_id: str,
        site: str,
        candidates: list[dict],
    ) -> tuple[list[dict], int]:
        normalized_site = "twitter" if site == "x" else site
        if normalized_site not in {"pixiv", "twitter"}:
            return candidates, 0
        keyed = [
            (
                item,
                candidate_source_key(
                    normalized_site,
                    item.get("id"),
                    item.get("download_url") or item.get("works_url") or item.get("url"),
                ),
            )
            for item in candidates
        ]
        matched = self.db.succeeded_danbooru_source_keys(
            batch_id,
            {key for _item, key in keyed if key},
        )
        if not matched:
            return candidates, 0
        kept: list[dict] = []
        skipped_count = 0
        for item, key in keyed:
            if key not in matched:
                kept.append(item)
                continue
            try:
                skipped_count += max(1, int(item.get("media_count") or 1))
            except (TypeError, ValueError):
                skipped_count += 1
        return kept, skipped_count

    @staticmethod
    def _deduplicate(units: list[CrawlUnit]) -> list[tuple[CrawlUnit, str]]:
        result: list[tuple[CrawlUnit, str]] = []
        seen: set[str] = set()
        for unit in units:
            material = json.dumps(
                [unit.site, unit.url, unit.extra_args],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if digest not in seen:
                seen.add(digest)
                result.append((unit, digest))
        return result
