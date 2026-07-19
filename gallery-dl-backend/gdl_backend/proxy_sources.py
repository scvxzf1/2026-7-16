from __future__ import annotations

import base64
import binascii
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote, urlsplit

import requests
import yaml


DIRECT_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}
TUNNEL_SCHEMES = {
    "vless",
    "vmess",
    "ss",
    "ssr",
    "trojan",
    "hysteria",
    "hysteria2",
    "hy2",
    "tuic",
    "anytls",
    "mieru",
}
MAX_SUBSCRIPTION_BYTES = 8 * 1024 * 1024
MAX_BASE64_DEPTH = 2


@dataclass(slots=True)
class ParsedProxyNode:
    raw: str = field(repr=False)
    scheme: str
    host: str = ""
    port: int = 0
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    name: str = ""
    endpoint: str = field(default="", repr=False)
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


def _decode_base64_value(value: str) -> str:
    compact = "".join(str(value or "").split())
    if not compact:
        return ""
    padded = compact + "=" * (-len(compact) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(padded).decode("utf-8-sig")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            continue
    return ""


def _query(raw: str) -> dict[str, list[str]]:
    return {
        str(key).lower(): [str(item) for item in values]
        for key, values in parse_qs(raw, keep_blank_values=True).items()
    }


def _query_value(values: dict[str, list[str]], *names: str, default: str = "") -> str:
    for name in names:
        items = values.get(name.lower())
        if items:
            return str(items[0])
    return default


def _query_bool(values: dict[str, list[str]], *names: str, default: bool = False) -> bool:
    raw = _query_value(values, *names)
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _query_list(values: dict[str, list[str]], *names: str) -> list[str]:
    raw = _query_value(values, *names)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _number_or_text(value: object) -> int | float | str:
    text = str(value or "").strip()
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def _first_port(value: object) -> int:
    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        match = re.search(r"\d+", str(item or ""))
        if match:
            port = int(match.group(0))
            if 1 <= port <= 65535:
                return port
    return 0


def _mieru_port_range(value: object) -> str:
    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        match = re.fullmatch(r"\s*(\d+)\s*[-:]\s*(\d+)\s*", str(item or ""))
        if not match:
            continue
        start, end = (int(part) for part in match.groups())
        if 1 <= start <= end <= 65535:
            return f"{start}-{end}"
    return ""


def _mieru_transport(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    return str(value or "TCP").strip().upper() or "TCP"


def _mieru_multiplexing(value: object) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_")
    aliases = {
        "NONE": "MULTIPLEXING_OFF",
        "OFF": "MULTIPLEXING_OFF",
        "DISABLED": "MULTIPLEXING_OFF",
        "LOW": "MULTIPLEXING_LOW",
        "MIDDLE": "MULTIPLEXING_MIDDLE",
        "MEDIUM": "MULTIPLEXING_MIDDLE",
        "HIGH": "MULTIPLEXING_HIGH",
    }
    return aliases.get(normalized, normalized)


def _uri_userinfo(parsed) -> str:
    if "@" not in parsed.netloc:
        return ""
    return unquote(parsed.netloc.rsplit("@", 1)[0])


def _core_node(
    raw: str,
    scheme: str,
    config: dict[str, object],
    *,
    fallback_name: str = "",
) -> ParsedProxyNode | None:
    host = str(config.get("server") or "").strip()
    canonical_scheme = str(config.get("type") or scheme).strip().lower()
    port_range = (
        _mieru_port_range(config.get("port-range") or config.get("port_range"))
        if canonical_scheme == "mieru"
        else ""
    )
    try:
        port = int(config.get("port") or _first_port(port_range))
    except (TypeError, ValueError):
        return None
    if not _valid_host_port(host, port):
        return None
    name = str(config.get("name") or fallback_name or f"{scheme}-{host}").strip()
    config["name"] = name
    config["type"] = canonical_scheme
    config["server"] = host
    if port_range:
        config["port-range"] = port_range
        config.pop("port_range", None)
        config.pop("port", None)
    else:
        config["port"] = port
    return ParsedProxyNode(
        raw=f"{scheme}://{host}:{port}#{quote(name, safe='')}",
        scheme=scheme,
        host=host,
        port=port,
        name=name,
        usable=False,
        note="tunnel protocol requires a transport core",
        core_config=config,
    )


def _apply_tls_options(
    config: dict[str, object],
    values: dict[str, list[str]],
    *,
    sni_key: str,
    enabled: bool,
) -> None:
    if enabled:
        config["tls"] = True
    servername = _query_value(values, "sni", "servername", "server-name", "peer")
    if servername:
        config[sni_key] = servername
    if _query_bool(values, "allowinsecure", "insecure", "skip-cert-verify"):
        config["skip-cert-verify"] = True
    fingerprint = _query_value(values, "fp", "fingerprint", "client-fingerprint")
    if fingerprint:
        config["client-fingerprint"] = fingerprint
    alpn = _query_list(values, "alpn")
    if alpn:
        config["alpn"] = alpn


def _apply_transport_options(
    config: dict[str, object],
    values: dict[str, list[str]],
    network: str,
) -> None:
    transport = str(network or "tcp").strip().lower()
    if transport in {"httpupgrade", "http-upgrade"}:
        transport = "httpupgrade"
    elif transport in {"splithttp", "xhttp"}:
        transport = "xhttp"
    config["network"] = transport
    host = _query_value(values, "host")
    path = _query_value(values, "path")
    if transport == "ws":
        options: dict[str, object] = {}
        if path:
            options["path"] = path
        if host:
            options["headers"] = {"Host": host}
        if options:
            config["ws-opts"] = options
    elif transport == "grpc":
        service = _query_value(values, "servicename", "service-name", "grpc-service-name")
        if service:
            config["grpc-opts"] = {"grpc-service-name": service}
    elif transport == "httpupgrade":
        options = {}
        if path:
            options["path"] = path
        if host:
            options["headers"] = {"Host": host}
        if options:
            config["http-upgrade-opts"] = options
    elif transport == "xhttp":
        options = {}
        if path:
            options["path"] = path
        if host:
            options["host"] = host
        mode = _query_value(values, "mode")
        if mode:
            options["mode"] = mode
        if options:
            config["xhttp-opts"] = options
    elif transport in {"http", "h2"}:
        options = {}
        if path:
            options["path"] = [path] if transport == "http" else path
        if host:
            if transport == "http":
                options["headers"] = {"Host": [host]}
            else:
                options["host"] = [host]
        if options:
            config["http-opts" if transport == "http" else "h2-opts"] = options


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


def _parse_vless_uri(line: str) -> ParsedProxyNode | None:
    parsed = urlsplit(line)
    try:
        port = int(parsed.port or 0)
    except ValueError:
        return None
    uuid = unquote(parsed.username or "").strip()
    if not uuid:
        return None
    values = _query(parsed.query)
    security = _query_value(values, "security").lower()
    config: dict[str, object] = {
        "name": _fragment_name(line),
        "type": "vless",
        "server": str(parsed.hostname or ""),
        "port": port,
        "uuid": uuid,
        "udp": _query_bool(values, "udp", default=True),
    }
    encryption = _query_value(values, "encryption")
    if encryption:
        config["encryption"] = encryption
    flow = _query_value(values, "flow")
    if flow:
        config["flow"] = flow
    _apply_tls_options(
        config,
        values,
        sni_key="servername",
        enabled=security in {"tls", "reality"},
    )
    public_key = _query_value(values, "pbk", "publickey", "public-key")
    short_id = _query_value(values, "sid", "shortid", "short-id")
    if security == "reality" or public_key or short_id:
        reality: dict[str, object] = {}
        if public_key:
            reality["public-key"] = public_key
        if short_id:
            reality["short-id"] = short_id
        if reality:
            config["reality-opts"] = reality
    packet_encoding = _query_value(values, "packetencoding", "packet-encoding")
    if packet_encoding:
        config["packet-encoding"] = packet_encoding
    _apply_transport_options(config, values, _query_value(values, "type", "network", default="tcp"))
    return _core_node(line, "vless", config)


def _parse_trojan_uri(line: str) -> ParsedProxyNode | None:
    parsed = urlsplit(line)
    try:
        port = int(parsed.port or 0)
    except ValueError:
        return None
    password = _uri_userinfo(parsed)
    if not password:
        return None
    values = _query(parsed.query)
    config: dict[str, object] = {
        "name": _fragment_name(line),
        "type": "trojan",
        "server": str(parsed.hostname or ""),
        "port": port,
        "password": password,
        "udp": True,
    }
    _apply_tls_options(config, values, sni_key="sni", enabled=False)
    public_key = _query_value(values, "pbk", "publickey", "public-key")
    short_id = _query_value(values, "sid", "shortid", "short-id")
    if public_key or short_id:
        reality: dict[str, object] = {}
        if public_key:
            reality["public-key"] = public_key
        if short_id:
            reality["short-id"] = short_id
        config["reality-opts"] = reality
    _apply_transport_options(config, values, _query_value(values, "type", "network", default="tcp"))
    return _core_node(line, "trojan", config)


def _parse_hysteria_uri(line: str, scheme: str) -> ParsedProxyNode | None:
    parsed = urlsplit(line)
    try:
        port = int(parsed.port or 0)
    except ValueError:
        return None
    values = _query(parsed.query)
    canonical = "hysteria2" if scheme in {"hysteria2", "hy2"} else "hysteria"
    auth = _uri_userinfo(parsed) or _query_value(values, "auth", "password")
    if canonical == "hysteria2" and not auth:
        return None
    config: dict[str, object] = {
        "name": _fragment_name(line),
        "type": canonical,
        "server": str(parsed.hostname or ""),
        "port": port,
    }
    if auth:
        config["password" if canonical == "hysteria2" else "auth-str"] = auth
    servername = _query_value(values, "sni", "servername", "peer")
    if servername:
        config["sni"] = servername
    if _query_bool(values, "insecure", "allowinsecure", "skip-cert-verify"):
        config["skip-cert-verify"] = True
    alpn = _query_list(values, "alpn")
    if alpn:
        config["alpn"] = alpn
    obfs = _query_value(values, "obfs")
    obfs_password = _query_value(values, "obfs-password", "obfspassword")
    if obfs:
        config["obfs"] = obfs
    if obfs_password:
        config["obfs-password"] = obfs_password
    for query_name, config_name in (
        ("upmbps", "up"),
        ("downmbps", "down"),
        ("protocol", "protocol"),
        ("ports", "ports"),
    ):
        value = _query_value(values, query_name, config_name)
        if value:
            config[config_name] = (
                _number_or_text(value) if config_name in {"up", "down"} else value
            )
    return _core_node(line, canonical, config)


def _parse_tuic_uri(line: str) -> ParsedProxyNode | None:
    parsed = urlsplit(line)
    try:
        port = int(parsed.port or 0)
    except ValueError:
        return None
    userinfo = _uri_userinfo(parsed)
    uuid, separator, password = userinfo.partition(":")
    if not separator or not uuid or not password:
        return None
    values = _query(parsed.query)
    config: dict[str, object] = {
        "name": _fragment_name(line),
        "type": "tuic",
        "server": str(parsed.hostname or ""),
        "port": port,
        "uuid": uuid,
        "password": password,
    }
    servername = _query_value(values, "sni", "servername")
    if servername:
        config["sni"] = servername
    if _query_bool(values, "insecure", "allowinsecure", "skip-cert-verify"):
        config["skip-cert-verify"] = True
    for query_name, config_name, default in (
        ("congestion_control", "congestion-controller", "bbr"),
        ("congestion-controller", "congestion-controller", "bbr"),
        ("udp_relay_mode", "udp-relay-mode", "native"),
        ("udp-relay-mode", "udp-relay-mode", "native"),
    ):
        value = _query_value(values, query_name)
        if value:
            config[config_name] = value
        elif config_name not in config:
            config[config_name] = default
    alpn = _query_list(values, "alpn")
    if alpn:
        config["alpn"] = alpn
    return _core_node(line, "tuic", config)


def _parse_anytls_uri(line: str) -> ParsedProxyNode | None:
    parsed = urlsplit(line)
    try:
        port = int(parsed.port or 0)
    except ValueError:
        return None
    password = _uri_userinfo(parsed)
    if not password:
        return None
    values = _query(parsed.query)
    config: dict[str, object] = {
        "name": _fragment_name(line),
        "type": "anytls",
        "server": str(parsed.hostname or ""),
        "port": port,
        "password": password,
    }
    _apply_tls_options(config, values, sni_key="sni", enabled=False)
    for query_name, config_name in (
        ("idle-session-check-interval", "idle-session-check-interval"),
        ("idle-session-timeout", "idle-session-timeout"),
        ("min-idle-session", "min-idle-session"),
    ):
        value = _query_value(values, query_name)
        if value:
            try:
                config[config_name] = int(value)
            except ValueError:
                config[config_name] = value
    return _core_node(line, "anytls", config)


def _parse_mieru_uri(line: str) -> ParsedProxyNode | None:
    parsed = urlsplit(line)
    try:
        port = int(parsed.port or 0)
    except ValueError:
        return None
    userinfo = _uri_userinfo(parsed)
    username, separator, password = userinfo.partition(":")
    if not separator or not username or not password:
        return None
    values = _query(parsed.query)
    config: dict[str, object] = {
        "name": _fragment_name(line),
        "type": "mieru",
        "server": str(parsed.hostname or ""),
        "port": port,
        "username": username,
        "password": password,
        "transport": _mieru_transport(_query_value(values, "transport", default="TCP")),
    }
    multiplexing = _query_value(values, "multiplexing", "multiplex")
    if multiplexing:
        config["multiplexing"] = _mieru_multiplexing(multiplexing)
    port_range = _mieru_port_range(_query_value(values, "port-range", "port_range", "portrange"))
    if port_range:
        config["port-range"] = port_range
    return _core_node(line, "mieru", config)


def _parse_ss_plugin(config: dict[str, object], plugin_value: str) -> None:
    parts = [unquote(item).strip() for item in plugin_value.split(";") if item.strip()]
    if not parts:
        return
    plugin = parts[0].lower()
    if plugin in {"obfs-local", "simple-obfs"}:
        plugin = "obfs"
    config["plugin"] = plugin
    options: dict[str, object] = {}
    for item in parts[1:]:
        key, separator, value = item.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if not separator:
            options[key] = True
        elif key == "obfs":
            options["mode"] = value
        elif key == "obfs-host":
            options["host"] = value
        else:
            options[key] = value
    if options:
        config["plugin-opts"] = options


def _parse_ss_uri(line: str) -> ParsedProxyNode | None:
    body = line[len("ss://") :]
    body, _, fragment = body.partition("#")
    body, separator, raw_query = body.partition("?")
    values = _query(raw_query if separator else "")
    credentials = ""
    address = ""
    if "@" in body:
        encoded_credentials, address = body.rsplit("@", 1)
        credentials = _decode_base64_value(unquote(encoded_credentials)) or unquote(encoded_credentials)
    else:
        decoded = _decode_base64_value(body)
        if "/?" in decoded:
            decoded, decoded_query = decoded.split("/?", 1)
            values.update(_query(decoded_query))
        if "@" not in decoded:
            return None
        credentials, address = decoded.rsplit("@", 1)
    method, credential_separator, password = credentials.partition(":")
    if not credential_separator or not method or not password:
        return None
    parsed_address = urlsplit("//" + address.lstrip("/"))
    try:
        port = int(parsed_address.port or 0)
    except ValueError:
        return None
    config: dict[str, object] = {
        "name": unquote(fragment),
        "type": "ss",
        "server": str(parsed_address.hostname or ""),
        "port": port,
        "cipher": method,
        "password": password,
        "udp": True,
    }
    plugin = _query_value(values, "plugin")
    if plugin:
        _parse_ss_plugin(config, plugin)
    return _core_node(line, "ss", config)


def _parse_ssr_uri(line: str) -> ParsedProxyNode | None:
    decoded = _decode_base64_value(line[len("ssr://") :].split("#", 1)[0])
    if not decoded:
        return None
    base, _, raw_query = decoded.partition("/?")
    parts = base.rsplit(":", 5)
    if len(parts) != 6:
        return None
    host, port_text, protocol, method, obfs, password_encoded = parts
    try:
        port = int(port_text)
    except ValueError:
        return None
    password = _decode_base64_value(unquote(password_encoded))
    if not password:
        return None
    values = _query(raw_query)
    remarks = _decode_base64_value(unquote(_query_value(values, "remarks")))
    config: dict[str, object] = {
        "name": remarks or _fragment_name(line),
        "type": "ssr",
        "server": host,
        "port": port,
        "cipher": method,
        "password": password,
        "protocol": protocol,
        "obfs": obfs,
        "udp": True,
    }
    protocol_param = _decode_base64_value(unquote(_query_value(values, "protoparam")))
    obfs_param = _decode_base64_value(unquote(_query_value(values, "obfsparam")))
    if protocol_param:
        config["protocol-param"] = protocol_param
    if obfs_param:
        config["obfs-param"] = obfs_param
    return _core_node(line, "ssr", config)


def _parse_vmess_uri(line: str) -> ParsedProxyNode | None:
    payload = line[len("vmess://") :].split("#", 1)[0]
    decoded = _decode_base64_value(payload)
    try:
        data = json.loads(decoded)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        port = int(data.get("port") or 0)
    except (TypeError, ValueError):
        return None
    uuid = str(data.get("id") or data.get("uuid") or "").strip()
    if not uuid:
        return None
    config: dict[str, object] = {
        "name": str(data.get("ps") or _fragment_name(line)),
        "type": "vmess",
        "server": str(data.get("add") or data.get("server") or ""),
        "port": port,
        "uuid": uuid,
        "cipher": str(data.get("scy") or data.get("cipher") or "auto"),
        "udp": True,
    }
    try:
        config["alterId"] = int(data.get("aid") or data.get("alterId") or data.get("alter-id") or 0)
    except (TypeError, ValueError):
        config["alterId"] = 0
    values: dict[str, list[str]] = {}
    for key, value in (
        ("host", data.get("host")),
        ("path", data.get("path")),
        ("servicename", data.get("path") or data.get("serviceName")),
        ("sni", data.get("sni") or data.get("servername")),
        ("fp", data.get("fp")),
        ("alpn", data.get("alpn")),
    ):
        if value is not None and value != "":
            values[key] = [str(value)]
    tls = str(data.get("tls") or "").strip().lower() in {"tls", "1", "true"}
    _apply_tls_options(config, values, sni_key="servername", enabled=tls)
    if str(data.get("allowInsecure") or "").strip().lower() in {"1", "true"}:
        config["skip-cert-verify"] = True
    network = str(data.get("net") or data.get("network") or "tcp")
    _apply_transport_options(config, values, network)
    return _core_node(line, "vmess", config)


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
    if scheme == "vless":
        return _parse_vless_uri(line)
    if scheme == "vmess":
        return _parse_vmess_uri(line)
    if scheme == "ss":
        return _parse_ss_uri(line)
    if scheme == "ssr":
        return _parse_ssr_uri(line)
    if scheme == "trojan":
        return _parse_trojan_uri(line)
    if scheme in {"hysteria", "hysteria2", "hy2"}:
        return _parse_hysteria_uri(line, scheme)
    if scheme == "tuic":
        return _parse_tuic_uri(line)
    if scheme == "anytls":
        return _parse_anytls_uri(line)
    if scheme == "mieru":
        return _parse_mieru_uri(line)
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
    if scheme in {"socks5", "socks5h"} and (username or password):
        return _core_node(
            line,
            "socks5",
            {
                "name": name,
                "type": "socks5",
                "server": host,
                "port": port,
                "username": username,
                "password": password,
                "udp": True,
            },
        )
    if scheme == "socks4" and (username or password):
        return ParsedProxyNode(
            raw=f"socks4://{host}:{port}#{quote(name, safe='')}",
            scheme=scheme,
            host=host,
            port=port,
            name=name,
            usable=False,
            note="authenticated SOCKS4 is not supported by the transport core",
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
    source_scheme = str(item.get("type") or "").strip().lower()
    scheme = "hysteria2" if source_scheme == "hy2" else source_scheme
    host = str(item.get("server") or "").strip()
    try:
        port = int(
            item.get("port")
            or (
                _first_port(item.get("port-range") or item.get("port_range"))
                if scheme == "mieru"
                else 0
            )
        )
    except (TypeError, ValueError):
        port = 0
    name = str(item.get("name") or "").strip()
    if scheme not in DIRECT_SCHEMES | TUNNEL_SCHEMES or not _valid_host_port(host, port):
        return None
    if scheme in TUNNEL_SCHEMES:
        source = dict(item)
        source["type"] = scheme
        return ParsedProxyNode(
            raw=f"{scheme}://{host}:{port}#{name}",
            scheme=scheme,
            host=host,
            port=port,
            name=name,
            usable=False,
            note="tunnel protocol requires a transport core",
            core_config=source,
        )
    username = str(item.get("username") or "")
    password = str(item.get("password") or "")
    if scheme in {"socks5", "socks5h"} and (username or password):
        source = dict(item)
        source["type"] = "socks5"
        return _core_node(f"{scheme}://{host}:{port}#{name}", "socks5", source)
    if scheme == "socks4" and (username or password):
        return ParsedProxyNode(
            raw=f"socks4://{host}:{port}#{quote(name, safe='')}",
            scheme=scheme,
            host=host,
            port=port,
            name=name,
            usable=False,
            note="authenticated SOCKS4 is not supported by the transport core",
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


def _apply_singbox_tls(config: dict[str, object], tls: object) -> None:
    if not isinstance(tls, dict) or not bool(tls.get("enabled", True)):
        return
    config["tls"] = True
    servername = str(tls.get("server_name") or tls.get("server-name") or "").strip()
    if servername:
        key = "servername" if config.get("type") in {"vless", "vmess"} else "sni"
        config[key] = servername
    if bool(tls.get("insecure")):
        config["skip-cert-verify"] = True
    alpn = tls.get("alpn")
    if isinstance(alpn, list) and alpn:
        config["alpn"] = [str(item) for item in alpn if str(item)]
    utls = tls.get("utls")
    if isinstance(utls, dict) and utls.get("enabled", True) and utls.get("fingerprint"):
        config["client-fingerprint"] = str(utls["fingerprint"])
    reality = tls.get("reality")
    if isinstance(reality, dict) and reality.get("enabled", True):
        options: dict[str, object] = {}
        public_key = reality.get("public_key") or reality.get("public-key")
        short_id = reality.get("short_id") or reality.get("short-id")
        if public_key:
            options["public-key"] = str(public_key)
        if short_id:
            options["short-id"] = str(short_id)
        if options:
            config["reality-opts"] = options


def _apply_singbox_transport(config: dict[str, object], transport: object) -> None:
    if not isinstance(transport, dict):
        return
    values: dict[str, list[str]] = {}
    for source, target in (
        ("host", "host"),
        ("path", "path"),
        ("service_name", "servicename"),
        ("service-name", "servicename"),
    ):
        value = transport.get(source)
        if value is not None and value != "":
            values[target] = [str(value)]
    headers = transport.get("headers")
    if isinstance(headers, dict):
        host = headers.get("Host") or headers.get("host")
        if host:
            values["host"] = [str(host)]
    _apply_transport_options(config, values, str(transport.get("type") or "tcp"))
    if not isinstance(headers, dict):
        return
    normalized = {str(key): str(value) for key, value in headers.items() if str(key)}
    network = str(config.get("network") or "").lower()
    if network == "ws" and normalized:
        config.setdefault("ws-opts", {})["headers"] = normalized
    elif network == "httpupgrade" and normalized:
        config.setdefault("http-upgrade-opts", {})["headers"] = normalized
    elif network == "http" and normalized:
        config.setdefault("http-opts", {})["headers"] = {
            key: [value] for key, value in normalized.items()
        }


def _parse_singbox_outbound(item: object) -> ParsedProxyNode | None:
    if not isinstance(item, dict):
        return None
    source_type = str(item.get("type") or "").strip().lower()
    type_map = {
        "shadowsocks": "ss",
        "socks": "socks5",
        "http": "http",
        "vmess": "vmess",
        "vless": "vless",
        "trojan": "trojan",
        "hysteria": "hysteria",
        "hysteria2": "hysteria2",
        "tuic": "tuic",
        "anytls": "anytls",
        "mieru": "mieru",
    }
    scheme = type_map.get(source_type)
    if not scheme:
        return None
    host = str(item.get("server") or "").strip()
    mieru_range = _mieru_port_range(
        item.get("port_range") or item.get("port-range") or item.get("server_ports")
    )
    try:
        port = int(
            item.get("server_port")
            or item.get("port")
            or (_first_port(mieru_range) if scheme == "mieru" else 0)
        )
    except (TypeError, ValueError):
        return None
    if not _valid_host_port(host, port):
        return None
    name = str(item.get("tag") or item.get("name") or f"{scheme}-{host}").strip()
    if scheme in {"http", "socks5"}:
        direct = {
            "name": name,
            "type": scheme,
            "server": host,
            "port": port,
            "username": str(item.get("username") or ""),
            "password": str(item.get("password") or ""),
        }
        return _parse_clash_proxy(direct)
    config: dict[str, object] = {
        "name": name,
        "type": scheme,
        "server": host,
        "port": port,
    }
    if scheme in {"vless", "vmess", "tuic"} and item.get("uuid"):
        config["uuid"] = str(item["uuid"])
    if scheme == "vmess":
        try:
            config["alterId"] = int(
                item.get("alter_id") or item.get("alterId") or item.get("alter-id") or 0
            )
        except (TypeError, ValueError):
            config["alterId"] = 0
        config["cipher"] = str(item.get("security") or "auto")
        config["udp"] = True
    elif scheme == "vless":
        config["udp"] = True
        if item.get("flow"):
            config["flow"] = str(item["flow"])
        if item.get("packet_encoding"):
            config["packet-encoding"] = str(item["packet_encoding"])
    elif scheme in {"trojan", "hysteria2", "anytls"}:
        password = item.get("password")
        if password:
            config["password"] = str(password)
        if scheme == "trojan":
            config["udp"] = True
    elif scheme == "hysteria":
        auth = item.get("auth_str") or item.get("auth")
        if auth:
            config["auth-str"] = str(auth)
    elif scheme == "tuic":
        if item.get("password"):
            config["password"] = str(item["password"])
        config["congestion-controller"] = str(item.get("congestion_control") or "bbr")
        config["udp-relay-mode"] = str(item.get("udp_relay_mode") or "native")
    elif scheme == "ss":
        config["cipher"] = str(item.get("method") or "")
        config["password"] = str(item.get("password") or "")
        config["udp"] = True
        plugin = str(item.get("plugin") or "").strip()
        plugin_opts = str(item.get("plugin_opts") or item.get("plugin-opts") or "").strip()
        if plugin:
            _parse_ss_plugin(config, ";".join(part for part in (plugin, plugin_opts) if part))
    elif scheme == "mieru":
        config["username"] = str(item.get("username") or "")
        config["password"] = str(item.get("password") or "")
        config["transport"] = _mieru_transport(item.get("transport"))
        if item.get("multiplexing"):
            config["multiplexing"] = _mieru_multiplexing(item["multiplexing"])
        if mieru_range:
            config["port-range"] = mieru_range
            config.pop("port", None)
    if scheme in {"hysteria", "hysteria2"}:
        for source, target in (("up_mbps", "up"), ("down_mbps", "down")):
            if item.get(source) is not None:
                config[target] = _number_or_text(item[source])
        obfs = item.get("obfs")
        if isinstance(obfs, dict):
            if obfs.get("type"):
                config["obfs"] = str(obfs["type"])
            if obfs.get("password"):
                config["obfs-password"] = str(obfs["password"])
        elif obfs:
            config["obfs"] = str(obfs)
        if item.get("obfs_password"):
            config["obfs-password"] = str(item["obfs_password"])
    _apply_singbox_tls(config, item.get("tls"))
    if scheme != "mieru":
        _apply_singbox_transport(config, item.get("transport"))
    return _core_node("", scheme, config)


def _parse_sip008_server(item: object) -> ParsedProxyNode | None:
    if not isinstance(item, dict):
        return None
    config: dict[str, object] = {
        "name": str(item.get("remarks") or item.get("id") or ""),
        "type": "ss",
        "server": str(item.get("server") or ""),
        "port": item.get("server_port") or item.get("port") or 0,
        "cipher": str(item.get("method") or item.get("cipher") or ""),
        "password": str(item.get("password") or ""),
        "udp": True,
    }
    plugin = str(item.get("plugin") or "").strip()
    plugin_opts = str(item.get("plugin_opts") or item.get("plugin-opts") or "").strip()
    if plugin:
        _parse_ss_plugin(config, ";".join(part for part in (plugin, plugin_opts) if part))
    return _core_node("", "ss", config)


def _parse_json_document(raw: str) -> list[ParsedProxyNode] | None:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(document, dict) and isinstance(document.get("proxies"), list):
        return [node for item in document["proxies"] if (node := _parse_clash_proxy(item))]
    if isinstance(document, dict) and isinstance(document.get("outbounds"), list):
        return [node for item in document["outbounds"] if (node := _parse_singbox_outbound(item))]
    if isinstance(document, dict) and isinstance(document.get("servers"), list):
        return [node for item in document["servers"] if (node := _parse_sip008_server(item))]
    if isinstance(document, list):
        if any(isinstance(item, dict) and "server_port" in item and "method" in item for item in document):
            return [node for item in document if (node := _parse_sip008_server(item))]
        return [node for item in document if (node := _parse_singbox_outbound(item))]
    return None


def _decode_base64_subscription(text: str) -> str:
    compact = "".join(text.split())
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9_+/=-]+", compact):
        return ""
    decoded = _decode_base64_value(compact).strip()
    if (
        "://" in decoded
        or "\n" in decoded
        or decoded.lstrip().startswith(("proxies:", "{", "["))
    ):
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
    json_nodes = _parse_json_document(raw)
    if json_nodes is not None:
        return json_nodes
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
