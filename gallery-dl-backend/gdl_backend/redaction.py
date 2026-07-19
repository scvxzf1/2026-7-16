from __future__ import annotations

import re
from typing import Any, Iterable


_URL_USERINFO = re.compile(
    r"(?P<scheme>\b(?:https?|socks4|socks5h?|ss|ssr|vmess|vless|trojan|"
    r"hysteria|hysteria2|hy2|tuic|anytls|mieru)://)"
    r"(?P<userinfo>[^\s/@]+(?::[^\s/@]*)?)@",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"(?P<key>[?&;/](?:token|access_token|api[_-]?key|auth|password|passwd|secret|session|cookie|signature|sig|code|email|username|keystamp|fileindex)=)"
    r"(?P<value>[^&#\s]+)",
    re.IGNORECASE,
)
_KEY_VALUE_SECRET = re.compile(
    r"(?P<key>\b(?:authorization|proxy-authorization|cookie|set-cookie|token|api[_-]?key|password|passwd|secret)\b\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact_text(value: object, *, secrets: Iterable[str] = (), limit: int | None = None) -> str:
    text = str(value or "")
    for secret in secrets:
        secret_text = str(secret or "")
        if secret_text:
            text = text.replace(secret_text, "***")
    text = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}***:***@", text)
    text = _QUERY_SECRET.sub(lambda m: f"{m.group('key')}***", text)
    text = _KEY_VALUE_SECRET.sub(lambda m: f"{m.group('key')}***", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text


def redact_data(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"token|password|passwd|secret|cookie|api.?key|authorization", key_text, re.I):
                result[key_text] = "***"
            else:
                result[key_text] = redact_data(item)
        return result
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
