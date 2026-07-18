from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = PROJECT_DIR.parent


def _path(value: str | os.PathLike[str] | None, base: Path, default: Path) -> Path:
    if value in (None, ""):
        path = default
    else:
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not path.is_absolute():
            path = base / path
    return path.resolve()


def _paths(values: list[str] | None, base: Path, defaults: list[Path]) -> list[Path]:
    if not values:
        return [p.resolve() for p in defaults]
    return [_path(value, base, base) for value in values]


@dataclass(slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8787
    api_key: str = ""
    cors_origins: list[str] = field(default_factory=list)
    allow_private_targets: bool = False


@dataclass(slots=True)
class GallerySettings:
    repo_path: Path = field(default_factory=lambda: (WORKSPACE_DIR / "gallery-dl-codeberg").resolve())
    cache_file: Path = field(
        default_factory=lambda: (PROJECT_DIR / "credentials" / "managed" / "gallery-dl-cache.sqlite3").resolve()
    )
    python_executable: str = sys.executable
    migrate_default_auth: bool = True
    default_http_timeout: float = 30.0
    default_retries: int = 2
    terminate_grace_seconds: float = 5.0
    max_log_line_chars: int = 4000
    forbidden_args: list[str] = field(
        default_factory=lambda: [
            "--proxy",
            "--destination",
            "--directory",
            "--config-ignore",
            "--ignore-config",
            "--server",
            "--write-log",
            "--input-file",
            "--exec",
            "--exec-after",
            "--option",
            "--config",
            "--config-json",
            "--config-yaml",
            "--config-toml",
            "--cookies",
            "--cookies-export",
            "--cookies-from-browser",
            "--cache-file",
            "--username",
            "--password",
            "-d",
            "-D",
            "-S",
            "-o",
            "-c",
            "-C",
            "-u",
            "-p",
        ]
    )


@dataclass(slots=True)
class AuthSettings:
    chrome_executable: str = ""
    browser_login_timeout_seconds: float = 900.0
    browser_poll_interval_seconds: float = 1.0


@dataclass(slots=True)
class ProxySettings:
    enabled: bool = True
    auto_start: bool = True
    engine: str = "native"
    subscription_urls: list[str] = field(default_factory=list)
    node_file: Path | None = None
    inline_nodes: list[str] = field(default_factory=list)
    allow_socks: bool = True
    max_nodes: int = 50
    probe_url: str = "https://example.com/"
    probe_timeout_seconds: float = 5.0
    probe_workers: int = 32
    health_interval_seconds: float = 60.0
    fail_cooldown_seconds: float = 30.0
    subscription_timeout_seconds: float = 20.0
    transport_core_enabled: bool = True
    transport_core_binary: Path = field(
        default_factory=lambda: (PROJECT_DIR / "bin" / "proxy-core.exe").resolve()
    )
    transport_core_sha256: str = "a3799f2d75c623a7c6d307e1faf88269e24dd746c59df3e9f1c84d5cfbff6c92"
    transport_core_base_port: int = 29000
    transport_core_start_timeout_seconds: float = 15.0


@dataclass(slots=True)
class SchedulerSettings:
    max_concurrent_tasks: int = 20
    poll_interval_seconds: float = 0.5
    shutdown_grace_seconds: float = 15.0
    max_logs_per_task: int = 5000
    retry_jitter_seconds: float = 0.5


DEFAULT_SITE_POLICY: dict[str, Any] = {
    "max_concurrency": 20,
    "retry_limit": 2,
    "backoff_base_seconds": 2.0,
    "proxy_mode": "prefer",
    "probe_url": None,
    "probe_before_use": False,
    "node_tags": [],
    "http_timeout": 30.0,
    "gallery_retries": 2,
    "task_timeout_seconds": 0.0,
    "extra_args": [],
}


@dataclass(slots=True)
class AppSettings:
    project_dir: Path = PROJECT_DIR
    workspace_dir: Path = WORKSPACE_DIR
    runtime_dir: Path = field(default_factory=lambda: (PROJECT_DIR / "runtime").resolve())
    database_path: Path = field(default_factory=lambda: (PROJECT_DIR / "runtime" / "backend.sqlite3").resolve())
    default_output_root: Path = field(default_factory=lambda: (PROJECT_DIR / "runtime" / "downloads").resolve())
    allowed_output_roots: list[Path] = field(default_factory=lambda: [(PROJECT_DIR / "runtime" / "downloads").resolve()])
    allowed_config_roots: list[Path] = field(default_factory=lambda: [(PROJECT_DIR / "credentials").resolve()])
    allowed_cookie_roots: list[Path] = field(default_factory=lambda: [(PROJECT_DIR / "credentials").resolve()])
    server: ServerSettings = field(default_factory=ServerSettings)
    gallery: GallerySettings = field(default_factory=GallerySettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    proxy: ProxySettings = field(default_factory=ProxySettings)
    scheduler: SchedulerSettings = field(default_factory=SchedulerSettings)
    default_site_policy: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SITE_POLICY))
    config_path: Path | None = None

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "AppSettings":
        requested = path or os.environ.get("GDL_BACKEND_CONFIG")
        config_path = Path(requested).expanduser().resolve() if requested else (PROJECT_DIR / "config.json")
        data: dict[str, Any] = {}
        if config_path.is_file():
            data = json.loads(config_path.read_text(encoding="utf-8"))
        base = config_path.parent if config_path else PROJECT_DIR

        runtime = _path(data.get("runtime_dir"), base, PROJECT_DIR / "runtime")
        database = _path(data.get("database_path"), base, runtime / "backend.sqlite3")
        output_root = _path(data.get("default_output_root"), base, runtime / "downloads")

        server_data = dict(data.get("server") or {})
        gallery_data = dict(data.get("gallery") or {})
        auth_data = dict(data.get("auth") or {})
        proxy_data = dict(data.get("proxy") or {})
        scheduler_data = dict(data.get("scheduler") or {})

        server = ServerSettings(
            host=str(os.environ.get("GDL_BACKEND_HOST", server_data.get("host", "127.0.0.1"))),
            port=int(os.environ.get("GDL_BACKEND_PORT", server_data.get("port", 8787))),
            api_key=str(os.environ.get("GDL_BACKEND_API_KEY", server_data.get("api_key", ""))),
            cors_origins=[str(x) for x in server_data.get("cors_origins", [])],
            allow_private_targets=bool(server_data.get("allow_private_targets", False)),
        )
        gallery = GallerySettings(
            repo_path=_path(gallery_data.get("repo_path"), base, WORKSPACE_DIR / "gallery-dl-codeberg"),
            cache_file=_path(
                gallery_data.get("cache_file"),
                base,
                PROJECT_DIR / "credentials" / "managed" / "gallery-dl-cache.sqlite3",
            ),
            python_executable=str(gallery_data.get("python_executable") or sys.executable),
            migrate_default_auth=bool(gallery_data.get("migrate_default_auth", True)),
            default_http_timeout=float(gallery_data.get("default_http_timeout", 30.0)),
            default_retries=int(gallery_data.get("default_retries", 2)),
            terminate_grace_seconds=float(gallery_data.get("terminate_grace_seconds", 5.0)),
            max_log_line_chars=int(gallery_data.get("max_log_line_chars", 4000)),
            forbidden_args=[str(x) for x in gallery_data.get("forbidden_args", GallerySettings().forbidden_args)],
        )
        auth = AuthSettings(
            chrome_executable=str(auth_data.get("chrome_executable") or ""),
            browser_login_timeout_seconds=float(
                auth_data.get("browser_login_timeout_seconds", 900.0)
            ),
            browser_poll_interval_seconds=float(
                auth_data.get("browser_poll_interval_seconds", 1.0)
            ),
        )
        node_file_value = proxy_data.get("node_file")
        transport_core_binary = proxy_data.get("transport_core_binary", "bin/proxy-core.exe")
        proxy = ProxySettings(
            enabled=bool(proxy_data.get("enabled", True)),
            auto_start=bool(proxy_data.get("auto_start", True)),
            engine=str(proxy_data.get("engine", "native")).strip().lower(),
            subscription_urls=[str(x).strip() for x in proxy_data.get("subscription_urls", []) if str(x).strip()],
            node_file=_path(node_file_value, base, base) if node_file_value else None,
            inline_nodes=[str(x).strip() for x in proxy_data.get("inline_nodes", []) if str(x).strip()],
            allow_socks=bool(proxy_data.get("allow_socks", True)),
            max_nodes=int(proxy_data.get("max_nodes", 50)),
            probe_url=str(proxy_data.get("probe_url", "https://example.com/")),
            probe_timeout_seconds=float(proxy_data.get("probe_timeout_seconds", 5.0)),
            probe_workers=int(proxy_data.get("probe_workers", 32)),
            health_interval_seconds=float(proxy_data.get("health_interval_seconds", 60.0)),
            fail_cooldown_seconds=float(proxy_data.get("fail_cooldown_seconds", 30.0)),
            subscription_timeout_seconds=float(proxy_data.get("subscription_timeout_seconds", 20.0)),
            transport_core_enabled=bool(proxy_data.get("transport_core_enabled", True)),
            transport_core_binary=_path(transport_core_binary, base, PROJECT_DIR / "bin" / "proxy-core.exe"),
            transport_core_sha256=str(proxy_data.get("transport_core_sha256", "")).strip().lower()
            or "a3799f2d75c623a7c6d307e1faf88269e24dd746c59df3e9f1c84d5cfbff6c92",
            transport_core_base_port=int(proxy_data.get("transport_core_base_port", 29000)),
            transport_core_start_timeout_seconds=float(
                proxy_data.get("transport_core_start_timeout_seconds", 15.0)
            ),
        )
        scheduler = SchedulerSettings(
            max_concurrent_tasks=max(1, int(scheduler_data.get("max_concurrent_tasks", 20))),
            poll_interval_seconds=max(0.1, float(scheduler_data.get("poll_interval_seconds", 0.5))),
            shutdown_grace_seconds=max(1.0, float(scheduler_data.get("shutdown_grace_seconds", 15.0))),
            max_logs_per_task=max(100, int(scheduler_data.get("max_logs_per_task", 5000))),
            retry_jitter_seconds=max(0.0, float(scheduler_data.get("retry_jitter_seconds", 0.5))),
        )

        policy = dict(DEFAULT_SITE_POLICY)
        policy.update(data.get("default_site_policy") or {})
        settings = cls(
            runtime_dir=runtime,
            database_path=database,
            default_output_root=output_root,
            allowed_output_roots=_paths(data.get("allowed_output_roots"), base, [output_root]),
            allowed_config_roots=_paths(data.get("allowed_config_roots"), base, [PROJECT_DIR / "credentials"]),
            allowed_cookie_roots=_paths(data.get("allowed_cookie_roots"), base, [PROJECT_DIR / "credentials"]),
            server=server,
            gallery=gallery,
            auth=auth,
            proxy=proxy,
            scheduler=scheduler,
            default_site_policy=policy,
            config_path=config_path if config_path.is_file() else None,
        )
        settings.ensure_directories()
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 1 <= int(self.server.port) <= 65535:
            raise ValueError("server.port 超出范围")
        host = self.server.host.strip().lower()
        if host not in {"127.0.0.1", "localhost", "::1"} and not self.server.api_key:
            raise ValueError("监听非回环地址时必须配置 server.api_key")
        if self.proxy.engine != "native":
            raise ValueError("proxy.engine 当前支持 native")
        if self.auth.browser_login_timeout_seconds <= 0:
            raise ValueError("auth.browser_login_timeout_seconds 必须大于 0")
        if self.auth.browser_poll_interval_seconds <= 0:
            raise ValueError("auth.browser_poll_interval_seconds 必须大于 0")
        if not 1 <= int(self.proxy.max_nodes) <= 10000:
            raise ValueError("proxy.max_nodes 必须位于 1..10000")
        if not 1 <= int(self.proxy.probe_workers) <= 64:
            raise ValueError("proxy.probe_workers 必须位于 1..64")
        if self.proxy.probe_timeout_seconds <= 0 or self.proxy.subscription_timeout_seconds <= 0:
            raise ValueError("代理超时必须大于 0")
        if self.proxy.health_interval_seconds <= 0:
            raise ValueError("proxy.health_interval_seconds 必须大于 0")
        if self.proxy.fail_cooldown_seconds <= 0:
            raise ValueError("proxy.fail_cooldown_seconds 必须大于 0")
        if not 1024 <= int(self.proxy.transport_core_base_port) <= 65000:
            raise ValueError("proxy.transport_core_base_port 必须位于 1024..65000")
        if self.proxy.transport_core_start_timeout_seconds <= 0:
            raise ValueError("proxy.transport_core_start_timeout_seconds 必须大于 0")
        digest = self.proxy.transport_core_sha256
        if digest and (len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("proxy.transport_core_sha256 必须是 64 位十六进制 SHA-256")

    def ensure_directories(self) -> None:
        for path in (
            self.runtime_dir,
            self.database_path.parent,
            self.default_output_root,
            self.runtime_dir / "logs",
            self.runtime_dir / "proxy",
            self.gallery.cache_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _inside(path: Path, roots: list[Path]) -> bool:
        resolved = path.resolve()
        return any(resolved == root.resolve() or resolved.is_relative_to(root.resolve()) for root in roots)

    def task_output_dir(self, value: str | None, task_id: str) -> Path:
        if value:
            candidate = Path(os.path.expandvars(os.path.expanduser(value)))
            if not candidate.is_absolute():
                candidate = self.default_output_root / candidate
        else:
            candidate = self.default_output_root / task_id
        candidate = candidate.resolve()
        if not self._inside(candidate, self.allowed_output_roots):
            raise ValueError("输出目录超出 allowed_output_roots")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def allowed_file(self, value: str | None, roots: list[Path], label: str) -> Path | None:
        if not value:
            return None
        candidate = Path(os.path.expandvars(os.path.expanduser(value)))
        if not candidate.is_absolute():
            candidate = self.project_dir / candidate
        candidate = candidate.resolve()
        if not self._inside(candidate, roots):
            raise ValueError(f"{label}超出配置的许可目录")
        if not candidate.is_file():
            raise ValueError(f"{label}不存在或不是文件")
        return candidate

    def public_dict(self) -> dict[str, Any]:
        return {
            "runtime_dir": str(self.runtime_dir),
            "database_path": str(self.database_path),
            "default_output_root": str(self.default_output_root),
            "allowed_output_roots": [str(x) for x in self.allowed_output_roots],
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "api_key_configured": bool(self.server.api_key),
                "cors_origins": list(self.server.cors_origins),
                "allow_private_targets": self.server.allow_private_targets,
            },
            "gallery": {
                "repo_path": str(self.gallery.repo_path),
                "python_executable": self.gallery.python_executable,
                "default_http_timeout": self.gallery.default_http_timeout,
                "default_retries": self.gallery.default_retries,
                "managed_auth_cache": True,
            },
            "auth": {
                "managed_browser": True,
                "chrome_configured": bool(self.auth.chrome_executable.strip()),
                "browser_login_timeout_seconds": self.auth.browser_login_timeout_seconds,
                "browser_poll_interval_seconds": self.auth.browser_poll_interval_seconds,
            },
            "proxy": {
                "enabled": self.proxy.enabled,
                "auto_start": self.proxy.auto_start,
                "engine": self.proxy.engine,
                "subscription_count": len(self.proxy.subscription_urls),
                "node_file": str(self.proxy.node_file) if self.proxy.node_file else None,
                "inline_node_count": len(self.proxy.inline_nodes),
                "allow_socks": self.proxy.allow_socks,
                "max_nodes": self.proxy.max_nodes,
                "probe_url": self.proxy.probe_url,
                "transport_core_enabled": self.proxy.transport_core_enabled,
                "transport_core_binary": str(self.proxy.transport_core_binary),
                "transport_core_sha256": self.proxy.transport_core_sha256,
                "transport_core_base_port": self.proxy.transport_core_base_port,
            },
            "scheduler": asdict(self.scheduler),
            "default_site_policy": dict(self.default_site_policy),
        }
