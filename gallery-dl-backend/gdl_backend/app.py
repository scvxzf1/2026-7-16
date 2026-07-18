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
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import AuthError, AuthManager
from .config import AppSettings
from .crawl import CrawlPlanError, CrawlPlanner
from .database import Database, TERMINAL_STATUSES
from .discovery import (
    DiscoveryError,
    DiscoveryService,
    canonical_gallery_address,
    discovery_addresses,
    exhentai_tag_facets,
    search_site,
    search_site_catalog,
    validate_discovery_args,
)
from .gallery import GalleryRunner
from .ordered_crawl import OrderedCrawlManager
from .proxy import ProxyPoolAdapter, ProxyPoolConflict, ProxyPoolError
from .redaction import redact_text
from .scheduler import TaskScheduler
from .schemas import (
    CrawlRequest,
    PixivOAuthCompleteRequest,
    ProxyProbeRequest,
    ProxyStartRequest,
    ProxyStopRequest,
    RetryRequest,
    SearchRequest,
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
        self.auth = AuthManager(settings)
        self.gallery = GalleryRunner(settings.gallery, settings.project_dir)
        self.scheduler = TaskScheduler(
            self.db,
            self.gallery,
            self.proxy,
            settings.scheduler,
            credential_validator=self.auth.managed_credentials_available,
            auth_failure_callback=self.auth.invalidate_if_managed,
        )
        self.resolver = SiteResolver(settings.gallery.repo_path)
        self.discovery = DiscoveryService(
            self.gallery,
            self.proxy,
            settings.runtime_dir,
            auth_failure_callback=self.auth.invalidate_if_managed,
        )
        self.crawl_planner = CrawlPlanner(
            self.proxy,
            auth_failure_callback=self.auth.invalidate_if_managed,
        )
        self.ordered_crawls = OrderedCrawlManager(
            self.db,
            self.discovery,
            self.crawl_planner,
            self.scheduler,
            self.policy_for,
            poll_interval=settings.scheduler.poll_interval_seconds,
        )
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
            await self.ordered_crawls.start()
            self._health_task = asyncio.create_task(self._proxy_health_loop(), name="proxy-health-monitor")

    async def stop(self) -> None:
        if self._health_task is not None:
            self._health_task.cancel()
            await asyncio.gather(self._health_task, return_exceptions=True)
            self._health_task = None
        await self.ordered_crawls.stop()
        await self.scheduler.stop()
        await self.auth.stop()
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


def _canonical_site_name(site: str) -> str:
    value = _validate_site_name(site)
    try:
        return search_site(value).site
    except ValueError:
        return value


def _validate_site_match(explicit_site: str, resolved_site: str) -> None:
    try:
        explicit = search_site(explicit_site).site
        resolved = search_site(resolved_site).site
    except ValueError:
        return
    if explicit != resolved:
        raise ValueError(f"site={explicit} 与 URL 提取器站点 {resolved} 不一致")


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
    has_global = False
    for entry in addresses:
        address = entry[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_global:
            has_global = True
    if not has_global:
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
    app.mount(
        "/ui",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "webui"), html=True),
        name="webui",
    )
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
        return {
            "service": "gallery-dl-backend",
            "version": __version__,
            "ui": "/ui/",
            "docs": "/docs",
        }

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
            "ordered_crawls": service.ordered_crawls.status(),
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

    def _raise_auth_error(exc: AuthError) -> None:
        if exc.code in {
            "unsupported_auth_site",
            "pixiv_oauth_session_not_found",
            "browser_login_session_not_found",
        }:
            status_code = 404
        elif exc.code.startswith("invalid_"):
            status_code = 422
        else:
            status_code = 409
        raise ApiError(status_code, exc.code, exc.message, exc.details) from exc

    @api.get("/auth")
    async def auth_statuses(container: ServiceContainer = Depends(get_service)):
        return container.auth.statuses()

    @api.get("/auth/{site}")
    async def auth_status(site: str, container: ServiceContainer = Depends(get_service)):
        try:
            return container.auth.status(site)
        except AuthError as exc:
            _raise_auth_error(exc)

    @api.post("/auth/{site}/login/start", status_code=202)
    async def auth_start_browser_login(
        site: str,
        container: ServiceContainer = Depends(get_service),
    ):
        try:
            return await container.auth.start_browser_login(site)
        except AuthError as exc:
            _raise_auth_error(exc)

    @api.get("/auth/{site}/login/{session_id}")
    async def auth_browser_login_session(
        site: str,
        session_id: str,
        container: ServiceContainer = Depends(get_service),
    ):
        try:
            return container.auth.browser_login_session(site, session_id)
        except AuthError as exc:
            _raise_auth_error(exc)

    @api.delete("/auth/{site}/login/{session_id}")
    async def auth_cancel_browser_login(
        site: str,
        session_id: str,
        container: ServiceContainer = Depends(get_service),
    ):
        try:
            return await container.auth.cancel_browser_login(site, session_id)
        except AuthError as exc:
            _raise_auth_error(exc)

    @api.post("/auth/pixiv/oauth/start")
    async def auth_start_pixiv(container: ServiceContainer = Depends(get_service)):
        try:
            return await container.auth.start_pixiv_oauth()
        except AuthError as exc:
            _raise_auth_error(exc)

    @api.post("/auth/pixiv/oauth/complete")
    async def auth_complete_pixiv(
        body: PixivOAuthCompleteRequest,
        container: ServiceContainer = Depends(get_service),
    ):
        try:
            return await container.auth.complete_pixiv_oauth(body.session_id, body.callback)
        except AuthError as exc:
            _raise_auth_error(exc)

    @api.delete("/auth/pixiv/oauth/session")
    async def auth_cancel_pixiv(container: ServiceContainer = Depends(get_service)):
        return await container.auth.cancel_pixiv_oauth()

    @api.delete("/auth/{site}")
    async def auth_clear(site: str, container: ServiceContainer = Depends(get_service)):
        try:
            return await container.auth.clear(site)
        except AuthError as exc:
            _raise_auth_error(exc)

    def _validate_idempotency_key(value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 200:
            raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key 过长")
        result = value.strip()
        if not result:
            raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key 为空")
        return result

    def _allowed_request_files(
        container: ServiceContainer,
        *,
        cookies_file: str | None,
        config_file: str | None,
    ) -> tuple[Path | None, Path | None]:
        cookies = container.settings.allowed_file(
            cookies_file,
            container.settings.allowed_cookie_roots,
            "cookies_file",
        )
        config = container.settings.allowed_file(
            config_file,
            container.settings.allowed_config_roots,
            "config_file",
        )
        return cookies, config

    def _managed_request_credentials(
        container: ServiceContainer,
        site: str,
        *,
        credentials_ref: str | None,
        cookies_file: str | None,
        config_file: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        managed = container.auth.credentials_for(site)
        return (
            credentials_ref or managed.get("credentials_ref"),
            cookies_file or managed.get("cookies_file"),
            config_file or managed.get("config_file"),
        )

    async def _enqueue_task(
        body: TaskCreate,
        *,
        idempotency_key: str | None,
        container: ServiceContainer,
        concurrency_override: int | None = None,
        network_validated: bool = False,
        notify: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        key = _validate_idempotency_key(idempotency_key)
        if key is not None:
            existing = container.db.get_task_by_idempotency(key)
            if existing is not None:
                return existing, False
        try:
            if not network_validated:
                await asyncio.to_thread(
                    _validate_network_target,
                    body.url,
                    container.settings.server.allow_private_targets,
                )
            site_info = await asyncio.to_thread(container.resolver.resolve, body.url)
            if body.site:
                site = _canonical_site_name(body.site)
                if site_info.supported:
                    _validate_site_match(site, site_info.site)
            else:
                site = site_info.site
            policy = container.policy_for(site)
            if concurrency_override is not None:
                effective = min(
                    int(concurrency_override),
                    container.settings.scheduler.max_concurrent_tasks,
                )
                policy = policy.model_copy(update={"max_concurrency": max(1, effective)})
            task_id = str(uuid.uuid4())
            output_dir = container.settings.task_output_dir(body.output_dir, task_id)
            credentials_ref, cookies_value, config_value = _managed_request_credentials(
                container,
                site,
                credentials_ref=body.credentials_ref,
                cookies_file=body.cookies_file,
                config_file=body.config_file,
            )
            cookies, config_file = _allowed_request_files(
                container,
                cookies_file=cookies_value,
                config_file=config_value,
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
                "credentials_ref": credentials_ref,
                "extra_args": body.extra_args,
                "policy": policy.model_dump(),
            },
            idempotency_key=key,
        )
        if notify:
            container.scheduler.notify()
        return task, created

    async def _enqueue_ordered_task(
        body: TaskCreate,
        idempotency_key: str,
        concurrency: int,
    ) -> tuple[dict[str, Any], bool]:
        return await _enqueue_task(
            body,
            idempotency_key=idempotency_key,
            container=service,
            concurrency_override=concurrency,
            network_validated=True,
            notify=False,
        )

    service.ordered_crawls.set_enqueue(_enqueue_ordered_task)

    @api.post("/tasks")
    async def create_task(
        body: TaskCreate,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        container: ServiceContainer = Depends(get_service),
    ):
        task, created = await _enqueue_task(
            body,
            idempotency_key=idempotency_key,
            container=container,
        )
        return JSONResponse(status_code=202 if created else 200, content=task)

    def _effective_search_options(body: SearchRequest, site: str) -> dict[str, Any]:
        canonical_options: dict[str, Any] = {}
        for key, value in body.source_options.items():
            canonical = search_site(key).site
            if canonical in canonical_options:
                raise ValueError(f"source_options 重复配置来源: {canonical}")
            canonical_options[canonical] = value
        specific = canonical_options.get(site)

        def choose(name: str):
            if specific is not None and name in specific.model_fields_set:
                return getattr(specific, name)
            return getattr(body, name)

        return {
            "proxy_mode": choose("proxy_mode"),
            "credentials_ref": choose("credentials_ref"),
            "cookies_file": choose("cookies_file"),
            "config_file": choose("config_file"),
            "search_extra_args": [
                *body.search_extra_args,
                *(specific.search_extra_args if specific is not None else []),
            ],
            "timeout_seconds": choose("timeout_seconds"),
        }

    async def _perform_search(body: SearchRequest, container: ServiceContainer) -> dict[str, Any]:
        try:
            sites: list[str] = []
            for value in body.sites:
                canonical = search_site(value).site
                if canonical not in sites:
                    sites.append(canonical)
            options = {site: _effective_search_options(body, site) for site in sites}
        except ValueError as exc:
            raise ApiError(422, "invalid_search", str(exc)) from exc

        async def run_source(order: int, site: str) -> dict[str, Any]:
            spec = search_site(site)
            option = options[site]
            try:
                search_url = spec.search_url(body.keyword)
                await asyncio.to_thread(
                    _validate_network_target,
                    search_url,
                    container.settings.server.allow_private_targets,
                )
                credentials_ref, cookies_value, config_value = _managed_request_credentials(
                    container,
                    site,
                    credentials_ref=option["credentials_ref"],
                    cookies_file=option["cookies_file"],
                    config_file=option["config_file"],
                )
                cookies, config_file = _allowed_request_files(
                    container,
                    cookies_file=cookies_value,
                    config_file=config_value,
                )
                validate_discovery_args(option["search_extra_args"])
                container.gallery.validate_args(option["search_extra_args"])
                result = await container.discovery.search(
                    site=site,
                    keyword=body.keyword,
                    limit=body.limit,
                    policy=container.policy_for(site),
                    proxy_mode=option["proxy_mode"],
                    credentials_ref=credentials_ref,
                    cookies_file=str(cookies) if cookies else None,
                    config_file=str(config_file) if config_file else None,
                    extra_args=option["search_extra_args"],
                    timeout_seconds=option["timeout_seconds"],
                )
                source_enrichment_errors: list[dict[str, str]] = []
                if site == "exhentai":
                    try:
                        result = await container.discovery.enrich_exhentai_previews(
                            result,
                            policy=container.policy_for(site),
                            proxy_mode=option["proxy_mode"],
                            timeout_seconds=option["timeout_seconds"],
                        )
                    except Exception as exc:
                        result["preview_count"] = int(result.get("preview_count") or 0)
                        result["preview_missing_count"] = max(
                            0,
                            int(
                                result.get("candidate_count")
                                or len(result.get("candidates") or [])
                            )
                            - result["preview_count"],
                        )
                        source_enrichment_errors.append(
                            {
                                "stage": "exhentai_gallery_previews",
                                "message": redact_text(
                                    exc.message if isinstance(exc, DiscoveryError) else exc,
                                    limit=1000,
                                ),
                            }
                        )
                if site == "danbooru":
                    try:
                        artist_result = await container.discovery.search_danbooru_artists(
                            keyword=body.keyword,
                            limit=body.limit,
                            policy=container.policy_for(site),
                            proxy_mode=option["proxy_mode"],
                            credentials_ref=credentials_ref,
                            cookies_file=str(cookies) if cookies else None,
                            config_file=str(config_file) if config_file else None,
                            timeout_seconds=option["timeout_seconds"],
                        )
                        merged_authors = list(result.get("authors") or [])
                        author_by_key = {
                            str(author.get("works_url") or author.get("url") or author.get("name")): author
                            for author in merged_authors
                        }
                        for author in artist_result.get("authors") or []:
                            key = str(author.get("works_url") or author.get("url") or author.get("name"))
                            existing = author_by_key.get(key)
                            if existing is None:
                                merged_authors.append(author)
                                author_by_key[key] = author
                                continue
                            # Prefer the structured artist-directory identity over a
                            # post-derived author with the same works URL.
                            for field in (
                                "id",
                                "name",
                                "display_name",
                                "url",
                                "works_url",
                                "other_names",
                                "group_name",
                                "origin",
                            ):
                                value = author.get(field)
                                if value not in (None, "", []):
                                    existing[field] = value
                        result["authors"] = merged_authors
                    except Exception as exc:
                        source_enrichment_errors.append(
                            {
                                "stage": "danbooru_artist_directory",
                                "message": redact_text(
                                    exc.message if isinstance(exc, DiscoveryError) else exc,
                                    limit=1000,
                                ),
                            }
                        )
                discovered_addresses = discovery_addresses(
                    site,
                    result,
                    keyword=body.keyword,
                    limit=body.limit,
                )
                addresses = [
                    address
                    for address in discovered_addresses
                    if address.get("confidence") != "weak_evidence"
                ]
                weak_evidence = [
                    address
                    for address in discovered_addresses
                    if address.get("confidence") == "weak_evidence"
                ]
                tag_facets = (
                    exhentai_tag_facets(discovered_addresses)
                    if site == "exhentai"
                    else []
                )
                return {
                    "order": order,
                    "site": site,
                    "status": "partial" if source_enrichment_errors else "succeeded",
                    "search_url": result.get("search_url"),
                    "evidence_count": result.get("candidate_count", 0),
                    "preview_count": result.get("preview_count", 0),
                    "preview_missing_count": result.get("preview_missing_count", 0),
                    "address_count": len(addresses),
                    "addresses": addresses,
                    "weak_evidence_count": len(weak_evidence),
                    "weak_evidence": weak_evidence,
                    "tag_facets": tag_facets,
                    "proxy": result.get("proxy"),
                    "attempts": result.get("attempts", 0),
                    "error": None,
                    "enrichment_errors": source_enrichment_errors,
                    "auth": container.auth.status(site),
                }
            except Exception as exc:
                code = exc.code if isinstance(exc, DiscoveryError) else "invalid_search_source"
                message = exc.message if isinstance(exc, DiscoveryError) else str(exc)
                details = (
                    exc.details
                    if isinstance(exc, DiscoveryError) and isinstance(exc.details, dict)
                    else {}
                )
                return {
                    "order": order,
                    "site": site,
                    "status": "failed",
                    "search_url": spec.search_url(body.keyword),
                    "evidence_count": 0,
                    "preview_count": 0,
                    "preview_missing_count": 0,
                    "address_count": 0,
                    "addresses": [],
                    "weak_evidence_count": 0,
                    "weak_evidence": [],
                    "tag_facets": [],
                    "proxy": details.get("proxy"),
                    "attempts": int(details.get("attempts") or 0),
                    "error": {"code": code, "message": redact_text(message, limit=1000)},
                    "auth": container.auth.status(site),
                }

        sources = list(
            await asyncio.gather(
                *(run_source(order, site) for order, site in enumerate(sites))
            )
        )
        source_by_site = {source["site"]: source for source in sources}
        related_profiles: list[dict[str, Any]] = []
        enrichment_errors: list[dict[str, str]] = [
            {"source": source["site"], **error}
            for source in sources
            for error in source.get("enrichment_errors") or []
        ]
        danbooru = source_by_site.get("danbooru")
        if danbooru is not None and danbooru["addresses"]:
            artist_names = [
                str(address.get("tag") or "")
                for address in danbooru["addresses"]
                if address.get("address_type") == "artist_tag"
            ]
            if artist_names:
                try:
                    profiles, profile_errors = await container.discovery.danbooru_artist_profiles(
                        artist_names,
                        policy=container.policy_for("danbooru"),
                        proxy_mode=options["danbooru"]["proxy_mode"],
                        limit=body.limit,
                    )
                    enrichment_errors.extend(
                        {"source": "danbooru", **error} for error in profile_errors
                    )
                except Exception as exc:
                    profiles = []
                    enrichment_errors.append(
                        {
                            "source": "danbooru",
                            "artist": "*",
                            "message": redact_text(
                                exc.message if isinstance(exc, DiscoveryError) else exc,
                                limit=1000,
                            ),
                        }
                    )
                danbooru_errors = [
                    error for error in enrichment_errors if error.get("source") == "danbooru"
                ]
                if danbooru_errors:
                    danbooru["status"] = "partial"
                    danbooru["enrichment_errors"] = danbooru_errors
                profile_by_name = {str(profile["name"]): profile for profile in profiles}
                for address in danbooru["addresses"]:
                    profile = profile_by_name.get(str(address.get("tag") or ""))
                    if profile is None:
                        continue
                    address["danbooru_artist"] = {
                        key: profile.get(key)
                        for key in ("id", "name", "other_names", "group_name", "profile_url")
                    }
                    address["related_profiles"] = profile["related_profiles"]
                    for related in profile["related_profiles"]:
                        item = {
                            **related,
                            "artist_id": profile["id"],
                            "artist_name": profile["name"],
                            "origin": "danbooru_artist_url",
                        }
                        related_profiles.append(item)
                        crawl_site = related.get("crawl_site")
                        crawl_url = related.get("crawl_url")
                        if not related.get("active", True) or crawl_site not in source_by_site or not crawl_url:
                            continue
                        crawl_url = canonical_gallery_address(crawl_site, crawl_url)
                        target = source_by_site[crawl_site]
                        existing = next(
                            (
                                candidate
                                for candidate in target["addresses"]
                                if canonical_gallery_address(crawl_site, candidate.get("url") or "") == crawl_url
                            ),
                            None,
                        )
                        weak_existing = next(
                            (
                                candidate
                                for candidate in target["weak_evidence"]
                                if canonical_gallery_address(crawl_site, candidate.get("url") or "")
                                == crawl_url
                            ),
                            None,
                        )
                        if existing is not None:
                            origins = list(existing.get("origins") or [existing.get("origin", "site_search")])
                            if "danbooru_artist_url" not in origins:
                                origins.append("danbooru_artist_url")
                            existing["origins"] = origins
                            existing["confidence"] = "verified"
                            reasons = list(existing.get("evidence_reasons") or [])
                            if "danbooru_artist_url" not in reasons:
                                reasons.append("danbooru_artist_url")
                            existing["evidence_reasons"] = reasons
                            related_artists = existing.setdefault("related_artists", [])
                            if profile["name"] not in related_artists:
                                related_artists.append(profile["name"])
                            continue
                        if weak_existing is not None:
                            target["weak_evidence"].remove(weak_existing)
                            prior_origin = weak_existing.get("origin", "site_search")
                            weak_existing["origin"] = "danbooru_artist_url"
                            weak_existing["origins"] = list(
                                dict.fromkeys([prior_origin, "danbooru_artist_url"])
                            )
                            weak_existing["confidence"] = "verified"
                            reasons = list(weak_existing.get("evidence_reasons") or [])
                            if "danbooru_artist_url" not in reasons:
                                reasons.append("danbooru_artist_url")
                            weak_existing["evidence_reasons"] = reasons
                            weak_existing["related_artists"] = list(
                                dict.fromkeys(
                                    [*weak_existing.get("related_artists", []), profile["name"]]
                                )
                            )
                            target["addresses"].append(weak_existing)
                            if target["status"] == "failed":
                                target["status"] = "partial"
                            continue
                        if len(target["addresses"]) >= body.limit:
                            continue
                        target["addresses"].append(
                            {
                                "id": f"{crawl_site}:danbooru:{profile['id']}:{len(target['addresses']) + 1}",
                                "source": crawl_site,
                                "address_type": "account",
                                "label": profile["name"],
                                "url": crawl_url,
                                "profile_url": related["url"],
                                "origin": "danbooru_artist_url",
                                "confidence": "verified",
                                "evidence_reasons": ["danbooru_artist_url"],
                                "related_artists": [profile["name"]],
                            }
                        )
                        if target["status"] == "failed":
                            target["status"] = "partial"
                for source in sources:
                    source["address_count"] = len(source["addresses"])
                    source["weak_evidence_count"] = len(source["weak_evidence"])

        return {
            "keyword": body.keyword,
            "source_count": len(sources),
            "address_count": sum(len(source["addresses"]) for source in sources),
            "weak_evidence_count": sum(len(source["weak_evidence"]) for source in sources),
            "sources": sources,
            "related_profiles": related_profiles,
            "enrichment_errors": enrichment_errors,
            "selection_contract": {
                "field": "sources[].addresses[]",
                "weak_evidence_field": "sources[].weak_evidence[]",
                "default_visibility": "addresses_only",
                "execution_order": "source_then_address",
                "address_execution": "media_parallel",
            },
            "tag_filter_contract": {
                "source": "exhentai",
                "facets_field": "sources[].tag_facets[]",
                "tags_field": "sources[].addresses[].metadata.tags[]",
                "same_namespace": "or",
                "across_namespaces": "and",
                "exclusions": "take_precedence",
            },
        }

    @api.get("/search/sites")
    async def supported_search_sites():
        return {"items": search_site_catalog()}

    @api.post("/search")
    async def search_candidates(
        body: SearchRequest,
        container: ServiceContainer = Depends(get_service),
    ):
        return await _perform_search(body, container)

    def _range_argument_present(args: list[str]) -> bool:
        managed = {
            "--range",
            "--file-range",
            "--image-range",
            "--post-range",
            "--child-range",
            "--chapter-range",
        }
        return any(str(value).split("=", 1)[0] in managed for value in args)

    async def _perform_crawl(
        body: CrawlRequest,
        *,
        container: ServiceContainer,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        base_key = _validate_idempotency_key(idempotency_key)
        if base_key is not None:
            existing = container.db.get_crawl_batch_by_idempotency(base_key)
            if existing is not None:
                return existing, False
        try:
            container.gallery.validate_args(body.extra_args)
            validate_discovery_args(body.discovery_extra_args)
            if _range_argument_present(body.extra_args):
                raise ValueError("图片范围参数由单地址并发规划器管理")

            canonical_sources: list[tuple[str, Any]] = []
            seen_sites: set[str] = set()
            for source in body.sources:
                site = search_site(source.site).site
                if site in seen_sites:
                    raise ValueError(f"sources 重复配置来源: {site}")
                seen_sites.add(site)
                canonical_sources.append((site, source))

            batch_id = str(uuid.uuid4())
            output_dir = container.settings.task_output_dir(body.output_dir, f"batch-{batch_id}")
            flattened: list[dict[str, Any]] = []
            for source_order, (site, source) in enumerate(canonical_sources):
                policy = container.policy_for(site)

                def source_value(name: str):
                    if name in source.model_fields_set:
                        return getattr(source, name)
                    return getattr(body, name)

                task_args = [*body.extra_args, *source.extra_args]
                discovery_args = [*body.discovery_extra_args, *source.discovery_extra_args]
                container.gallery.validate_args(task_args)
                validate_discovery_args(discovery_args)
                if _range_argument_present(task_args):
                    raise ValueError("图片范围参数由单地址并发规划器管理")
                credentials_ref, cookies_value, config_value = _managed_request_credentials(
                    container,
                    site,
                    credentials_ref=source_value("credentials_ref"),
                    cookies_file=source_value("cookies_file"),
                    config_file=source_value("config_file"),
                )
                cookies, config_file = _allowed_request_files(
                    container,
                    cookies_file=cookies_value,
                    config_file=config_value,
                )
                mode = source_value("proxy_mode") or policy.proxy_mode
                max_attempts = source_value("max_attempts") or (policy.retry_limit + 1)
                priority = source.priority if "priority" in source.model_fields_set else body.priority
                timeout_seconds = (
                    source.timeout_seconds
                    if "timeout_seconds" in source.model_fields_set
                    else body.timeout_seconds
                )
                for address_order, address in enumerate(source.addresses):
                    url = canonical_gallery_address(site, address.url)
                    await asyncio.to_thread(
                        _validate_network_target,
                        url,
                        container.settings.server.allow_private_targets,
                    )
                    site_info = await asyncio.to_thread(container.resolver.resolve, url)
                    if site_info.supported:
                        _validate_site_match(site, site_info.site)
                    if site == "exhentai" and not re.search(
                        r"https?://(?:e-|ex)hentai\.org/g/\d+/[0-9a-f]{10}/?",
                        url,
                        re.I,
                    ):
                        raise ValueError("EH 来源地址必须是具体画廊 /g/GID/TOKEN/")
                    address_args = [*task_args, *address.extra_args]
                    container.gallery.validate_args(address_args)
                    if _range_argument_present(address_args):
                        raise ValueError("图片范围参数由单地址并发规划器管理")
                    flattened.append(
                        {
                            "id": str(uuid.uuid5(uuid.UUID(batch_id), f"{source_order}:{address_order}:{url}")),
                            "site": site,
                            "source_order": source_order,
                            "address_order": address_order,
                            "url": url,
                            "label": address.label or "",
                            "address_type": address.address_type or "",
                            "proxy_mode": mode,
                            "max_attempts": max_attempts,
                            "priority": priority,
                            "credentials_ref": credentials_ref,
                            "cookies_file": str(cookies) if cookies else None,
                            "config_file": str(config_file) if config_file else None,
                            "extra_args": address_args,
                            "discovery_args": discovery_args,
                            "timeout_seconds": timeout_seconds,
                        }
                    )
            batch_id, created = container.db.create_crawl_batch(
                {
                    "id": batch_id,
                    "output_dir": str(output_dir),
                    "concurrency": min(
                        body.concurrency,
                        container.settings.scheduler.max_concurrent_tasks,
                    ),
                    "max_tasks": body.max_tasks,
                },
                flattened,
                idempotency_key=base_key,
            )
            container.ordered_crawls.notify()
            result = container.db.get_crawl_batch(batch_id)
            if result is None:
                raise RuntimeError("顺序爬取批次创建后读取失败")
            result["created"] = created
            result["requested_concurrency"] = body.concurrency
            result["effective_concurrency"] = min(
                body.concurrency,
                container.settings.scheduler.max_concurrent_tasks,
            )
            return result, created
        except ValueError as exc:
            raise ApiError(422, "invalid_crawl", str(exc)) from exc

    @api.post("/crawls")
    async def create_crawl(
        body: CrawlRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        container: ServiceContainer = Depends(get_service),
    ):
        result, created = await _perform_crawl(
            body,
            container=container,
            idempotency_key=idempotency_key,
        )
        return JSONResponse(status_code=202 if created else 200, content=result)

    @api.get("/crawls")
    async def list_crawls(
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        container: ServiceContainer = Depends(get_service),
    ):
        return {
            "items": container.db.list_crawl_batches(limit=limit, offset=offset),
            "limit": limit,
            "offset": offset,
        }

    @api.get("/crawls/{batch_id}")
    async def get_crawl(batch_id: str, container: ServiceContainer = Depends(get_service)):
        batch = container.db.get_crawl_batch(batch_id)
        if batch is None:
            raise ApiError(404, "crawl_not_found", "爬取批次不存在")
        return batch

    @api.get("/crawls/{batch_id}/tasks")
    async def list_crawl_tasks(
        batch_id: str,
        address_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        container: ServiceContainer = Depends(get_service),
    ):
        if container.db.get_crawl_batch(batch_id) is None:
            raise ApiError(404, "crawl_not_found", "爬取批次不存在")
        return {
            "items": container.db.list_crawl_tasks(
                batch_id,
                address_id=address_id,
                limit=limit,
                offset=offset,
            ),
            "limit": limit,
            "offset": offset,
        }

    @api.post("/crawls/{batch_id}/cancel")
    async def cancel_crawl(batch_id: str, container: ServiceContainer = Depends(get_service)):
        batch, task_ids = container.db.request_cancel_crawl_batch(batch_id)
        if batch is None:
            raise ApiError(404, "crawl_not_found", "爬取批次不存在")
        for task_id in task_ids:
            await container.scheduler.cancel(task_id)
        container.ordered_crawls.notify()
        container.db.finish_crawl_batch_if_ready(batch_id)
        return container.db.get_crawl_batch(batch_id)

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
        return {
            "tasks": container.scheduler.active_summary(),
            "ordered_crawls": container.ordered_crawls.status(),
        }

    app.include_router(api)
    return app
