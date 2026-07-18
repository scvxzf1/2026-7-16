from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import socket
import subprocess
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .proxy_sources import ParsedProxyNode
from .redaction import redact_text


@dataclass(frozen=True, slots=True)
class CoreEndpoint:
    id: str
    name: str
    source_protocol: str
    source_host: str
    local_http: str


@dataclass(slots=True)
class _PreparedNode:
    endpoint: CoreEndpoint
    proxy: dict[str, Any] = field(repr=False)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_core_binary(
    explicit: Path | str | None,
    expected_sha256: str = "",
) -> Path:
    expected = str(expected_sha256 or "").strip().lower()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    for name in ("proxy-core", "proxy-core.exe", "mihomo", "mihomo.exe", "verge-mihomo"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            if expected:
                actual = _file_sha256(resolved)
                if not hmac.compare_digest(actual, expected):
                    raise RuntimeError(
                        "代理传输核心 SHA-256 校验失败: "
                        f"expected={expected}, actual={actual}"
                    )
            return resolved
    raise FileNotFoundError("代理传输核心文件不存在，请检查 proxy.transport_core_binary")


def _restrict_sensitive_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    username = str(os.environ.get("USERNAME") or "").strip()
    principal = f"{domain}\\{username}" if domain and username else username
    if not principal:
        raise RuntimeError("读取当前 Windows 用户名失败，未写入代理核心敏感配置")
    result = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{principal}:(F)",
        ],
        capture_output=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        raise RuntimeError(f"代理核心敏感文件 ACL 设置失败: {redact_text(output, limit=500)}")


def _unique_name(raw: str, index: int, used: set[str]) -> str:
    base = " ".join(str(raw or f"node-{index + 1}").replace("\x00", "").split())[:180]
    if not base:
        base = f"node-{index + 1}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base[:160]}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _prepare_nodes(nodes: Iterable[ParsedProxyNode]) -> list[_PreparedNode]:
    prepared: list[_PreparedNode] = []
    used_names: set[str] = set()
    for index, node in enumerate(nodes):
        source = deepcopy(node.core_config)
        if not isinstance(source, dict):
            continue
        protocol = str(source.get("type") or node.scheme or "").strip().lower()
        host = str(source.get("server") or node.host or "").strip()
        try:
            port = int(source.get("port") or node.port or 0)
        except (TypeError, ValueError):
            port = 0
        if not protocol or not host or not 1 <= port <= 65535:
            continue
        name = _unique_name(str(source.get("name") or node.name), index, used_names)
        source["name"] = name
        source["type"] = protocol
        source["server"] = host
        source["port"] = port
        identity = yaml.safe_dump(source, allow_unicode=True, sort_keys=True)
        node_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        prepared.append(
            _PreparedNode(
                endpoint=CoreEndpoint(
                    id=node_id,
                    name=name,
                    source_protocol=protocol,
                    source_host=host,
                    local_http="",
                ),
                proxy=source,
            )
        )
    return prepared


def build_transport_config(
    nodes: Iterable[ParsedProxyNode],
    *,
    listen_host: str,
    base_port: int,
) -> tuple[dict[str, Any], list[CoreEndpoint]]:
    host = str(listen_host or "127.0.0.1").strip() or "127.0.0.1"
    prepared = _prepare_nodes(nodes)
    proxies: list[dict[str, Any]] = []
    listeners: list[dict[str, Any]] = []
    endpoints: list[CoreEndpoint] = []
    for index, item in enumerate(prepared):
        port = int(base_port) + index
        if not 1 <= port <= 65535:
            raise ValueError("代理核心监听端口超出范围")
        proxies.append(item.proxy)
        listeners.append(
            {
                "name": f"native-http-{port}",
                "type": "http",
                "listen": host,
                "port": port,
                "proxy": item.proxy["name"],
                "users": [],
            }
        )
        endpoints.append(
            CoreEndpoint(
                id=item.endpoint.id,
                name=item.endpoint.name,
                source_protocol=item.endpoint.source_protocol,
                source_host=item.endpoint.source_host,
                local_http=f"http://{host}:{port}",
            )
        )
    return (
        {
            "allow-lan": False,
            "mode": "rule",
            "log-level": "warning",
            "proxies": proxies,
            "listeners": listeners,
            "rules": ["MATCH,DIRECT"],
        },
        endpoints,
    )


def _port_is_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, int(port)))
        return True
    except OSError:
        return False


def _find_port_block(host: str, preferred: int, count: int) -> int:
    if count <= 0:
        raise ValueError("代理核心节点列表为空")
    starts = [int(preferred)]
    starts.extend(range(max(20000, int(preferred) + 100), 65000 - count, max(100, count + 8)))
    for start in starts:
        if start < 1024 or start + count - 1 > 65535:
            continue
        if all(_port_is_free(host, start + offset) for offset in range(count)):
            return start
    raise RuntimeError("未找到可用的连续本地代理端口")


