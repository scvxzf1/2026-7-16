from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from .config import ProxySettings
from .proxy_core import TunnelTransportCore
from .proxy_runtime import LocalHTTPForwarder, NativeProxyPool, mask_proxy
from .proxy_sources import ParsedProxyNode, fetch_subscriptions, parse_subscription_text
from .redaction import redact_text


class ProxyPoolError(RuntimeError):
    pass


class ProxyPoolConflict(ProxyPoolError):
    pass


@dataclass(slots=True)
class ProxyLease:
    task_id: str
    node_id: str
    endpoint: str
    name: str
    protocol: str
    tags: list[str]
    acquired_at: float
    pool_endpoint: str = field(default="", repr=False)
    pool_token: str = field(default="", repr=False)
    forwarder_key: str = field(default="", repr=False)


@dataclass(slots=True)
class _NodeRecord:
    id: str
    name: str
    protocol: str
    endpoint: str = field(repr=False)
    tags: list[str] = field(default_factory=list)
    healthy: bool = False
    success_count: int = 0
    fail_count: int = 0
    last_latency_ms: float | None = None
    cooldown_until: float = 0.0
    last_error: str = ""


_REGIONS: dict[str, tuple[str, ...]] = {
    "jp": ("jp", "japan", "日本", "东京", "大阪", "🇯🇵"),
    "hk": ("hk", "hong kong", "香港", "🇭🇰"),
    "tw": ("tw", "taiwan", "台湾", "台北", "🇹🇼"),
    "sg": ("sg", "singapore", "新加坡", "狮城", "🇸🇬"),
    "us": ("us", "usa", "united states", "美国", "洛杉矶", "圣何塞", "🇺🇸"),
    "kr": ("kr", "korea", "韩国", "首尔", "🇰🇷"),
    "de": ("de", "germany", "德国", "🇩🇪"),
    "gb": ("gb", "uk", "united kingdom", "英国", "🇬🇧"),
    "fr": ("fr", "france", "法国", "🇫🇷"),
    "ca": ("ca", "canada", "加拿大", "🇨🇦"),
    "au": ("au", "australia", "澳大利亚", "澳洲", "🇦🇺"),
}


