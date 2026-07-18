from __future__ import annotations

import asyncio
import hmac
import ipaddress
import re
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .config import AppSettings
from .database import Database, TERMINAL_STATUSES
from .gallery import GalleryRunner
from .proxy import ProxyPoolAdapter, ProxyPoolConflict, ProxyPoolError
from .redaction import redact_text
from .scheduler import TaskScheduler
from .schemas import (
    ProxyProbeRequest,
    ProxyStartRequest,
    ProxyStopRequest,
    RetryRequest,
    SitePolicy,
    TaskCreate,
)
from .site import SiteResolver


class ApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class ServiceContainer:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.db = Database(
            settings.database_path,
            max_logs_per_task=settings.scheduler.max_logs_per_task,
        )
        self.proxy = ProxyPoolAdapter(settings.proxy, settings.runtime_dir)
        self.gallery = GalleryRunner(settings.gallery, settings.project_dir)
        self.scheduler = TaskScheduler(self.db, self.gallery, self.proxy, settings.scheduler)
        self.resolver = SiteResolver(settings.gallery.repo_path)
        self._health_task: asyncio.Task | None = None
        self._started = False

    def policy_for(self, site: str) -> SitePolicy:
        stored = self.db.get_site_policy(site)
        raw = stored["policy"] if stored else self.settings.default_site_policy
        return SitePolicy.model_validate(raw)

    async def start(self, *, background: bool = True) -> None:
        if self._started:
            return
        self._started = True
        if self.settings.proxy.enabled and self.settings.proxy.auto_start:
            try:
                await asyncio.to_thread(self.proxy.start, force_refresh=True)
            except Exception:
                # Service remains available in degraded mode; /proxy/status exposes the cause.
                pass
        if background:
            await self.scheduler.start()
            self._health_task = asyncio.create_task(self._proxy_health_loop(), name="proxy-health-monitor")

    async def stop(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
            self._health_task = None
        await self.scheduler.stop()
        try:
            await asyncio.to_thread(self.proxy.stop, force=True)
        except Exception:
            pass
        self.db.close()
        self._started = False

    async def _proxy_health_loop(self) -> None:
        interval = max(5.0, self.settings.proxy.health_interval_seconds)
        while True:
            try:
                await asyncio.sleep(interval)
                if self.proxy.status().get("running"):
                    await asyncio.to_thread(self.proxy.probe)
            except asyncio.CancelledError:
                break
            except Exception:
                continue


def _validate_site_name(site: str) -> str:
    value = site.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]{0,127}", value):
        raise ApiError(422, "invalid_site", "site 格式无效")
    return value


def _task_files(task: dict[str, Any], limit: int = 2000) -> list[dict[str, Any]]:
    root = Path(task["output_dir"]).resolve()
    if not root.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if len(files) >= limit:
            break
        try:
            if path.is_symlink():
                continue
            if path.is_file():
                stat = path.stat()
                files.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "size": stat.st_size,
                        "modified_at": stat.st_mtime,
                    }
                )
        except OSError:
            continue
    return files


