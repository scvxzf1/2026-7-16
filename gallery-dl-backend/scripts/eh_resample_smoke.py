from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gdl_backend.app import ServiceContainer, create_app
from gdl_backend.config import AppSettings
from gdl_backend.redaction import redact_text


TERMINAL = {"succeeded", "failed", "cancelled"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:60] or "query"


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(config_path: Path, query: str, concurrency: int, timeout: float) -> dict:
    project = config_path.resolve().parent
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_root = (project / "runtime" / f"eh-{concurrency}-{_slug(query)}-{stamp}").resolve()
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
            "gallery_retries": 1,
            "task_timeout_seconds": timeout,
            "retry_limit": 1,
            "backoff_base_seconds": 0.25,
        }
    )
    settings.ensure_directories()

    gallery_config = (project / "credentials" / "eh-resample.json").resolve()
    if not gallery_config.is_file():
        raise FileNotFoundError(gallery_config)
    search_url = "https://e-hentai.org/?" + urlencode({"f_search": query})
    container = ServiceContainer(settings)
    app = create_app(settings, container=container, start_background=False)
    task_ids: list[str] = []
    actual_task_id = ""
    max_active = 0
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

        for index in range(concurrency):
            actual = index == 0
            args = ["--child-range", "1", "--range", "1", "--verbose"]
            if not actual:
                args.append("--simulate")
            body = {
                "url": search_url,
                "site": "exhentai",
                "output_dir": f"worker-{index:02d}-{'download' if actual else 'simulate'}",
                "proxy_mode": "required",
                "max_attempts": 2,
                "priority": 100 if actual else 0,
                "config_file": str(gallery_config),
                "extra_args": args,
            }
            response = client.post(
                "/api/v1/tasks",
                json=body,
                headers={"Idempotency-Key": f"eh-smoke-{stamp}-{index}"},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"创建 EH smoke 任务失败 ({response.status_code}): {response.text}")
            task_id = response.json()["id"]
            task_ids.append(task_id)
            if actual:
                actual_task_id = task_id

        if client.portal is None:
            raise RuntimeError("测试客户端后台 portal 尚未初始化")
        client.portal.call(container.scheduler.start)

        deadline = time.time() + timeout
        tasks: dict[str, dict] = {}
        while time.time() < deadline:
            scheduler = client.get("/api/v1/scheduler/status").json()
            max_active = max(max_active, int(scheduler.get("active") or 0))
            tasks = {
                task_id: client.get(f"/api/v1/tasks/{task_id}").json()
                for task_id in task_ids
            }
            if all(task.get("status") in TERMINAL for task in tasks.values()):
                break
            time.sleep(0.2)
        else:
            for task_id in task_ids:
                client.post(f"/api/v1/tasks/{task_id}/cancel")
            raise TimeoutError(f"EH smoke 超过 {timeout:.0f}s")

        actual_files = client.get(f"/api/v1/tasks/{actual_task_id}/files").json()["items"]
        actual_logs = client.get(
            f"/api/v1/tasks/{actual_task_id}/logs",
            params={"tail": 200},
        ).json()["items"]
        final_proxy = client.get("/api/v1/proxy/status").json()

    status_counts = Counter(task["status"] for task in tasks.values())
    node_ids = {
        task.get("latest_attempt", {}).get("proxy_node_id")
        for task in tasks.values()
        if task.get("latest_attempt", {}).get("proxy_node_id")
    }
    image_rows: list[dict] = []
    actual_output = Path(tasks[actual_task_id]["output_dir"])
    for item in actual_files:
        path = actual_output / item["path"]
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
            image_rows.append(
                {
                    "path": str(path),
                    "relative_path": item["path"],
                    "size": item["size"],
                    "sha256": _file_hash(path),
                }
            )

    report = {
        "query": query,
        "search_url": search_url,
        "requested_concurrency": concurrency,
        "observed_max_active": max_active,
        "elapsed_seconds": round(time.time() - started, 3),
        "task_status_counts": dict(status_counts),
        "unique_proxy_nodes": len(node_ids),
        "actual_task_id": actual_task_id,
        "actual_task_status": tasks[actual_task_id]["status"],
        "actual_attempt_count": tasks[actual_task_id]["attempt_count"],
        "actual_error_class": tasks[actual_task_id].get("last_error_class", ""),
        "actual_error": tasks[actual_task_id].get("last_error", ""),
        "images": image_rows,
        "pool": {
            "source_nodes": final_proxy["sources"].get("source_nodes", 0),
            "core_nodes": final_proxy["sources"].get("core_nodes", 0),
            "healthy": final_proxy.get("healthy", 0),
            "total": final_proxy.get("total", 0),
            "core": final_proxy.get("transport_core", {}),
        },
        "actual_log_tail": [redact_text(row["line"]) for row in actual_logs[-40:]],
        "run_root": str(run_root),
    }
    report_path = run_root / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="EH search + resample proxy-pool smoke test")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--query", default="clover days")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 128:
        parser.error("--concurrency must be 1..128")
    report = run(args.config, args.query, args.concurrency, args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    success = (
        report["actual_task_status"] == "succeeded"
        and len(report["images"]) == 1
        and report["observed_max_active"] == args.concurrency
    )
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