class ProxyPoolAdapter:
    """Self-contained subscription proxy pool used by the backend."""

    def __init__(self, settings: ProxySettings, runtime_dir: Path) -> None:
        self.settings = settings
        self.runtime_dir = (runtime_dir / "proxy").resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._pool: NativeProxyPool | None = None
        self._transport_core: TunnelTransportCore | None = None
        self._core_candidates: list[ParsedProxyNode] = []
        self._forwarders: dict[str, LocalHTTPForwarder] = {}
        self._records: list[_NodeRecord] = []
        self._record_by_endpoint: dict[str, _NodeRecord] = {}
        self._record_by_id: dict[str, _NodeRecord] = {}
        self._node_meta: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, ProxyLease] = {}
        self._pending_acquires: dict[str, threading.Event] = {}
        self._generation = 0
        self._running = False
        self._last_error = ""
        self._source_summary: dict[str, Any] = {
            "subscriptions": 0,
            "source_nodes": 0,
            "pool_nodes": 0,
            "skipped_nodes": 0,
            "scheme_counts": {},
            "warnings": [],
        }

    def _set_records(self, records: list[_NodeRecord]) -> None:
        with self._lock:
            self._records = records
            self._record_by_endpoint = {record.endpoint: record for record in records}
            self._record_by_id = {record.id: record for record in records}
            self._node_meta = {
                record.id: {
                    "id": record.id,
                    "name": record.name,
                    "protocol": record.protocol,
                    "tags": list(record.tags),
                }
                for record in records
            }

    @staticmethod
    def _node_tags(name: str, protocol: str, host: str) -> list[str]:
        combined = " ".join([name, protocol, host]).lower()
        tokens = set(re.findall(r"[a-z][a-z0-9_-]{1,20}", combined))
        tags: set[str] = {protocol.lower(), *tokens}
        for canonical, aliases in _REGIONS.items():
            matched = False
            for alias in aliases:
                value = alias.lower()
                if re.fullmatch(r"[a-z][a-z0-9_-]*", value):
                    matched = value in tokens
                else:
                    matched = value in combined
                if matched:
                    break
            if matched:
                tags.add(canonical)
        return sorted(tags)

    @staticmethod
    def _probe_parts(url: str) -> tuple[str, int]:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ProxyPoolError("探活地址必须是带主机名的 https:// URL")
        if parsed.username or parsed.password:
            raise ProxyPoolError("探活地址不接受 URL 凭据")
        port = int(parsed.port or 443)
        if not 1 <= port <= 65535:
            raise ProxyPoolError("探活端口超出范围")
        return parsed.hostname, port

    @staticmethod
    def _public_probe_target(url: str) -> str:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        return f"https://{host}{port}{parsed.path or '/'}"

    def _safe_warning(self, warning: object) -> str:
        text = str(warning or "")
        for index, url in enumerate(self.settings.subscription_urls, start=1):
            text = text.replace(url, f"SUBSCRIPTION_URL_{index}")
        return redact_text(text, limit=500)

    def _collect_nodes(self, *, force_refresh: bool) -> tuple[list[_NodeRecord], dict[str, Any]]:
        del force_refresh  # Fetches are explicit; no hidden external cache is used.
        parsed_nodes: list[ParsedProxyNode] = []
        warnings: list[str] = []
        if self.settings.subscription_urls:
            remote_nodes, remote_warnings = fetch_subscriptions(
                self.settings.subscription_urls,
                timeout=self.settings.subscription_timeout_seconds,
                max_workers=min(8, max(1, len(self.settings.subscription_urls))),
            )
            parsed_nodes.extend(remote_nodes)
            warnings.extend(remote_warnings)
        if self.settings.node_file:
            node_file = self.settings.node_file.resolve()
            if not node_file.is_file():
                raise ProxyPoolError(f"节点文件不存在: {node_file}")
            parsed_nodes.extend(parse_subscription_text(node_file.read_text(encoding="utf-8-sig")))
        if self.settings.inline_nodes:
            parsed_nodes.extend(parse_subscription_text("\n".join(self.settings.inline_nodes)))

        records: list[_NodeRecord] = []
        core_candidates: list[ParsedProxyNode] = []
        seen: set[str] = set()
        scheme_counts: dict[str, int] = {}
        skipped = 0
        for node in parsed_nodes:
            scheme = str(node.scheme or "unknown").lower()
            scheme_counts[scheme] = scheme_counts.get(scheme, 0) + 1
            if not node.usable or not node.endpoint:
                if self.settings.transport_core_enabled and node.core_config:
                    core_candidates.append(node)
                    continue
                skipped += 1
                continue
            if scheme.startswith("socks") and not self.settings.allow_socks:
                skipped += 1
                continue
            if node.endpoint in seen:
                continue
            seen.add(node.endpoint)
            name = node.name or f"{scheme}-{node.host}"
            node_id = hashlib.sha256(node.endpoint.encode("utf-8")).hexdigest()[:20]
            records.append(
                _NodeRecord(
                    id=node_id,
                    name=name,
                    protocol=scheme,
                    endpoint=node.endpoint,
                    tags=self._node_tags(name, scheme, node.host),
                )
            )

        self._set_records(records)
        with self._lock:
            self._core_candidates = list(core_candidates)
        summary = {
            "subscriptions": len(self.settings.subscription_urls),
            "source_nodes": len(parsed_nodes),
            "pool_nodes": len(records),
            "core_candidates": len(core_candidates),
            "core_nodes": 0,
            "skipped_nodes": skipped,
            "scheme_counts": scheme_counts,
            "warnings": [self._safe_warning(item) for item in warnings[:20]],
        }
        return records, summary

    def start(self, *, force_refresh: bool = True, probe_url: str | None = None) -> dict[str, Any]:
        with self._lifecycle_lock:
            if not self.settings.enabled:
                raise ProxyPoolError("代理池配置为停用状态")
            with self._lock:
                if self._leases or self._pending_acquires:
                    raise ProxyPoolConflict("仍有任务正在申请或持有代理租约")
                old_pool = self._pool
                old_core = self._transport_core
                self._pool = None
                self._transport_core = None
                self._running = False
                self._generation += 1
            if old_pool is not None:
                old_pool.close()
            if old_core is not None:
                old_core.stop()
            new_core: TunnelTransportCore | None = None
            try:
                records, summary = self._collect_nodes(force_refresh=force_refresh)
                with self._lock:
                    core_candidates = list(self._core_candidates)
                if core_candidates:
                    new_core = TunnelTransportCore(
                        binary_path=self.settings.transport_core_binary,
                        expected_sha256=self.settings.transport_core_sha256,
                        runtime_dir=self.runtime_dir / "transport-core",
                        base_port=self.settings.transport_core_base_port,
                        start_timeout_seconds=self.settings.transport_core_start_timeout_seconds,
                    )
                    core_endpoints = new_core.start(core_candidates)
                    for endpoint in core_endpoints:
                        tags = set(
                            self._node_tags(
                                endpoint.name,
                                endpoint.source_protocol,
                                endpoint.source_host,
                            )
                        )
                        tags.update({"http", "transport-core", endpoint.source_protocol})
                        records.append(
                            _NodeRecord(
                                id=endpoint.id,
                                name=endpoint.name,
                                protocol="http",
                                endpoint=endpoint.local_http,
                                tags=sorted(tags),
                            )
                        )
                    summary["core_nodes"] = len(core_endpoints)
                    summary["pool_nodes"] = len(records)
                    summary["skipped_nodes"] += max(0, len(core_candidates) - len(core_endpoints))
                if not records:
                    raise ProxyPoolError("节点源中未解析出可直接使用的 HTTP/HTTPS/SOCKS 代理")
                self._set_records(records)
                native_pool = NativeProxyPool(record.endpoint for record in records)
                with self._lock:
                    self._pool = native_pool
                    self._transport_core = new_core
                    self._source_summary = summary
                    self._running = True
                    self._generation += 1
                probed = self.probe(target_url=probe_url or self.settings.probe_url)
                self._last_error = "" if probed.get("healthy") else "全部代理节点探活未通过"
                return {
                    "start": {"engine": self.settings.engine, "nodes": len(records)},
                    "probe": probed,
                    "status": self.status(),
                }
            except Exception as exc:
                with self._lock:
                    failed_pool = self._pool
                    failed_core = self._transport_core or new_core
                    self._pool = None
                    self._transport_core = None
                    self._running = False
                    self._generation += 1
                if failed_pool is not None:
                    failed_pool.close()
                if failed_core is not None:
                    failed_core.stop()
                self._last_error = redact_text(exc, limit=1000)
                raise

    def reload(self, *, force_refresh: bool = True, probe_url: str | None = None) -> dict[str, Any]:
        with self._lifecycle_lock:
            with self._lock:
                if self._leases or self._pending_acquires:
                    raise ProxyPoolConflict("仍有任务正在申请或持有代理租约")
            self.stop(force=False)
            return self.start(force_refresh=force_refresh, probe_url=probe_url)

    def stop(self, *, force: bool = False) -> dict[str, Any]:
        with self._lifecycle_lock:
            with self._lock:
                active_task_ids = list(self._leases)
                has_pending = bool(self._pending_acquires)
                if (active_task_ids or has_pending) and not force:
                    raise ProxyPoolConflict("仍有任务正在申请或持有代理租约")
                self._running = False
                self._generation += 1
            if force:
                for task_id in active_task_ids:
                    self.release(task_id, proxy_fault=False, reason="代理池停止")
            with self._lock:
                forwarders = list(self._forwarders.values())
                self._forwarders.clear()
                pool = self._pool
                transport_core = self._transport_core
                self._pool = None
                self._transport_core = None
                self._running = False
            for forwarder in forwarders:
                forwarder.stop()
            if pool is not None:
                pool.close()
            if transport_core is not None:
                transport_core.stop()
            return self.status()

    def _node_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records)
            running = self._running
            pool_status = self._pool.status() if self._pool is not None else {}
            now = time.time()
            rows: list[dict[str, Any]] = []
            for record in records:
                status = pool_status.get(record.endpoint, {})
                cooldown_left = float(status.get("cooldown_left") or 0.0)
                rows.append(
                    {
                        "id": record.id,
                        "name": record.name,
                        "protocol": record.protocol,
                        "endpoint": mask_proxy(record.endpoint),
                        "healthy": bool(running and record.healthy),
                        "retry_eligible": bool(running and cooldown_left <= 0),
                        "ref_count": 1 if status.get("leased") else 0,
                        "success_count": record.success_count,
                        "fail_count": record.fail_count,
                        "last_latency_ms": record.last_latency_ms,
                        "cooldown_until": now + cooldown_left,
                        "last_error": redact_text(record.last_error, limit=300),
                        "tags": list(record.tags),
                    }
                )
            return rows

    def status(self) -> dict[str, Any]:
        nodes = self._node_rows()
        with self._lock:
            transport_core = self._transport_core
        core_status = (
            transport_core.status()
            if transport_core is not None
            else {"enabled": self.settings.transport_core_enabled, "running": False, "listeners": 0}
        )
        return {
            "enabled": self.settings.enabled,
            "engine": self.settings.engine,
            "managed_by_backend": True,
            "auto_start": self.settings.auto_start,
            "executable_required": False,
            "running": bool(self._running and self._pool is not None),
            "total": len(nodes),
            "healthy": sum(1 for node in nodes if node["healthy"]),
            "retry_eligible": sum(1 for node in nodes if node["retry_eligible"]),
            "leases": len(self._leases),
            "last_error": self._last_error,
            "transport_core": core_status,
            "sources": dict(self._source_summary),
            "nodes": nodes,
        }

    def _probe_endpoint(
        self,
        node_id: str,
        endpoint: str,
        target_url: str,
        *,
        update_pool: bool = True,
    ) -> dict[str, Any]:
        self._probe_parts(target_url)
        started = time.time()
        healthy = False
        error = ""
        try:
            response = requests.get(
                target_url,
                proxies={"http": endpoint, "https": endpoint},
                headers={"User-Agent": "gallery-dl-backend-probe/1.0"},
                timeout=self.settings.probe_timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            status_code = int(response.status_code)
            response.close()
            healthy = status_code < 500 and status_code != 407
            if not healthy:
                error = f"代理探活返回 HTTP {status_code}"
        except Exception as exc:
            error = redact_text(exc, limit=300)
        latency = (time.time() - started) * 1000.0
        with self._lock:
            record = self._record_by_id.get(node_id)
            if record is not None:
                record.healthy = healthy
                record.last_latency_ms = latency if healthy else record.last_latency_ms
                record.last_error = "" if healthy else error
                record.cooldown_until = (
                    0.0 if healthy else time.time() + self.settings.fail_cooldown_seconds
                )
                if update_pool:
                    if healthy:
                        record.success_count += 1
                    else:
                        record.fail_count += 1
            if update_pool and self._pool is not None:
                self._pool.mark(
                    endpoint,
                    success=healthy,
                    cooldown_seconds=self.settings.fail_cooldown_seconds,
                )
        return {
            "id": node_id,
            "healthy": healthy,
            "latency_ms": latency if healthy else None,
            "error": error,
            "endpoint": mask_proxy(endpoint),
            "target": self._public_probe_target(target_url),
        }

    def probe(self, *, target_url: str | None = None, node_id: str | None = None) -> dict[str, Any]:
        with self._lifecycle_lock:
            if not self._running or self._pool is None:
                raise ProxyPoolError("代理池尚未加载")
            target = target_url or self.settings.probe_url
            self._probe_parts(target)
            with self._lock:
                records = [record for record in self._records if node_id is None or record.id == node_id]
            if node_id and not records:
                raise ProxyPoolError("节点不存在")
            results: list[dict[str, Any]] = []
            workers = max(1, min(self.settings.probe_workers, len(records), 64))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._probe_endpoint, record.id, record.endpoint, target): record
                    for record in records
                }
                for future in as_completed(futures):
                    record = futures[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        error = redact_text(exc, limit=300)
                        with self._lock:
                            record.healthy = False
                            record.fail_count += 1
                            record.last_error = error
                            record.cooldown_until = time.time() + self.settings.fail_cooldown_seconds
                            if self._pool is not None:
                                self._pool.mark(
                                    record.endpoint,
                                    success=False,
                                    cooldown_seconds=self.settings.fail_cooldown_seconds,
                                )
                        results.append({"id": record.id, "healthy": False, "error": error})
            healthy = sum(1 for item in results if item.get("healthy"))
            self._last_error = "" if healthy else f"探活目标 {self._public_probe_target(target)} 无健康节点"
            return {
                "total": len(results),
                "healthy": healthy,
                "results": results,
                "target": self._public_probe_target(target),
            }

    def acquire(
        self,
        task_id: str,
        *,
        node_tags: list[str] | None = None,
        exclude_ids: set[str] | None = None,
        allowed_ids: Collection[str] | None = None,
        probe_before_use: bool = False,
        probe_url: str | None = None,
    ) -> ProxyLease | None:
        wait_event: threading.Event | None = None
        with self._lock:
            if task_id in self._leases:
                return self._leases[task_id]
            wait_event = self._pending_acquires.get(task_id)
            if wait_event is not None:
                pool = None
                generation = -1
                allowed_records: list[_NodeRecord] = []
            else:
                if not self._running or self._pool is None:
                    return None
                pool = self._pool
                generation = self._generation
                excluded = set(exclude_ids or set())
                allowed_node_ids = (
                    None
                    if allowed_ids is None
                    else {str(node_id) for node_id in allowed_ids}
                )
                wanted = {str(tag).strip().lower() for tag in (node_tags or []) if str(tag).strip()}
                allowed_records = [
                    record
                    for record in self._records
                    if record.id not in excluded
                    and (allowed_node_ids is None or record.id in allowed_node_ids)
                    and (not wanted or wanted.intersection(record.tags))
                ]
                self._pending_acquires[task_id] = threading.Event()
        if wait_event is not None:
            wait_event.wait()
            with self._lock:
                return self._leases.get(task_id)

        records_by_endpoint = {record.endpoint: record for record in allowed_records}
        remaining = set(records_by_endpoint)
        try:
            while remaining:
                pool_lease = pool.acquire(owner=task_id, allowed=remaining)
                if pool_lease is None:
                    return None
                record = records_by_endpoint.get(pool_lease.endpoint)
                if record is None:
                    pool.release(pool_lease.token)
                    remaining.discard(pool_lease.endpoint)
                    continue
                if probe_before_use:
                    result = self._probe_endpoint(
                        record.id,
                        record.endpoint,
                        probe_url or self.settings.probe_url,
                        update_pool=False,
                    )
                    if not result.get("healthy"):
                        with self._lock:
                            record.fail_count += 1
                        pool.release(
                            pool_lease.token,
                            success=False,
                            cooldown_seconds=self.settings.fail_cooldown_seconds,
                        )
                        remaining.discard(record.endpoint)
                        continue
                endpoint = record.endpoint
                forwarder: LocalHTTPForwarder | None = None
                parsed = urlsplit(record.endpoint)
                if record.protocol in {"http", "https"} and (parsed.username or parsed.password):
                    try:
                        forwarder = LocalHTTPForwarder(record.endpoint)
                        endpoint = forwarder.start()
                    except Exception as exc:
                        if forwarder is not None:
                            forwarder.stop()
                        with self._lock:
                            record.fail_count += 1
                            record.last_error = redact_text(exc, limit=300)
                        pool.release(
                            pool_lease.token,
                            success=False,
                            cooldown_seconds=self.settings.fail_cooldown_seconds,
                        )
                        remaining.discard(record.endpoint)
                        continue
                lease = ProxyLease(
                    task_id=task_id,
                    node_id=record.id,
                    endpoint=endpoint,
                    name=record.name,
                    protocol=record.protocol,
                    tags=list(record.tags),
                    acquired_at=time.time(),
                    pool_endpoint=record.endpoint,
                    pool_token=pool_lease.token,
                    forwarder_key=task_id if forwarder is not None else "",
                )
                with self._lock:
                    existing = self._leases.get(task_id)
                    current = bool(
                        self._running
                        and self._pool is pool
                        and self._generation == generation
                    )
                    if existing is None and current:
                        self._leases[task_id] = lease
                        if forwarder is not None:
                            self._forwarders[task_id] = forwarder
                        return lease
                if forwarder is not None:
                    forwarder.stop()
                pool.release(pool_lease.token)
                return existing
            return None
        finally:
            with self._lock:
                pending = self._pending_acquires.pop(task_id, None)
                if pending is not None:
                    pending.set()

    def release(self, task_id: str, *, proxy_fault: bool, reason: str = "") -> None:
        with self._lock:
            lease = self._leases.pop(task_id, None)
            if lease is None:
                return
            forwarder = self._forwarders.pop(lease.forwarder_key, None) if lease.forwarder_key else None
            record = self._record_by_endpoint.get(lease.pool_endpoint)
            if record is not None:
                if proxy_fault:
                    record.fail_count += 1
                    record.healthy = False
                    record.last_error = redact_text(reason, limit=300)
                    record.cooldown_until = time.time() + self.settings.fail_cooldown_seconds
                else:
                    record.success_count += 1
            pool = self._pool
        if forwarder is not None:
            forwarder.stop()
        if pool is not None and lease.pool_token:
            pool.release(
                lease.pool_token,
                success=not proxy_fault,
                cooldown_seconds=self.settings.fail_cooldown_seconds if proxy_fault else 0.0,
            )

    @property
    def active_leases(self) -> int:
        with self._lock:
            return len(self._leases)