def _validate_network_target(url: str, allow_private: bool) -> None:
    if allow_private:
        return
    text = url.strip()
    lower = text.lower()
    starts = [pos for pos in (lower.find("http://"), lower.find("https://")) if pos >= 0]
    if starts:
        text = text[min(starts) :]
    parsed = urlsplit(text)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("目标 URL 缺少主机名")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("目标 URL 指向本机或私有网络")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("目标主机 DNS 解析失败") from exc
    for entry in addresses:
        address = entry[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if not ip.is_global:
            raise ValueError("目标 URL 指向本机或私有网络")


def create_app(
    settings: AppSettings | None = None,
    *,
    container: ServiceContainer | None = None,
    start_background: bool = True,
) -> FastAPI:
    settings = settings or AppSettings.load()
    settings.validate()
    service = container or ServiceContainer(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = service
        await service.start(background=start_background)
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="gallery-dl Backend",
        version=__version__,
        description="gallery-dl 子进程调度与内置订阅代理池后端",
        lifespan=lifespan,
    )
    app.state.container = service
    if settings.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.server.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                    "request_id": getattr(request.state, "request_id", ""),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "请求参数校验失败",
                    "details": jsonable_encoder(exc.errors()),
                    "request_id": getattr(request.state, "request_id", ""),
                }
            },
        )

    async def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        expected = settings.server.api_key
        if expected and not hmac.compare_digest(x_api_key or "", expected):
            raise ApiError(401, "invalid_api_key", "API Key 校验失败")

    def get_service(request: Request) -> ServiceContainer:
        return request.app.state.container

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

    @app.get("/")
    async def root():
        return {"service": "gallery-dl-backend", "version": __version__, "docs": "/docs"}

    @app.get("/healthz")
    async def healthz():
        return {"ok": service.db.ping(), "time": time.time()}

    @app.get("/readyz")
    async def readyz():
        gallery_ok = (settings.gallery.repo_path / "gallery_dl" / "__init__.py").is_file()
        payload = {
            "ready": bool(gallery_ok and service.db.ping()),
            "gallery_source": gallery_ok,
            "scheduler": service.scheduler.active_summary(),
            "proxy": {
                key: value
                for key, value in service.proxy.status().items()
                if key not in {"nodes", "binary"}
            },
        }
        return JSONResponse(status_code=200 if payload["ready"] else 503, content=payload)

    @api.get("/config")
    async def public_config(container: ServiceContainer = Depends(get_service)):
        return container.settings.public_dict()

    @api.post("/tasks")
    async def create_task(
        body: TaskCreate,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        container: ServiceContainer = Depends(get_service),
    ):
        if idempotency_key and len(idempotency_key) > 200:
            raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key 过长")
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key:
                raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key 为空")
            existing = container.db.get_task_by_idempotency(idempotency_key)
            if existing is not None:
                return JSONResponse(status_code=200, content=existing)
        try:
            await asyncio.to_thread(
                _validate_network_target,
                body.url,
                container.settings.server.allow_private_targets,
            )
            site_info = await asyncio.to_thread(container.resolver.resolve, body.url)
            site = body.site or site_info.site
            policy = container.policy_for(site)
            task_id = str(uuid.uuid4())
            output_dir = container.settings.task_output_dir(body.output_dir, task_id)
            cookies = container.settings.allowed_file(
                body.cookies_file,
                container.settings.allowed_cookie_roots,
                "cookies_file",
            )
            config_file = container.settings.allowed_file(
                body.config_file,
                container.settings.allowed_config_roots,
                "config_file",
            )
            container.gallery.validate_args([*policy.extra_args, *body.extra_args])
        except ValueError as exc:
            raise ApiError(422, "invalid_task", str(exc)) from exc
        task, created = container.db.create_task(
            {
                "id": task_id,
                "url": body.url,
                "site": site,
                "subcategory": site_info.subcategory,
                "extractor": site_info.extractor,
                "priority": body.priority,
                "output_dir": str(output_dir),
                "proxy_mode": body.proxy_mode or policy.proxy_mode,
                "max_attempts": body.max_attempts or (policy.retry_limit + 1),
                "cookies_file": str(cookies) if cookies else None,
                "config_file": str(config_file) if config_file else None,
                "credentials_ref": body.credentials_ref,
                "extra_args": body.extra_args,
                "policy": policy.model_dump(),
            },
            idempotency_key=idempotency_key,
        )
        container.scheduler.notify()
        return JSONResponse(status_code=202 if created else 200, content=task)

    @api.get("/tasks")
    async def list_tasks(
        status: str | None = None,
        site: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        container: ServiceContainer = Depends(get_service),
    ):
        items = container.db.list_tasks(status=status, site=site, limit=limit, offset=offset)
        return {"items": items, "limit": limit, "offset": offset}

    @api.get("/tasks/{task_id}")
    async def get_task(task_id: str, container: ServiceContainer = Depends(get_service)):
        task = container.db.get_task(task_id)
        if task is None:
            raise ApiError(404, "task_not_found", "任务不存在")
        return task

    @api.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, container: ServiceContainer = Depends(get_service)):
        task = await container.scheduler.cancel(task_id)
        if task is None:
            raise ApiError(404, "task_not_found", "任务不存在")
        return task

    @api.post("/tasks/{task_id}/retry", status_code=202)
    async def retry_task(task_id: str, body: RetryRequest, container: ServiceContainer = Depends(get_service)):
        try:
            task = container.scheduler.retry(task_id, body.additional_attempts)
        except RuntimeError as exc:
            raise ApiError(409, "task_state_conflict", str(exc)) from exc
        if task is None:
            raise ApiError(404, "task_not_found", "任务不存在")
        return task

    @api.get("/tasks/{task_id}/logs")
    async def task_logs(
        task_id: str,
        since: int = Query(default=0, ge=0),
        tail: int | None = Query(default=None, ge=1, le=5000),
        limit: int = Query(default=1000, ge=1, le=5000),
        container: ServiceContainer = Depends(get_service),
    ):
        if container.db.get_task(task_id) is None:
            raise ApiError(404, "task_not_found", "任务不存在")
        return {"items": container.db.get_logs(task_id, since=since, tail=tail, limit=limit)}

    @api.get("/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=5000),
        container: ServiceContainer = Depends(get_service),
    ):
        if container.db.get_task(task_id) is None:
            raise ApiError(404, "task_not_found", "任务不存在")
        return {"items": container.db.get_events(task_id, since=since, limit=limit)}

    @api.get("/tasks/{task_id}/files")
    async def list_task_files(task_id: str, container: ServiceContainer = Depends(get_service)):
        task = container.db.get_task(task_id)
        if task is None:
            raise ApiError(404, "task_not_found", "任务不存在")
        return {"items": await asyncio.to_thread(_task_files, task)}

    @api.get("/tasks/{task_id}/files/{relative_path:path}")
    async def download_task_file(task_id: str, relative_path: str, container: ServiceContainer = Depends(get_service)):
        task = container.db.get_task(task_id)
        if task is None:
            raise ApiError(404, "task_not_found", "任务不存在")
        root_dir = Path(task["output_dir"]).resolve()
        unresolved = root_dir / relative_path
        cursor = root_dir
        for part in Path(relative_path).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ApiError(404, "file_not_found", "任务文件不存在")
        target = unresolved.resolve()
        if not (target == root_dir or target.is_relative_to(root_dir)) or not target.is_file():
            raise ApiError(404, "file_not_found", "任务文件不存在")
        return FileResponse(target)

    @api.get("/sites/policies")
    async def site_policies(container: ServiceContainer = Depends(get_service)):
        return {
            "default": SitePolicy.model_validate(container.settings.default_site_policy).model_dump(),
            "items": container.db.list_site_policies(),
        }

    @api.get("/sites/policies/{site}")
    async def get_site_policy(site: str, container: ServiceContainer = Depends(get_service)):
        name = _validate_site_name(site)
        stored = container.db.get_site_policy(name)
        return stored or {"site": name, "policy": container.policy_for(name).model_dump(), "inherited": True}

    @api.put("/sites/policies/{site}")
    async def put_site_policy(site: str, body: SitePolicy, container: ServiceContainer = Depends(get_service)):
        name = _validate_site_name(site)
        try:
            container.gallery.validate_args(body.extra_args)
        except ValueError as exc:
            raise ApiError(422, "invalid_policy", str(exc)) from exc
        return container.db.put_site_policy(name, body.model_dump())

    @api.delete("/sites/policies/{site}")
    async def delete_site_policy(site: str, container: ServiceContainer = Depends(get_service)):
        name = _validate_site_name(site)
        if not container.db.delete_site_policy(name):
            raise ApiError(404, "site_policy_not_found", "站点策略不存在")
        return {"deleted": True, "site": name}

    @api.get("/proxy/status")
    async def proxy_status(container: ServiceContainer = Depends(get_service)):
        return await asyncio.to_thread(container.proxy.status)

    async def _proxy_action(call):
        try:
            return await asyncio.to_thread(call)
        except ProxyPoolConflict as exc:
            raise ApiError(409, "proxy_conflict", str(exc)) from exc
        except (ProxyPoolError, FileNotFoundError, ValueError, RuntimeError) as exc:
            raise ApiError(503, "proxy_error", redact_text(exc, limit=500)) from exc

    @api.post("/proxy/start")
    async def proxy_start(body: ProxyStartRequest, container: ServiceContainer = Depends(get_service)):
        return await _proxy_action(
            lambda: container.proxy.start(force_refresh=body.force_refresh, probe_url=body.probe_url)
        )

    @api.post("/proxy/reload")
    async def proxy_reload(body: ProxyStartRequest, container: ServiceContainer = Depends(get_service)):
        return await _proxy_action(
            lambda: container.proxy.reload(force_refresh=body.force_refresh, probe_url=body.probe_url)
        )

    @api.post("/proxy/stop")
    async def proxy_stop(body: ProxyStopRequest, container: ServiceContainer = Depends(get_service)):
        return await _proxy_action(lambda: container.proxy.stop(force=body.force))

    @api.post("/proxy/probe")
    async def proxy_probe(body: ProxyProbeRequest, container: ServiceContainer = Depends(get_service)):
        target = body.target_url
        if body.site and not target:
            target = container.policy_for(_validate_site_name(body.site)).probe_url
        if target:
            try:
                await asyncio.to_thread(
                    _validate_network_target,
                    target,
                    container.settings.server.allow_private_targets,
                )
            except ValueError as exc:
                raise ApiError(422, "invalid_probe_target", str(exc)) from exc
        return await _proxy_action(lambda: container.proxy.probe(target_url=target, node_id=body.node_id))

    @api.get("/scheduler/status")
    async def scheduler_status(container: ServiceContainer = Depends(get_service)):
        return container.scheduler.active_summary()

    app.include_router(api)
    return app
