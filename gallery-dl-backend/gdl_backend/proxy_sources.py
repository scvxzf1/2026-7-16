from __future__ import annotations

import base64
import binascii
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit

import requests
import yaml


DIRECT_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}
TUNNEL_SCHEMES = {
    "vless",
    "vmess",
    "ss",
    "trojan",
    "hysteria2",
    "hy2",
    "anytls",
    "mieru",
}
MAX_SUBSCRIPTION_BYTES = 8 * 1024 * 1024
MAX_BASE64_DEPTH = 2


@dataclass(slots=True)
class ParsedProxyNode:
    raw: str
    scheme: str
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    name: str = ""
    endpoint: str = ""
    usable: bool = False
    note: str = ""
    core_config: dict[str, object] = field(default_factory=dict, repr=False)


def _fragment_name(raw: str) -> str:
    if "#" not in raw:
        return ""
    return unquote(raw.rsplit("#", 1)[1]).strip()


def _proxy_url(scheme: str, host: str, port: int, username: str = "", password: str = "") -> str:
    host_text = f"[{host}]" if ":" in host and not host.startswith("[") else host
    auth = ""
    if username or password:
        auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{host_text}:{int(port)}"


def _valid_host_port(host: str, port: int) -> bool:
    return bool(host and not any(char.isspace() for char in host) and 1 <= int(port) <= 65535)


def _parse_host_port_credentials(line: str) -> ParsedProxyNode | None:
    if "://" in line or line.startswith("-") or line.count(":") < 3:
        return None
    host, port_text, username, password = line.split(":", 3)
    if not port_text.isdigit():
        return None
    port = int(port_text)
    if not _valid_host_port(host, port):
        return None
    return ParsedProxyNode(
        raw=line,
        scheme="http",
        host=host,
        port=port,
        username=username,
        password=password,
        endpoint=_proxy_url("http", host, port, username, password),
        usable=True,
    )


def parse_proxy_line(raw: str) -> ParsedProxyNode | None:
    line = str(raw or "").strip().strip("\ufeff")
    if not line or line.startswith("#"):
        return None
    credential_node = _parse_host_port_credentials(line)
    if credential_node is not None:
        return credential_node
    if "://" not in line and line.count(":") == 1:
        host, port_text = line.rsplit(":", 1)
        if port_text.isdigit() and _valid_host_port(host, int(port_text)):
            return ParsedProxyNode(
                raw=line,
                scheme="http",
                host=host,
                port=int(port_text),
                endpoint=_proxy_url("http", host, int(port_text)),
                usable=True,
            )
    scheme = line.split(":", 1)[0].lower() if ":" in line else ""
    if scheme not in DIRECT_SCHEMES | TUNNEL_SCHEMES:
        return None
    name = _fragment_name(line)
    if scheme in TUNNEL_SCHEMES:
        parsed = urlsplit(line)
        try:
            port = int(parsed.port or 0)
        except ValueError:
            port = 0
        return ParsedProxyNode(
            raw=line,
            scheme=scheme,
            host=str(parsed.hostname or ""),
            port=port,
            name=name,
            usable=False,
            note="tunnel protocol requires a transport core",
        )
    parsed = urlsplit(line)
    host = str(parsed.hostname or "")
    try:
        port = int(parsed.port or (443 if scheme == "https" else 80))
    except ValueError:
        return None
    if not _valid_host_port(host, port):
        return None
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if scheme.startswith("socks") and (username or password):
        return ParsedProxyNode(
            raw=line,
            scheme=scheme,
            host=host,
            port=port,
            username=username,
            password=password,
            name=name,
            usable=False,
            note="authenticated SOCKS is excluded from child process arguments",
        )
    return ParsedProxyNode(
        raw=line,
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=password,
        name=name,
        endpoint=_proxy_url(scheme, host, port, username, password),
        usable=True,
    )


