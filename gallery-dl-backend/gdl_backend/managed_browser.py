from __future__ import annotations

import json
import os
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

import websocket

from .file_security import secure_private_path


@dataclass(frozen=True, slots=True)
class ManagedBrowserSite:
    site: str
    label: str
    login_url: str
    domains: tuple[str, ...]
    required: tuple[str, ...]
    recommended: tuple[str, ...]


MANAGED_BROWSER_SITES: dict[str, ManagedBrowserSite] = {
    "twitter": ManagedBrowserSite(
        site="twitter",
        label="X / Twitter",
        login_url="https://x.com/i/flow/login",
        domains=("x.com", "twitter.com"),
        required=("auth_token", "ct0"),
        recommended=(),
    ),
    "exhentai": ManagedBrowserSite(
        site="exhentai",
        label="EH",
        login_url="https://forums.e-hentai.org/index.php?act=Login&CODE=00",
        domains=("e-hentai.org", "exhentai.org"),
        required=("ipb_member_id", "ipb_pass_hash"),
        recommended=("igneous",),
    ),
}


def discover_chrome_executable(explicit: str = "") -> Path:
    candidates: list[Path] = []
    configured = explicit.strip() or os.environ.get("GDL_BACKEND_CHROME", "").strip()
    if configured:
        candidates.append(Path(os.path.expandvars(os.path.expanduser(configured))))
    else:
        for name in (
            "chrome.exe",
            "chrome",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        if os.name == "nt":
            for root in (
                os.environ.get("PROGRAMFILES"),
                os.environ.get("PROGRAMFILES(X86)"),
                os.environ.get("LOCALAPPDATA"),
            ):
                if root:
                    candidates.append(
                        Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
                    )
        elif os.name == "posix":
            candidates.extend(
                (
                    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    Path("/usr/bin/google-chrome"),
                    Path("/usr/bin/google-chrome-stable"),
                    Path("/usr/bin/chromium"),
                    Path("/usr/bin/chromium-browser"),
                )
            )
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_file() and (os.name == "nt" or os.access(resolved, os.X_OK)):
            return resolved
    raise FileNotFoundError("Chrome or Chromium executable was not found")


def read_devtools_active_port(profile_dir: Path) -> tuple[int, str] | None:
    path = profile_dir / "DevToolsActivePort"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        endpoint = str(lines[1]).strip()
    except (OSError, ValueError, IndexError):
        return None
    if not 1 <= port <= 65535 or not endpoint.startswith("/devtools/browser/"):
        return None
    return port, f"ws://127.0.0.1:{port}{endpoint}"


def allocate_debug_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _devtools_json(port: int, path: str) -> Any:
    if not 1 <= int(port) <= 65535:
        raise ValueError("Chrome DevTools port is invalid")
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{int(port)}{path}", timeout=0.5) as response:
        return json.load(response)


def read_browser_websocket(port: int) -> tuple[int, str] | None:
    try:
        payload = _devtools_json(port, "/json/version")
        websocket_url = str(payload.get("webSocketDebuggerUrl") or "")
        parsed = urlsplit(websocket_url)
    except Exception:
        return None
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if parsed.port != int(port) or not parsed.path.startswith("/devtools/browser/"):
        return None
    return int(port), websocket_url


def _page_websocket(browser_websocket_url: str, target_id: str) -> str:
    parsed = urlsplit(browser_websocket_url)
    if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Chrome DevTools browser endpoint is invalid")
    port = parsed.port
    if port is None:
        raise RuntimeError("Chrome DevTools browser endpoint has no port")
    expected = str(target_id or "").strip()
    if not expected:
        raise RuntimeError("Chrome DevTools target id is missing")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        targets = _devtools_json(port, "/json/list")
        for target in targets if isinstance(targets, list) else ():
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            current_id = str(target.get("id") or target.get("targetId") or "")
            if current_id != expected:
                continue
            websocket_url = str(target.get("webSocketDebuggerUrl") or "")
            page = urlsplit(websocket_url)
            if (
                page.scheme == "ws"
                and page.hostname in {"127.0.0.1", "localhost", "::1"}
                and page.port == port
                and page.path.startswith("/devtools/page/")
            ):
                return websocket_url
        time.sleep(0.05)
    raise RuntimeError("Chrome DevTools page target was not found")


def cdp_request(
    websocket_url: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    connection = websocket.create_connection(
        websocket_url,
        timeout=max(0.5, float(timeout)),
        suppress_origin=True,
    )
    try:
        request_id = 1
        connection.send(
            json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                separators=(",", ":"),
            )
        )
        while True:
            payload = json.loads(connection.recv())
            if payload.get("id") != request_id:
                continue
            error = payload.get("error")
            if error:
                raise RuntimeError(str(error.get("message") or "Chrome DevTools request failed"))
            result = payload.get("result")
            return result if isinstance(result, dict) else {}
    finally:
        connection.close()


def _is_pixiv_oauth_callback(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        codes = query.get("code", ())
        states = query.get("state", ())
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() == "app-api.pixiv.net"
            and parsed.path == "/web/v1/users/auth/pixiv/callback"
            and len(codes) == 1
            and bool(codes[0].strip())
            and len(states) == 1
            and bool(states[0].strip())
        )
    except ValueError:
        return False


def capture_pixiv_oauth_callback(
    browser_websocket_url: str,
    target_id: str,
    login_url: str,
    *,
    timeout: float = 600,
) -> str:
    """Navigate a dedicated Chrome page and return its Pixiv OAuth callback URL."""

    login = urlsplit(str(login_url or ""))
    if (
        login.scheme != "https"
        or (login.hostname or "").lower() != "app-api.pixiv.net"
        or login.path != "/web/v1/login"
    ):
        raise ValueError("Pixiv OAuth login URL is invalid")

    page_websocket_url = _page_websocket(browser_websocket_url, target_id)
    deadline = time.monotonic() + max(1.0, float(timeout))
    connection = websocket.create_connection(
        page_websocket_url,
        timeout=min(5.0, max(1.0, float(timeout))),
        suppress_origin=True,
    )
    request_id = 0
    captured_callback = ""

    def remember_callback(payload: dict[str, Any]) -> None:
        nonlocal captured_callback
        if captured_callback:
            return
        method = payload.get("method")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        if method == "Network.requestWillBeSent":
            request = params.get("request") if isinstance(params.get("request"), dict) else {}
            url = str(request.get("url") or "")
        elif method == "Network.responseReceived":
            response = params.get("response") if isinstance(params.get("response"), dict) else {}
            url = str(response.get("url") or "")
        else:
            return
        if _is_pixiv_oauth_callback(url):
            captured_callback = url

    def command(method: str, params: dict[str, Any] | None = None) -> None:
        nonlocal request_id
        request_id += 1
        current_id = request_id
        connection.send(
            json.dumps(
                {"id": current_id, "method": method, "params": params or {}},
                separators=(",", ":"),
            )
        )
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            connection.settimeout(max(0.1, min(1.0, remaining)))
            try:
                raw = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise RuntimeError("Pixiv project Chrome DevTools connection closed")
            payload = json.loads(raw)
            if payload.get("id") != current_id:
                # CDP can emit redirect/network events before Page.navigate's
                # response. Remember the short-lived callback instead of
                # discarding it while waiting for the command acknowledgement.
                remember_callback(payload)
                continue
            error = payload.get("error")
            if error:
                raise RuntimeError(
                    str(error.get("message") or "Pixiv project Chrome DevTools command failed")
                )
            return
        raise TimeoutError("Pixiv project Chrome DevTools command timed out")

    try:
        # Enable Network before navigation so the short-lived callback request
        # cannot race ahead of the listener. Pixiv redirects it to pixiv://.
        command("Network.enable")
        command("Page.enable")
        command("Page.navigate", {"url": login_url})
        if captured_callback:
            return captured_callback
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            connection.settimeout(max(0.1, min(1.0, remaining)))
            try:
                raw = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise RuntimeError("Pixiv project Chrome DevTools connection closed")
            payload = json.loads(raw)
            remember_callback(payload)
            if captured_callback:
                return captured_callback
        raise TimeoutError("Pixiv login timed out before the OAuth callback")
    finally:
        connection.close()


def _domain_matches(domain: str, roots: tuple[str, ...]) -> bool:
    value = str(domain or "").lstrip(".").lower()
    return any(value == root or value.endswith(f".{root}") for root in roots)


def site_cookies(cookies: list[dict[str, Any]], spec: ManagedBrowserSite) -> list[dict[str, Any]]:
    now = time.time()
    selected: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cookie in cookies:
        if not isinstance(cookie, dict) or not _domain_matches(str(cookie.get("domain") or ""), spec.domains):
            continue
        try:
            expires = float(cookie.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0
        if expires > 0 and expires <= now:
            continue
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        path = str(cookie.get("path") or "/")
        value = str(cookie.get("value") or "")
        if not name or not domain or any("\t" in field or "\r" in field or "\n" in field for field in (name, domain, path, value)):
            continue
        selected[(domain, path, name)] = {**cookie, "expires": expires}
    return list(selected.values())


def get_site_cookies(websocket_url: str, spec: ManagedBrowserSite) -> list[dict[str, Any]]:
    result = cdp_request(websocket_url, "Storage.getCookies")
    cookies = result.get("cookies")
    return site_cookies(cookies if isinstance(cookies, list) else [], spec)


def managed_login_ready(
    websocket_url: str,
    spec: ManagedBrowserSite,
    target_id: str,
) -> bool:
    """Confirm that cookie creation was followed by a completed login page."""

    if spec.site != "twitter":
        return True
    try:
        targets = cdp_request(websocket_url, "Target.getTargets").get("targetInfos") or []
    except Exception:
        return False
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        if str(target.get("targetId") or "") != str(target_id or ""):
            continue
        parsed = urlsplit(str(target.get("url") or ""))
        if (parsed.hostname or "").lower() not in {"x.com", "www.x.com", "twitter.com"}:
            continue
        path = (parsed.path or "/").lower().rstrip("/") or "/"
        if path.startswith("/i/flow/login") or path.startswith("/account/access"):
            continue
        return True
    return False


def open_login_target(websocket_url: str, login_url: str) -> str:
    created = cdp_request(websocket_url, "Target.createTarget", {"url": login_url})
    created_id = str(created.get("targetId") or "")
    if not created_id:
        raise RuntimeError("Chrome DevTools did not create a page target")
    return created_id


def close_target(websocket_url: str, target_id: str) -> None:
    if not websocket_url or not target_id:
        return
    try:
        cdp_request(websocket_url, "Target.closeTarget", {"targetId": target_id}, timeout=2)
    except Exception:
        pass


def close_browser(websocket_url: str) -> None:
    try:
        cdp_request(websocket_url, "Browser.close", timeout=2)
    except Exception:
        pass


def write_netscape_cookies(path: Path, cookies: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_private_path(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as fp:
        fp.write("# Netscape HTTP Cookie File\n\n")
        for cookie in sorted(
            cookies,
            key=lambda item: (
                str(item.get("domain") or ""),
                str(item.get("path") or "/"),
                str(item.get("name") or ""),
            ),
        ):
            domain = str(cookie.get("domain") or "")
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            cookie_path = str(cookie.get("path") or "/")
            secure = "TRUE" if cookie.get("secure") else "FALSE"
            expires = max(0, int(float(cookie.get("expires") or 0)))
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            fp.write(
                f"{domain}\t{include_subdomains}\t{cookie_path}\t{secure}\t"
                f"{expires}\t{name}\t{value}\n"
            )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    secure_private_path(path)
