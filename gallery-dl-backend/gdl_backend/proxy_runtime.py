from __future__ import annotations

import base64
import secrets
import select
import socket
import socketserver
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import unquote, urlsplit


def mask_proxy(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***@" if parsed.username or parsed.password else ""
        return f"{parsed.scheme}://{auth}{host}{port}"
    except Exception:
        return "[REDACTED_PROXY]"


@dataclass(frozen=True, slots=True)
class PoolLease:
    token: str
    endpoint: str
    owner: str


class NativeProxyPool:
    def __init__(self, endpoints: Iterable[str]) -> None:
        self._endpoints = list(dict.fromkeys(str(item) for item in endpoints if str(item)))
        self._lock = threading.RLock()
        self._leases: dict[str, PoolLease] = {}
        self._endpoint_tokens: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        self._cursor = 0
        self._closed = False

    def endpoints(self) -> list[str]:
        with self._lock:
            return list(self._endpoints)

    def acquire(self, *, owner: str, allowed: set[str] | None = None) -> PoolLease | None:
        with self._lock:
            if self._closed or not self._endpoints:
                return None
            now = time.time()
            allowed_set = allowed if allowed is not None else set(self._endpoints)
            count = len(self._endpoints)
            for offset in range(count):
                index = (self._cursor + offset) % count
                endpoint = self._endpoints[index]
                if endpoint not in allowed_set:
                    continue
                if endpoint in self._endpoint_tokens:
                    continue
                if self._cooldown_until.get(endpoint, 0.0) > now:
                    continue
                token = secrets.token_urlsafe(24)
                lease = PoolLease(token=token, endpoint=endpoint, owner=str(owner)[:120])
                self._leases[token] = lease
                self._endpoint_tokens[endpoint] = token
                self._cursor = (index + 1) % count
                return lease
            return None

    def release(
        self,
        token: str,
        *,
        success: bool | None = None,
        cooldown_seconds: float = 0.0,
    ) -> bool:
        with self._lock:
            lease = self._leases.pop(str(token), None)
            if lease is None:
                return False
            if self._endpoint_tokens.get(lease.endpoint) == lease.token:
                self._endpoint_tokens.pop(lease.endpoint, None)
            if success is True:
                self._cooldown_until.pop(lease.endpoint, None)
            elif success is False:
                self._cooldown_until[lease.endpoint] = time.time() + max(0.0, cooldown_seconds)
            return True

    def mark(self, endpoint: str, *, success: bool, cooldown_seconds: float = 0.0) -> None:
        with self._lock:
            if endpoint not in self._endpoints:
                return
            if success:
                self._cooldown_until.pop(endpoint, None)
            else:
                self._cooldown_until[endpoint] = time.time() + max(0.0, cooldown_seconds)

    def status(self) -> dict[str, dict[str, object]]:
        with self._lock:
            now = time.time()
            return {
                endpoint: {
                    "leased": endpoint in self._endpoint_tokens,
                    "cooldown_left": max(0.0, self._cooldown_until.get(endpoint, 0.0) - now),
                }
                for endpoint in self._endpoints
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._leases.clear()
            self._endpoint_tokens.clear()


@dataclass(frozen=True, slots=True)
class _UpstreamHTTPProxy:
    scheme: str
    host: str
    port: int
    username: str
    password: str

    @classmethod
    def parse(cls, endpoint: str) -> "_UpstreamHTTPProxy":
        parsed = urlsplit(endpoint)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise ValueError("本地认证转发器仅接收 HTTP/HTTPS 上游代理")
        return cls(
            scheme=scheme,
            host=parsed.hostname,
            port=int(parsed.port),
            username=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
        )

    @property
    def authorization(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    def connect(self, *, timeout: float) -> socket.socket:
        upstream = socket.create_connection((self.host, self.port), timeout=timeout)
        if self.scheme == "https":
            try:
                upstream = ssl.create_default_context().wrap_socket(
                    upstream,
                    server_hostname=self.host,
                )
            except Exception:
                upstream.close()
                raise
        return upstream


def _read_headers(sock: socket.socket, *, limit: int = 65536) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("HTTP proxy header too large")
    marker = data.find(b"\r\n\r\n")
    if marker < 0:
        return bytes(data), b""
    end = marker + 4
    return bytes(data[:end]), bytes(data[end:])


def _with_proxy_auth(header_block: bytes, authorization: str, *, connect: bool) -> bytes:
    text = header_block.decode("iso-8859-1")
    lines = text.split("\r\n")
    output = [lines[0]]
    for line in lines[1:]:
        lower = line.lower()
        if (
            lower.startswith("proxy-authorization:")
            or lower.startswith("proxy-connection:")
            or lower.startswith("connection:")
        ):
            continue
        if line:
            output.append(line)
    output.append(f"Proxy-Authorization: {authorization}")
    output.append("Proxy-Connection: Keep-Alive" if connect else "Connection: close")
    output.extend(["", ""])
    return "\r\n".join(output).encode("iso-8859-1")


def _connect_response_succeeded(header_block: bytes) -> bool:
    status_line = header_block.split(b"\r\n", 1)[0]
    parts = status_line.split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return False
    return 200 <= int(parts[1]) <= 299


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while sockets:
        readable, _, _ = select.select(sockets, [], [], 30.0)
        if not readable:
            return
        for source in readable:
            target = right if source is left else left
            try:
                chunk = source.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            try:
                target.sendall(chunk)
            except OSError:
                return


class _ForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address: tuple[str, int], upstream: _UpstreamHTTPProxy):
        self.upstream = upstream
        super().__init__(address, _ForwardHandler, bind_and_activate=True)


class _ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client = self.request
        client.settimeout(30.0)
        try:
            header, extra = _read_headers(client)
            if not header:
                return
            first_line = header.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
            method = first_line.split(" ", 1)[0].upper()
            upstream_config = self.server.upstream
            upstream = upstream_config.connect(timeout=15.0)
            upstream.settimeout(30.0)
            try:
                upstream.sendall(
                    _with_proxy_auth(
                        header,
                        upstream_config.authorization,
                        connect=method == "CONNECT",
                    )
                )
                if extra:
                    upstream.sendall(extra)
                if method == "CONNECT":
                    response_header, response_extra = _read_headers(upstream)
                    client.sendall(response_header)
                    if response_extra:
                        client.sendall(response_extra)
                    if not _connect_response_succeeded(response_header):
                        return
                _relay(client, upstream)
            finally:
                upstream.close()
        except (OSError, ValueError):
            return


class LocalHTTPForwarder:
    def __init__(self, upstream_endpoint: str) -> None:
        self.upstream = _UpstreamHTTPProxy.parse(upstream_endpoint)
        if not (self.upstream.username or self.upstream.password):
            raise ValueError("上游代理不含认证信息")
        self._server: _ForwardServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def local_url(self) -> str:
        if self._server is None:
            return ""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        if self._server is not None:
            return self.local_url
        server = _ForwardServer(("127.0.0.1", 0), self.upstream)
        thread = threading.Thread(target=server.serve_forever, name="proxy-forwarder", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        return self.local_url

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