def _parse_clash_proxy(item: object) -> ParsedProxyNode | None:
    if not isinstance(item, dict):
        return None
    scheme = str(item.get("type") or "").strip().lower()
    host = str(item.get("server") or "").strip()
    try:
        port = int(item.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    name = str(item.get("name") or "").strip()
    if scheme not in DIRECT_SCHEMES | TUNNEL_SCHEMES or not _valid_host_port(host, port):
        return None
    if scheme in TUNNEL_SCHEMES:
        return ParsedProxyNode(
            raw=f"{scheme}://{host}:{port}#{name}",
            scheme=scheme,
            host=host,
            port=port,
            name=name,
            usable=False,
            note="tunnel protocol requires a transport core",
            core_config=dict(item),
        )
    username = str(item.get("username") or "")
    password = str(item.get("password") or "")
    if scheme.startswith("socks") and (username or password):
        return ParsedProxyNode(
            raw=f"{scheme}://{host}:{port}#{name}",
            scheme=scheme,
            host=host,
            port=port,
            username=username,
            password=password,
            name=name,
            usable=False,
            note="authenticated SOCKS is excluded from child process arguments",
        )
    endpoint = _proxy_url(scheme, host, port, username, password)
    return ParsedProxyNode(
        raw=endpoint,
        scheme=scheme,
        host=host,
        port=port,
        username=username,
        password=password,
        name=name,
        endpoint=endpoint,
        usable=True,
    )


def _decode_base64_subscription(text: str) -> str:
    compact = "".join(text.split())
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9_+/=-]+", compact):
        return ""
    padded = compact + "=" * (-len(compact) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder(padded).decode("utf-8-sig").strip()
        except (ValueError, UnicodeDecodeError, binascii.Error):
            continue
        if "://" in decoded or "\n" in decoded or decoded.lstrip().startswith("proxies:"):
            return decoded
    return ""


def parse_subscription_text(text: str, *, _depth: int = 0) -> list[ParsedProxyNode]:
    raw = str(text or "").strip()
    if not raw:
        return []
    if len(raw.encode("utf-8")) > MAX_SUBSCRIPTION_BYTES:
        raise ValueError("订阅内容超过 8 MiB 上限")
    decoded = _decode_base64_subscription(raw) if _depth < MAX_BASE64_DEPTH else ""
    if decoded:
        return parse_subscription_text(decoded, _depth=_depth + 1)
    if raw.lstrip().startswith("proxies:") or "\nproxies:" in raw[:4000]:
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError:
            document = None
        if isinstance(document, dict) and isinstance(document.get("proxies"), list):
            return [node for item in document["proxies"] if (node := _parse_clash_proxy(item))]
    return [node for line in raw.splitlines() if (node := parse_proxy_line(line))]


def fetch_subscriptions(
    urls: Iterable[str],
    *,
    timeout: float,
    max_workers: int = 8,
) -> tuple[list[ParsedProxyNode], list[str]]:
    clean_urls = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
    if not clean_urls:
        return [], []
    for url in clean_urls:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("订阅地址必须使用 http:// 或 https://")

    def fetch(url: str) -> tuple[str, list[ParsedProxyNode], str]:
        session = requests.Session()
        session.trust_env = False
        session.max_redirects = 5
        try:
            with session.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "gallery-dl-backend/1.0"},
                stream=True,
            ) as response:
                response.raise_for_status()
                final_url = urlsplit(response.url)
                if final_url.scheme.lower() not in {"http", "https"} or not final_url.hostname:
                    raise ValueError("订阅重定向地址必须使用 http:// 或 https://")
                declared = response.headers.get("Content-Length", "").strip()
                if declared.isdigit() and int(declared) > MAX_SUBSCRIPTION_BYTES:
                    raise ValueError("订阅响应超过 8 MiB 上限")
                payload = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    payload.extend(chunk)
                    if len(payload) > MAX_SUBSCRIPTION_BYTES:
                        raise ValueError("订阅响应超过 8 MiB 上限")
                encoding = response.encoding or "utf-8-sig"
                text = bytes(payload).decode(encoding, errors="replace")
                return url, parse_subscription_text(text), ""
        except Exception as exc:
            return url, [], str(exc) or exc.__class__.__name__
        finally:
            session.close()

    nodes: list[ParsedProxyNode] = []
    warnings: list[str] = []
    workers = max(1, min(int(max_workers), len(clean_urls), 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, url): url for url in clean_urls}
        for future in as_completed(futures):
            url, parsed_nodes, error = future.result()
            if error:
                warnings.append(f"订阅拉取失败: {url} -> {error}")
            else:
                nodes.extend(parsed_nodes)
    if warnings and len(warnings) == len(clean_urls):
        raise ValueError("全部订阅地址拉取失败")
    return nodes, warnings