class TunnelTransportCore:
    def __init__(
        self,
        *,
        binary_path: Path | str | None,
        expected_sha256: str = "",
        runtime_dir: Path,
        base_port: int,
        start_timeout_seconds: float,
        listen_host: str = "127.0.0.1",
    ) -> None:
        self.binary_path = binary_path
        self.expected_sha256 = str(expected_sha256 or "").strip().lower()
        self.runtime_dir = runtime_dir.resolve()
        self.base_port = int(base_port)
        self.start_timeout_seconds = max(1.0, float(start_timeout_seconds))
        self.listen_host = str(listen_host or "127.0.0.1")
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_file: Any = None
        self._endpoints: list[CoreEndpoint] = []
        self._binary: Path | None = None
        self._binary_sha256 = ""
        self._version = ""
        self._config_path = self.runtime_dir / "config.yaml"
        self._log_path = self.runtime_dir / "core.log"
        self._last_error = ""

    @staticmethod
    def _creation_flags() -> int:
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0

    def _read_version(self, binary: Path) -> str:
        try:
            result = subprocess.run(
                [str(binary), "-v"],
                capture_output=True,
                timeout=5,
                creationflags=self._creation_flags(),
                check=False,
            )
            output = (result.stdout or result.stderr).decode("utf-8", errors="replace")
            return " ".join(output.split())[:200]
        except Exception:
            return ""

    def _validate_config(self, binary: Path) -> None:
        result = subprocess.run(
            [str(binary), "-t", "-f", str(self._config_path), "-d", str(self.runtime_dir)],
            capture_output=True,
            timeout=30,
            creationflags=self._creation_flags(),
            check=False,
        )
        if result.returncode != 0:
            output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            raise RuntimeError(f"代理核心配置校验失败: {redact_text(output, limit=1000)}")

    def _wait_ready(self, process: subprocess.Popen[bytes], endpoints: list[CoreEndpoint]) -> None:
        ports = [int(endpoint.local_http.rsplit(":", 1)[1]) for endpoint in endpoints]
        deadline = time.time() + self.start_timeout_seconds
        pending = set(ports)
        while pending and time.time() < deadline:
            if process.poll() is not None:
                break
            for port in list(pending):
                try:
                    with socket.create_connection((self.listen_host, port), timeout=0.15):
                        pending.discard(port)
                except OSError:
                    pass
            if pending:
                time.sleep(0.05)
        if pending or process.poll() is not None:
            raise RuntimeError(
                f"代理核心启动失败，{len(pending)} 个监听端口尚未就绪；日志: {self._log_path}"
            )

    def start(self, nodes: Iterable[ParsedProxyNode]) -> list[CoreEndpoint]:
        with self._lifecycle_lock:
            try:
                return self._start(nodes)
            except Exception as exc:
                with self._lock:
                    self._last_error = redact_text(str(exc), limit=500)
                raise

    def _start(self, nodes: Iterable[ParsedProxyNode]) -> list[CoreEndpoint]:
        self._stop()
        binary = resolve_core_binary(self.binary_path, self.expected_sha256)
        binary_sha256 = self.expected_sha256 or _file_sha256(binary)
        node_list = list(nodes)
        prepared = _prepare_nodes(node_list)
        base_port = _find_port_block(self.listen_host, self.base_port, len(prepared))
        config, endpoints = build_transport_config(
            node_list,
            listen_host=self.listen_host,
            base_port=base_port,
        )
        if not endpoints:
            raise ValueError("订阅中未发现代理核心可加载的节点")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        _restrict_sensitive_file(self._config_path)
        self._validate_config(binary)
        self._log_path.touch(exist_ok=True)
        _restrict_sensitive_file(self._log_path)
        log_file = self._log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [str(binary), "-f", str(self._config_path), "-d", str(self.runtime_dir)],
                cwd=str(self.runtime_dir),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=self._creation_flags(),
            )
            self._wait_ready(process, endpoints)
        except Exception:
            if "process" in locals() and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            log_file.close()
            raise
        with self._lock:
            self._process = process
            self._log_file = log_file
            self._endpoints = endpoints
            self._binary = binary
            self._binary_sha256 = binary_sha256
            self._version = self._read_version(binary)
            self._last_error = ""
            self.base_port = base_port
        return list(endpoints)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop()

    def _stop(self) -> None:
        with self._lock:
            process = self._process
            log_file = self._log_file
            self._process = None
            self._log_file = None
            self._endpoints = []
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                pass
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            running = bool(process is not None and process.poll() is None)
            return {
                "enabled": True,
                "running": running,
                "pid": process.pid if running and process is not None else None,
                "version": self._version,
                "binary": self._binary.name if self._binary else "",
                "sha256": self._binary_sha256,
                "listeners": len(self._endpoints),
                "base_port": self.base_port if self._endpoints else None,
                "last_error": self._last_error,
            }
