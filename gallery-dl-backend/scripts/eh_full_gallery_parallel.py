from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

import requests
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gdl_backend.app import ServiceContainer, create_app
from gdl_backend.config import AppSettings
from gdl_backend.redaction import redact_text


TERMINAL = {"succeeded", "failed", "cancelled"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
GALLERY_URL_RE = re.compile(
    r"^https://(?:e-hentai|exhentai)\.org/g/(?P<gid>\d+)/(?P<token>[0-9a-f]{10})/"
)
IMAGE_PAGE_RE = re.compile(
    r"https://(?:e-hentai|exhentai)\.org/s/[0-9a-f]{10}/(?P<gid>\d+)-(?P<num>\d+)$"
)


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:60] or "query"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_json(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _extract_gallery_queue(data: Any) -> tuple[str, int]:
    for value in _walk_json(data):
        if not isinstance(value, list) or len(value) < 2 or value[0] != 6:
            continue
        url = str(value[1] or "")
        match = GALLERY_URL_RE.match(url)
        if match:
            return url, int(match.group("gid"))
    raise ValueError("gallery-dl 搜索结果中未找到画廊队列 URL")


def _parse_gallery_page(page: str, gallery_url: str, gid: int) -> tuple[str, int, dict[int, str]]:
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
        match = IMAGE_PAGE_RE.match(url)
        if match and int(match.group("gid")) == gid:
            links[int(match.group("num"))] = url
    return title, total, links


def _stdout_json(client: TestClient, task_id: str) -> Any:
    response = client.get(f"/api/v1/tasks/{task_id}/logs", params={"tail": 5000})
    response.raise_for_status()
    lines = [
        row["line"]
        for row in response.json()["items"]
        if row.get("stream") == "stdout"
    ]
    if not lines:
        raise ValueError("gallery-dl JSON 发现任务没有 stdout")
    return json.loads("\n".join(lines))


def _create_task(
    client: TestClient,
    *,
    url: str,
    output_dir: str,
    config_file: Path,
    extra_args: list[str],
    idempotency_key: str,
    max_attempts: int = 3,
    priority: int = 0,
) -> str:
    response = client.post(
        "/api/v1/tasks",
        json={
            "url": url,
            "site": "exhentai",
            "output_dir": output_dir,
            "proxy_mode": "required",
            "max_attempts": max_attempts,
            "priority": priority,
            "config_file": str(config_file),
            "extra_args": extra_args,
        },
        headers={"Idempotency-Key": idempotency_key},
    )
    if response.status_code >= 400:
        raise RuntimeError(f"创建 EH 任务失败 ({response.status_code}): {response.text}")
    return str(response.json()["id"])


def _task_map(client: TestClient, task_ids: set[str]) -> dict[str, dict[str, Any]]:
    response = client.get("/api/v1/tasks", params={"limit": 500})
    response.raise_for_status()
    return {
        str(item["id"]): item
        for item in response.json()["items"]
        if str(item["id"]) in task_ids
    }


def _wait_tasks(
    client: TestClient,
    task_ids: list[str],
    *,
    deadline: float,
    progress_label: str,
) -> tuple[dict[str, dict[str, Any]], int]:
    wanted = set(task_ids)
    max_active = 0
    last_progress = 0.0
    tasks: dict[str, dict[str, Any]] = {}
    while time.time() < deadline:
        scheduler = client.get("/api/v1/scheduler/status").json()
        active = int(scheduler.get("active") or 0)
        max_active = max(max_active, active)
        tasks = _task_map(client, wanted)
        complete = sum(task.get("status") in TERMINAL for task in tasks.values())
        now = time.time()
        if now - last_progress >= 10.0:
            statuses = Counter(task.get("status") for task in tasks.values())
            print(
                f"[{progress_label}] {complete}/{len(wanted)} terminal; "
                f"active={active}; status={dict(statuses)}",
                flush=True,
            )
            last_progress = now
        if len(tasks) == len(wanted) and complete == len(wanted):
            return tasks, max_active
        time.sleep(0.25)
    for task_id in task_ids:
        client.post(f"/api/v1/tasks/{task_id}/cancel")
    raise TimeoutError(f"{progress_label} 超过运行时限")


def _collect_image_pages(
    container: ServiceContainer,
    gallery_url: str,
    gid: int,
    *,
    stamp: str,
    attempts: int = 3,
) -> tuple[str, int, dict[int, str], str, int]:
    last_error = ""
    for attempt in range(1, attempts + 1):
        lease_id = f"eh-index-{stamp}-{attempt}"
        lease = container.proxy.acquire(lease_id)
        if lease is None:
            last_error = "代理池没有可分配的索引节点"
            continue
        proxy_fault = False
        try:
            session = requests.Session()
            session.headers["User-Agent"] = "gallery-dl-backend/eh-parallel"
            session.cookies.set("nw", "1", domain=".e-hentai.org")
            proxies = {"http": lease.endpoint, "https": lease.endpoint}
            title = ""
            expected = 0
            image_pages: dict[int, str] = {}
            page_index = 0
            empty_pages = 0
            while page_index < 200:
                page_url = gallery_url if page_index == 0 else f"{gallery_url}?p={page_index}"
                response = session.get(page_url, proxies=proxies, timeout=45)
                response.raise_for_status()
                parsed_title, parsed_total, links = _parse_gallery_page(
                    response.text,
                    gallery_url,
                    gid,
                )
                if page_index == 0:
                    title = parsed_title
                    expected = parsed_total
                    if not title or expected <= 0:
                        raise ValueError("画廊首页缺少标题或图片总数")
                before = len(image_pages)
                image_pages.update(links)
                empty_pages = empty_pages + 1 if len(image_pages) == before else 0
                if expected and len(image_pages) >= expected:
                    break
                if empty_pages >= 2:
                    raise ValueError(
                        f"画廊索引提前结束: collected={len(image_pages)}, expected={expected}"
                    )
                page_index += 1
            expected_numbers = set(range(1, expected + 1))
            missing = sorted(expected_numbers.difference(image_pages))
            if missing:
                raise ValueError(f"画廊索引缺少 {len(missing)} 页，首个缺页={missing[0]}")
            container.proxy.release(lease_id, proxy_fault=False)
            return title, expected, image_pages, lease.node_id, page_index + 1
        except Exception as exc:
            proxy_fault = True
            last_error = redact_text(exc, limit=500)
        finally:
            if proxy_fault:
                container.proxy.release(lease_id, proxy_fault=True, reason=last_error)
    raise RuntimeError(f"画廊索引获取失败: {last_error}")


def _inventory_images(output_root: Path, gid: int) -> tuple[list[dict[str, Any]], list[int]]:
    rows: list[dict[str, Any]] = []
    pages: list[int] = []
    prefix = re.compile(rf"^{gid}_(\d{{4,}})_")
    for path in sorted(output_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        match = prefix.match(path.name)
        if not match:
            continue
        number = int(match.group(1))
        pages.append(number)
        rows.append(
            {
                "page": number,
                "path": str(path),
                "relative_path": path.relative_to(output_root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _file_hash(path),
            }
        )
    rows.sort(key=lambda item: item["page"])
    return rows, sorted(pages)


def run(
    config_path: Path,
    query: str,
    concurrency: int,
    timeout: float,
    recovery_rounds: int,
) -> dict[str, Any]:
    project = config_path.resolve().parent
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_root = (
        project / "runtime" / f"eh-full-{concurrency}-{_slug(query)}-{stamp}"
    ).resolve()
    output_root = run_root / "downloads"
    settings = AppSettings.load(config_path)
    settings.runtime_dir = run_root
    settings.database_path = run_root / "backend.sqlite3"
    settings.default_output_root = output_root
    settings.allowed_output_roots = [output_root]
    settings.server.allow_private_targets = True
    settings.scheduler.max_concurrent_tasks = concurrency
    settings.scheduler.poll_interval_seconds = 0.05
    settings.proxy.probe_workers = concurrency
    settings.proxy.health_interval_seconds = max(timeout, 300.0)
    settings.default_site_policy.update(
        {
            "max_concurrency": concurrency,
            "proxy_mode": "required",
            "probe_url": "https://e-hentai.org/",
            "probe_before_use": False,
            "http_timeout": 45,
            "gallery_retries": 2,
            "task_timeout_seconds": min(timeout, 300.0),
            "retry_limit": 2,
            "backoff_base_seconds": 0.5,
        }
    )
    settings.ensure_directories()

    gallery_config = (project / "credentials" / "eh-resample.json").resolve()
    if not gallery_config.is_file():
        raise FileNotFoundError(gallery_config)
    search_url = "https://e-hentai.org/?" + urlencode({"f_search": query})
    container = ServiceContainer(settings)
    app = create_app(settings, container=container, start_background=False)
    started = time.time()

    with TestClient(app) as client:
        proxy_status = client.get("/api/v1/proxy/status").json()
        if not proxy_status.get("running"):
            response = client.post(
                "/api/v1/proxy/start",
                json={"force_refresh": True, "probe_url": "https://e-hentai.org/"},
            )
            response.raise_for_status()
            proxy_status = response.json()["status"]

        policy = dict(settings.default_site_policy)
        response = client.put("/api/v1/sites/policies/exhentai", json=policy)
        response.raise_for_status()
        if client.portal is None:
            raise RuntimeError("测试客户端后台 portal 尚未初始化")

        discovery_id = _create_task(
            client,
            url=search_url,
            output_dir="discovery",
            config_file=gallery_config,
            extra_args=["--dump-json", "--child-range", "1"],
            idempotency_key=f"eh-full-discovery-{stamp}",
            priority=100,
        )
        client.portal.call(container.scheduler.start)
        discovery_tasks, _ = _wait_tasks(
            client,
            [discovery_id],
            deadline=time.time() + min(timeout, 300.0),
            progress_label="search",
        )
        discovery = discovery_tasks[discovery_id]
        if discovery["status"] != "succeeded":
            raise RuntimeError(
                f"EH 搜索任务失败: {discovery.get('last_error_class')}: "
                f"{discovery.get('last_error')}"
            )
        gallery_url, gid = _extract_gallery_queue(_stdout_json(client, discovery_id))
        client.portal.call(container.scheduler.stop)

        title, expected_images, image_pages, index_node_id, index_page_count = (
            _collect_image_pages(container, gallery_url, gid, stamp=stamp)
        )
        print(
            f"[index] {title}; gid={gid}; images={expected_images}; "
            f"listing_pages={index_page_count}",
            flush=True,
        )

        download_task_ids: list[str] = []
        task_page: dict[str, int] = {}
        for page_number in range(1, expected_images + 1):
            task_id = _create_task(
                client,
                url=image_pages[page_number],
                output_dir="gallery",
                config_file=gallery_config,
                extra_args=["--range", "1"],
                idempotency_key=f"eh-full-{stamp}-{page_number:04d}",
                max_attempts=3,
                priority=expected_images - page_number,
            )
            download_task_ids.append(task_id)
            task_page[task_id] = page_number

        download_started = time.time()
        client.portal.call(container.scheduler.start)
        download_deadline = download_started + timeout
        tasks, max_active = _wait_tasks(
            client,
            download_task_ids,
            deadline=download_deadline,
            progress_label="download",
        )

        recovery_used = 0
        for recovery_used in range(1, recovery_rounds + 1):
            failed = [
                task_id
                for task_id, task in tasks.items()
                if task.get("status") != "succeeded"
            ]
            if not failed:
                recovery_used -= 1
                break
            print(f"[recovery-{recovery_used}] retrying {len(failed)} tasks", flush=True)
            for task_id in failed:
                response = client.post(
                    f"/api/v1/tasks/{task_id}/retry",
                    json={"additional_attempts": 2},
                )
                response.raise_for_status()
            retried, retry_active = _wait_tasks(
                client,
                failed,
                deadline=download_deadline,
                progress_label=f"recovery-{recovery_used}",
            )
            tasks.update(retried)
            max_active = max(max_active, retry_active)

        final_proxy = client.get("/api/v1/proxy/status").json()
        download_elapsed = time.time() - download_started
        tasks = {
            task_id: client.get(f"/api/v1/tasks/{task_id}").json()
            for task_id in download_task_ids
        }

    status_counts = Counter(task["status"] for task in tasks.values())
    node_ids: set[str] = set()
    total_attempts = 0
    errors: list[dict[str, Any]] = []
    for task_id, task in tasks.items():
        total_attempts += int(task.get("attempt_count") or 0)
        latest = task.get("latest_attempt") or {}
        if latest.get("proxy_node_id"):
            node_ids.add(str(latest["proxy_node_id"]))
        if task.get("status") != "succeeded":
            errors.append(
                {
                    "page": task_page[task_id],
                    "status": task.get("status"),
                    "error_class": task.get("last_error_class", ""),
                    "error": redact_text(task.get("last_error", ""), limit=500),
                }
            )

    images, downloaded_pages = _inventory_images(output_root / "gallery", gid)
    expected_pages = list(range(1, expected_images + 1))
    missing_pages = sorted(set(expected_pages).difference(downloaded_pages))
    duplicate_pages = sorted(
        page for page, count in Counter(downloaded_pages).items() if count > 1
    )
    total_bytes = sum(int(item["size"]) for item in images)
    manifest_digest = hashlib.sha256()
    for item in images:
        manifest_digest.update(
            f"{item['page']}\0{item['size']}\0{item['sha256']}\n".encode("utf-8")
        )
    with sqlite3.connect(settings.database_path) as connection:
        all_attempt_nodes = int(
            connection.execute(
                "SELECT COUNT(DISTINCT proxy_node_id) FROM attempts "
                "WHERE proxy_node_id IS NOT NULL AND proxy_node_id != ''"
            ).fetchone()[0]
        )
    report: dict[str, Any] = {
        "query": query,
        "search_url": search_url,
        "gallery": {
            "gid": gid,
            "title": title,
            "url": gallery_url,
            "expected_images": expected_images,
            "index_pages": index_page_count,
        },
        "requested_concurrency": concurrency,
        "observed_max_active": max_active,
        "elapsed_seconds": round(time.time() - started, 3),
        "download_elapsed_seconds": round(download_elapsed, 3),
        "download_task_status_counts": dict(status_counts),
        "download_tasks": len(download_task_ids),
        "total_attempts": total_attempts,
        "recovery_rounds_used": recovery_used,
        "unique_proxy_nodes": len(node_ids),
        "unique_proxy_nodes_all_attempts": all_attempt_nodes,
        "index_proxy_node_id": index_node_id,
        "downloaded_images": len(images),
        "downloaded_bytes": total_bytes,
        "image_manifest_sha256": manifest_digest.hexdigest(),
        "missing_pages": missing_pages,
        "duplicate_pages": duplicate_pages,
        "errors": errors,
        "images": images,
        "pool": {
            "source_nodes": final_proxy["sources"].get("source_nodes", 0),
            "core_nodes": final_proxy["sources"].get("core_nodes", 0),
            "healthy": final_proxy.get("healthy", 0),
            "total": final_proxy.get("total", 0),
            "core": final_proxy.get("transport_core", {}),
        },
        "run_root": str(run_root),
        "output_root": str(output_root / "gallery"),
        "report_path": str(run_root / "report.json"),
    }
    report_path = Path(report["report_path"])
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="EH exact search + full gallery + true parallel resample download"
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--query", default='"clover days"')
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--recovery-rounds", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 128:
        parser.error("--concurrency must be 1..128")
    if not 0 <= args.recovery_rounds <= 5:
        parser.error("--recovery-rounds must be 0..5")
    report = run(
        args.config,
        args.query,
        args.concurrency,
        args.timeout,
        args.recovery_rounds,
    )
    console_report = dict(report)
    console_report.pop("images", None)
    console_report["image_manifest_entries"] = len(report["images"])
    print(json.dumps(console_report, ensure_ascii=False, indent=2))
    success = (
        report["download_task_status_counts"] == {"succeeded": report["download_tasks"]}
        and report["downloaded_images"] == report["gallery"]["expected_images"]
        and not report["missing_pages"]
        and not report["duplicate_pages"]
        and report["observed_max_active"] == args.concurrency
    )
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
