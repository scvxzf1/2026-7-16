# -*- coding: utf-8 -*-
"""Manual proxy pool: normalize / validate / weighted rotate / cooldown.

Cloned and adapted from an earlier ProxyRotator implementation, with project
integration points (host validation, shared stats path, display helpers).
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union
from urllib.parse import urlparse

from local_paths import STATE_DIR

try:
    import orjson as _orjson  # type: ignore
except Exception:  # pragma: no cover
    _orjson = None


def _json_dumps(obj: Any) -> str:
    """Fast JSON dumps via orjson when available, else stdlib json."""
    if _orjson is not None:
        return _orjson.dumps(obj).decode("utf-8")
    return json.dumps(obj, ensure_ascii=False)


def _json_loads(text: str) -> Any:
    if _orjson is not None:
        return _orjson.loads(text)
    return json.loads(text)


def orjson_available() -> bool:
    return _orjson is not None


_PROXY_STATS_DEFAULT = "proxy_stats.log"
_INVALID_HOSTS = {
    "",
    "null",
    "none",
    "undefined",
    "nil",
    "0.0.0.0",
    "localhost",
    "example.com",
    "example.org",
}


def project_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_stats_path() -> str:
    return str(STATE_DIR / _PROXY_STATS_DEFAULT)


def normalize_proxy_line(line: str) -> str:
    """Normalize free-form proxy text into http(s)/socks URL.

    Supports:
      - already-qualified URLs
      - socks5 / socks5h prefixes
      - host:port:user:pass
      - host:port
      - user:pass@host:port
    """
    text = str(line or "").strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
        if not text:
            return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        return text
    lower = text.lower()
    if lower.startswith("socks5h"):
        rest = text[7:].lstrip()
        if rest.startswith("//"):
            rest = rest[2:].lstrip()
        return f"socks5h://{rest}"
    if lower.startswith("socks5"):
        rest = text[6:].lstrip()
        if rest.startswith("//"):
            rest = rest[2:].lstrip()
        return f"socks5://{rest}"
    # host:port:user:pass
    if "://" not in text and text.count(":") >= 3 and "@" not in text:
        parts = text.split(":")
        host, port_s, user = parts[0], parts[1], parts[2]
        pwd = ":".join(parts[3:])
        if host and port_s.isdigit():
            return f"http://{user}:{pwd}@{host}:{port_s}"
    if "://" not in text and "@" in text:
        return f"http://{text}"
    if "://" not in text and text.count(":") == 1:
        return f"http://{text}"
    return f"http://{text}"


def proxy_host_of(proxy_str: str) -> str:
    text = str(proxy_str or "").strip()
    if not text:
        return ""
    try:
        # Prefer raw host:port:user:pass host segment before URL encoding surprises.
        if "://" not in text and text.count(":") >= 3 and "@" not in text:
            return text.split(":", 1)[0].strip().lower()
        parsed = urlparse(text if "://" in text else f"http://{text}")
        return str(parsed.hostname or "").strip().lower()
    except Exception:
        return ""


def is_valid_proxy_host(host: str) -> bool:
    h = str(host or "").strip().lower()
    if not h or h in _INVALID_HOSTS:
        return False
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    # bare placeholder / non-resolvable literals
    if h in _INVALID_HOSTS:
        return False
    if re.fullmatch(r"\d+", h):
        return False
    return True


def validate_proxy_line(line: str) -> tuple[str, str]:
    """Return (normalized_url, error). error empty means ok."""
    raw = str(line or "").strip()
    if not raw:
        return "", "空代理"
    normalized = normalize_proxy_line(raw)
    if not normalized:
        return "", "无法规范化代理"
    host = proxy_host_of(normalized) or proxy_host_of(raw)
    if not is_valid_proxy_host(host):
        return "", f"无效代理主机: {host or '(empty)'}"
    # port sanity when parseable
    try:
        parsed = urlparse(normalized)
        port = parsed.port
        if port is not None and not (1 <= int(port) <= 65535):
            return "", f"无效代理端口: {port}"
    except Exception as exc:
        return "", f"代理解析失败: {exc}"
    return normalized, ""


def load_proxy_lines(filepath: str) -> List[str]:
    """Load proxies from file; skip blanks/comments; normalize; drop invalids."""
    path = str(filepath or "").strip()
    if not path or not os.path.exists(path):
        return []
    out: List[str] = []
    seen = set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = str(raw or "").strip()
                if not line or line.startswith("#"):
                    continue
                normalized, err = validate_proxy_line(line)
                if err or not normalized:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                out.append(normalized)
    except OSError:
        return []
    return out


def split_proxy_text(raw: str) -> List[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = [str(x) for x in parsed]
                return normalize_proxy_pool(items)
        except Exception:
            pass
    parts = re.split(r"[\n\r,;|]+", text)
    return normalize_proxy_pool(parts)


def normalize_proxy_pool(raw: Sequence[str] | str) -> List[str]:
    if isinstance(raw, str):
        lines = re.split(r"[\n\r,;|]+", raw)
    else:
        lines = list(raw or [])
    out: List[str] = []
    seen = set()
    for part in lines:
        normalized, err = validate_proxy_line(str(part or ""))
        if err or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def extract_country(proxy_str: str) -> str:
    """Best-effort country code from zone/user/host.

    Examples:
      _zone_JP / zone-JP / region-US / us.swiftproxy.net
    """
    if not proxy_str:
        return "??"
    text = str(proxy_str)
    for pattern in (
        r"(?:_zone_|zone[-_]|region[-_]|country[-_])([A-Za-z]{2})(?:\b|[_-])",
        r"[_-]([A-Za-z]{2})[_-](?:sid|session|sess)",
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    try:
        host = proxy_host_of(text)
        parts = host.split(".")
        if parts and len(parts[0]) == 2 and parts[0].isalpha():
            return parts[0].upper()
    except Exception:
        pass
    return "??"


def extract_session_seconds(proxy: str) -> Optional[int]:
    """sessTime-N is minutes in common residential formats → seconds."""
    match = re.search(r"sessTime-(\d+)", str(proxy or ""), re.IGNORECASE)
    if not match:
        match = re.search(r"time[_-]?(\d+)", str(proxy or ""), re.IGNORECASE)
        if not match:
            return None
    try:
        return int(match.group(1)) * 60
    except Exception:
        return None


def mask_proxy(proxy_str: str) -> str:
    try:
        u = urlparse(proxy_str if "://" in proxy_str else f"http://{proxy_str}")
        if u.username or u.password:
            host = u.hostname or ""
            port = u.port or ""
            return f"{u.scheme}://***@{host}:{port}"
    except Exception:
        pass
    return str(proxy_str or "")


_CREDENTIAL_PROXY_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<credentials>[^/@\s]+@)"
)


def redact_proxy_text(text: object, proxy_list: Sequence[str] = ()) -> str:
    """Remove proxy credentials from diagnostic text before it is persisted.

    Exact pool entries are replaced first so unusual usernames/passwords do not
    defeat the generic URL pattern.  The function intentionally leaves host and
    port visible because they are useful route diagnostics and are not secrets.
    """
    clean = str(text or "")
    for proxy in proxy_list or ():
        raw = str(proxy or "")
        if raw and raw in clean:
            clean = clean.replace(raw, mask_proxy(raw))
    return _CREDENTIAL_PROXY_RE.sub(r"\g<scheme>***@", clean)


@dataclass(frozen=True)
class ProxyLease:
    """Opaque exclusive binding between one task and one proxy entry."""

    token: str = field(repr=False)
    proxy: str = field(repr=False)
    owner: str = field(default="", repr=False)
    acquired_at: float = field(default=0.0, repr=False)
    expires_at: float = field(default=0.0, repr=False)

    @property
    def masked_proxy(self) -> str:
        return mask_proxy(self.proxy)


class ProxyRotator:
    """Thread-safe weighted proxy rotator with dynamic cooldown + JSONL/SQLite stats.

    Performance notes (P0–P3):
      - Stats are buffered (dirty + interval flush) to cut write amplification.
      - Available set + country-weight cache avoid rebuilding on every pick.
      - Country success/fail counters are updated incrementally (O(1)).
      - RLock + dict indexes support multi-threaded acquire/release.
      - Optional SQLite backend (stats_file ends with .db or backend='sqlite').
      - orjson used for JSONL dumps when installed.
    """

    DEFAULT_SAVE_INTERVAL = 2.0

    def __init__(
        self,
        proxy_list: Sequence[str],
        stats_file: str = "",
        *,
        save_interval: float = DEFAULT_SAVE_INTERVAL,
        auto_flush_thread: bool = True,
        stats_backend: str = "auto",
    ):
        normalized = normalize_proxy_pool(list(proxy_list or []))
        # Ordered list for stable iteration; set for O(1) membership.
        self._proxies: List[str] = list(normalized)
        self._proxy_set: set[str] = set(self._proxies)
        self._lock = threading.RLock()
        self._bad_proxies: Dict[str, float] = {}
        self._country_stats: Dict[str, Dict[str, Any]] = {}
        self._proxy_country: Dict[str, str] = {}
        self._leases_by_token: Dict[str, ProxyLease] = {}
        self._lease_token_by_proxy: Dict[str, str] = {}
        self._stats_file = stats_file or default_stats_path()
        backend = str(stats_backend or "auto").strip().lower()
        if backend == "auto":
            backend = "sqlite" if str(self._stats_file).lower().endswith(".db") else "jsonl"
        if backend not in {"jsonl", "sqlite"}:
            backend = "jsonl"
        self._stats_backend = backend
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._save_interval = max(0.0, float(save_interval))
        self._pending_logs: List[Dict[str, Any]] = []
        self._dirty = False
        # Start the interval clock at construction so the first non-force flush
        # still respects save_interval (avoids an immediate write on first mark).
        self._last_flush_at = time.time()
        # Available free (not leased, not cooling) set — rebuilt lazily.
        self._available: set[str] = set(self._proxies)
        self._available_dirty = False
        # Country weight cache; invalidated on score change.
        self._country_weight_cache: Dict[str, float] = {}
        self._weights_dirty = True
        self._stop_flush = threading.Event()
        self._flush_thread: Optional[threading.Thread] = None

        for proxy in self._proxies:
            country = extract_country(proxy)
            self._proxy_country[proxy] = country
            if country not in self._country_stats:
                self._country_stats[country] = {
                    "success": 0,
                    "fail": 0,
                    "consecutive_fail": 0,
                    "last_fail_time": 0.0,
                }
        if self._stats_backend == "sqlite":
            self._init_sqlite()
        self._load_history()
        self._rebuild_available_locked(time.time())
        self._rebuild_weight_cache_locked()

        if auto_flush_thread and self._save_interval > 0:
            self._flush_thread = threading.Thread(
                target=self._flush_loop,
                name="proxy-rotator-flush",
                daemon=True,
            )
            self._flush_thread.start()

    def __len__(self) -> int:
        return len(self._proxies)

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    def proxies(self) -> List[str]:
        with self._lock:
            return list(self._proxies)

    @property
    def proxy_map(self) -> Dict[str, str]:
        """Read-only snapshot: proxy URL → country code."""
        with self._lock:
            return dict(self._proxy_country)

    def close(self) -> None:
        """Stop background flush and force-write pending stats."""
        self._stop_flush.set()
        thread = self._flush_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self.flush(force=True)
        with self._lock:
            conn = self._sqlite_conn
            self._sqlite_conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _flush_loop(self) -> None:
        interval = max(0.2, self._save_interval)
        while not self._stop_flush.wait(interval):
            try:
                self.flush(force=False)
            except Exception:
                pass

    def _init_sqlite(self) -> None:
        path = self._stats_file
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS proxy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                country TEXT NOT NULL,
                proxy TEXT NOT NULL,
                result TEXT NOT NULL,
                reason TEXT DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS country_stats (
                country TEXT PRIMARY KEY,
                success INTEGER NOT NULL DEFAULT 0,
                fail INTEGER NOT NULL DEFAULT 0,
                consecutive_fail INTEGER NOT NULL DEFAULT 0,
                last_fail_time REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
        self._sqlite_conn = conn

    def _load_history(self) -> None:
        if self._stats_backend == "sqlite":
            self._load_history_sqlite()
            return
        if not os.path.exists(self._stats_file):
            return
        try:
            with open(self._stats_file, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-2000:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json_loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                country = str(rec.get("country") or "??")
                result = str(rec.get("result") or "")
                if country not in self._country_stats:
                    self._country_stats[country] = {
                        "success": 0,
                        "fail": 0,
                        "consecutive_fail": 0,
                        "last_fail_time": 0.0,
                    }
                if result == "success":
                    self._country_stats[country]["success"] += 1
                elif result == "fail":
                    self._country_stats[country]["fail"] += 1
            self._weights_dirty = True
        except Exception:
            pass

    def _load_history_sqlite(self) -> None:
        conn = self._sqlite_conn
        if conn is None:
            return
        try:
            rows = conn.execute(
                "SELECT country, success, fail, consecutive_fail, last_fail_time FROM country_stats"
            ).fetchall()
            if rows:
                for country, success, fail, consecutive_fail, last_fail_time in rows:
                    self._country_stats[str(country)] = {
                        "success": int(success or 0),
                        "fail": int(fail or 0),
                        "consecutive_fail": int(consecutive_fail or 0),
                        "last_fail_time": float(last_fail_time or 0.0),
                    }
            else:
                # Rebuild aggregates from recent events if stats table empty.
                events = conn.execute(
                    "SELECT country, result FROM proxy_events ORDER BY id DESC LIMIT 5000"
                ).fetchall()
                for country, result in reversed(events):
                    c = str(country or "??")
                    if c not in self._country_stats:
                        self._country_stats[c] = {
                            "success": 0,
                            "fail": 0,
                            "consecutive_fail": 0,
                            "last_fail_time": 0.0,
                        }
                    if result == "success":
                        self._country_stats[c]["success"] += 1
                    elif result == "fail":
                        self._country_stats[c]["fail"] += 1
            self._weights_dirty = True
        except Exception:
            pass

    def _queue_log_locked(
        self,
        country: str,
        proxy_str: str,
        result: str,
        reason: str = "",
    ) -> None:
        rec: Dict[str, Any] = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "country": country,
            "proxy": mask_proxy(proxy_str),
            "result": result,
        }
        if reason:
            rec["reason"] = redact_proxy_text(reason, self._proxies)[:200]
        self._pending_logs.append(rec)
        self._dirty = True

    def _append_log(self, country: str, proxy_str: str, result: str, reason: str = "") -> None:
        """Buffer one stats line; disk write is deferred to :meth:`flush`."""
        with self._lock:
            self._queue_log_locked(country, proxy_str, result, reason)
        if self._save_interval <= 0:
            self.flush(force=True)

    def flush(self, force: bool = False) -> int:
        """Write pending stats. Returns number of records written.

        When ``force`` is false, respects ``save_interval`` (dirty batching).
        Backend is JSONL (orjson when available) or SQLite.
        """
        with self._lock:
            if not self._dirty or not self._pending_logs:
                return 0
            now = time.time()
            if (
                not force
                and self._save_interval > 0
                and (now - self._last_flush_at) < self._save_interval
            ):
                return 0
            batch = self._pending_logs
            self._pending_logs = []
            self._dirty = False
            self._last_flush_at = now
            stats_file = self._stats_file
            backend = self._stats_backend
            country_snapshot = {
                c: dict(st) for c, st in self._country_stats.items()
            }
            conn = self._sqlite_conn

        if not batch:
            return 0
        try:
            if backend == "sqlite":
                return self._flush_sqlite(batch, country_snapshot, conn)
            parent = os.path.dirname(stats_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(stats_file, "a", encoding="utf-8") as handle:
                for rec in batch:
                    handle.write(_json_dumps(rec) + "\n")
            return len(batch)
        except Exception:
            # Put back on failure so a later flush can retry.
            with self._lock:
                self._pending_logs = batch + self._pending_logs
                self._dirty = True
            return 0

    def _flush_sqlite(
        self,
        batch: List[Dict[str, Any]],
        country_snapshot: Dict[str, Dict[str, Any]],
        conn: Optional[sqlite3.Connection],
    ) -> int:
        if conn is None:
            self._init_sqlite()
            conn = self._sqlite_conn
        if conn is None:
            raise RuntimeError("sqlite connection unavailable")
        rows = [
            (
                str(rec.get("time") or ""),
                str(rec.get("country") or "??"),
                str(rec.get("proxy") or ""),
                str(rec.get("result") or ""),
                str(rec.get("reason") or ""),
            )
            for rec in batch
        ]
        with self._lock:
            conn.executemany(
                "INSERT INTO proxy_events (ts, country, proxy, result, reason) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            for country, st in country_snapshot.items():
                conn.execute(
                    """
                    INSERT INTO country_stats (country, success, fail, consecutive_fail, last_fail_time)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(country) DO UPDATE SET
                        success=excluded.success,
                        fail=excluded.fail,
                        consecutive_fail=excluded.consecutive_fail,
                        last_fail_time=excluded.last_fail_time
                    """,
                    (
                        country,
                        int(st.get("success") or 0),
                        int(st.get("fail") or 0),
                        int(st.get("consecutive_fail") or 0),
                        float(st.get("last_fail_time") or 0.0),
                    ),
                )
            conn.commit()
        return len(batch)

    def pending_log_count(self) -> int:
        with self._lock:
            return len(self._pending_logs)

    def _stats_for_proxy_locked(self, proxy_str: str) -> tuple[str, Dict[str, Any]]:
        country = self._proxy_country.get(proxy_str) or extract_country(proxy_str)
        self._proxy_country[proxy_str] = country
        if country not in self._country_stats:
            self._country_stats[country] = {
                "success": 0,
                "fail": 0,
                "consecutive_fail": 0,
                "last_fail_time": 0.0,
            }
            self._weights_dirty = True
        return country, self._country_stats[country]

    def _record_result_locked(self, proxy_str: str, success: bool) -> str:
        """O(1) incremental country score update."""
        country, stats = self._stats_for_proxy_locked(proxy_str)
        if success:
            stats["success"] += 1
            stats["consecutive_fail"] = 0
        else:
            stats["fail"] += 1
            stats["consecutive_fail"] += 1
            stats["last_fail_time"] = time.time()
        # Invalidate only this country's cached weight.
        self._country_weight_cache.pop(country, None)
        self._weights_dirty = True
        return country

    def _mark_bad_locked(self, proxy_str: str, cooldown_seconds: int = 0) -> None:
        _country, stats = self._stats_for_proxy_locked(proxy_str)
        if cooldown_seconds > 0:
            cooldown = int(cooldown_seconds)
        else:
            consecutive = int(stats["consecutive_fail"] or 0)
            cooldown = min(60 * (2 ** max(consecutive, 0)), 600)
        self._bad_proxies[proxy_str] = time.time() + cooldown
        self._available.discard(proxy_str)

    def _mark_good_locked(self, proxy_str: str) -> None:
        self._bad_proxies.pop(proxy_str, None)
        if (
            proxy_str in self._proxy_set
            and proxy_str not in self._lease_token_by_proxy
        ):
            self._available.add(proxy_str)

    def record_result(self, proxy_str: str, success: bool, reason: str = "") -> None:
        if not proxy_str:
            return
        with self._lock:
            country = self._record_result_locked(proxy_str, bool(success))
            self._queue_log_locked(
                country,
                proxy_str,
                "success" if success else "fail",
                reason,
            )
        if self._save_interval <= 0:
            self.flush(force=True)

    def mark_bad(self, proxy_str: str, cooldown_seconds: int = 0) -> None:
        if not proxy_str:
            return
        with self._lock:
            self._mark_bad_locked(proxy_str, cooldown_seconds=cooldown_seconds)

    def mark_good(self, proxy_str: str) -> None:
        if not proxy_str:
            return
        with self._lock:
            self._mark_good_locked(proxy_str)

    def _expire_cooldowns_locked(self, now: Optional[float] = None) -> int:
        current = time.time() if now is None else float(now)
        recovered = 0
        expired = [p for p, deadline in self._bad_proxies.items() if current >= deadline]
        for proxy in expired:
            del self._bad_proxies[proxy]
            if proxy in self._proxy_set and proxy not in self._lease_token_by_proxy:
                self._available.add(proxy)
            recovered += 1
        return recovered

    def _is_available(self, proxy_str: str, now: Optional[float] = None) -> bool:
        deadline = self._bad_proxies.get(proxy_str)
        if deadline is None:
            return True
        current = time.time() if now is None else float(now)
        if current >= deadline:
            del self._bad_proxies[proxy_str]
            if proxy_str in self._proxy_set and proxy_str not in self._lease_token_by_proxy:
                self._available.add(proxy_str)
            return True
        return False

    def _rebuild_available_locked(self, now: Optional[float] = None) -> None:
        current = time.time() if now is None else float(now)
        self._expire_cooldowns_locked(current)
        free: set[str] = set()
        for proxy in self._proxies:
            if proxy in self._lease_token_by_proxy:
                continue
            deadline = self._bad_proxies.get(proxy)
            if deadline is None or current >= deadline:
                if deadline is not None:
                    del self._bad_proxies[proxy]
                free.add(proxy)
        self._available = free
        self._available_dirty = False

    def _rebuild_weight_cache_locked(self) -> None:
        self._country_weight_cache = {
            country: self._country_weight_compute(country)
            for country in self._country_stats
        }
        self._weights_dirty = False

    def _country_weight_compute(self, country: str) -> float:
        st = self._country_stats.get(country)
        if not st:
            return 1.0
        total = int(st["success"]) + int(st["fail"])
        if total <= 0:
            return 1.0
        rate = float(st["success"]) / float(total)
        return max(rate * 10.0, 0.1)

    def _country_weight(self, country: str) -> float:
        cached = self._country_weight_cache.get(country)
        if cached is not None and not self._weights_dirty:
            return cached
        weight = self._country_weight_compute(country)
        self._country_weight_cache[country] = weight
        return weight

    def _pick_weighted_locked(self, available: Sequence[str]) -> str:
        if len(available) == 1:
            return available[0]
        if self._weights_dirty:
            # Partial rebuild: only missing keys.
            for proxy in available:
                country = self._proxy_country.get(proxy, "??")
                if country not in self._country_weight_cache:
                    self._country_weight_cache[country] = self._country_weight_compute(country)
            self._weights_dirty = False
        weights = [
            self._country_weight_cache.get(
                self._proxy_country.get(item, "??"),
                1.0,
            )
            for item in available
        ]
        return random.choices(list(available), weights=weights, k=1)[0]

    def _list_available_locked(self, now: Optional[float] = None) -> List[str]:
        current = time.time() if now is None else float(now)
        self._expire_cooldowns_locked(current)
        if self._available_dirty:
            self._rebuild_available_locked(current)
        # Filter leased (available set should already exclude them, but be safe).
        return [p for p in self._proxies if p in self._available and p not in self._lease_token_by_proxy]

    def _recover_expired_leases_locked(self, now: Optional[float] = None) -> int:
        current = time.monotonic() if now is None else float(now)
        expired = [
            token
            for token, lease in self._leases_by_token.items()
            if lease.expires_at > 0 and current >= lease.expires_at
        ]
        for token in expired:
            lease = self._leases_by_token.pop(token, None)
            if lease is not None and self._lease_token_by_proxy.get(lease.proxy) == token:
                self._lease_token_by_proxy.pop(lease.proxy, None)
                if lease.proxy not in self._bad_proxies:
                    self._available.add(lease.proxy)
        return len(expired)

    def recover_expired_leases(self) -> int:
        """Return abandoned TTL leases to the free pool."""
        with self._lock:
            return self._recover_expired_leases_locked()

    def acquire_lease(
        self,
        *,
        owner: object = "",
        ttl_seconds: float = 0.0,
    ) -> Optional[ProxyLease]:
        """Atomically lease one healthy, currently-unbound entry.

        Unlike :meth:`next`, this never falls back to a cooled entry and never
        hands one entry to two active owners.
        """
        if not self._proxies:
            return None
        with self._lock:
            self._recover_expired_leases_locked()
            now = time.time()
            available = self._list_available_locked(now)
            if not available:
                return None
            proxy = self._pick_weighted_locked(available)
            mono = time.monotonic()
            ttl = max(0.0, float(ttl_seconds or 0.0))
            token = secrets.token_urlsafe(24)
            lease = ProxyLease(
                token=token,
                proxy=proxy,
                owner=redact_proxy_text(owner, self._proxies)[:120],
                acquired_at=mono,
                expires_at=(mono + ttl) if ttl > 0 else 0.0,
            )
            self._leases_by_token[token] = lease
            self._lease_token_by_proxy[proxy] = token
            self._available.discard(proxy)
            return lease

    def release_lease(
        self,
        lease_or_token: Union[ProxyLease, str],
        *,
        success: Optional[bool] = None,
        reason: str = "",
        cooldown_seconds: int = 0,
    ) -> bool:
        """Release an exclusive lease once and optionally attribute its outcome.

        A stale/duplicate token is a no-op and returns ``False``.  Health updates
        happen under the lock; stats lines are queued via :meth:`_append_log`
        after the critical section so cooldown is visible before disk I/O.
        """
        token = (
            lease_or_token.token
            if isinstance(lease_or_token, ProxyLease)
            else str(lease_or_token or "")
        )
        if not token:
            return False
        log_record: Optional[tuple[str, str, str, str]] = None
        with self._lock:
            lease = self._leases_by_token.pop(token, None)
            if lease is None:
                return False
            if self._lease_token_by_proxy.get(lease.proxy) == token:
                self._lease_token_by_proxy.pop(lease.proxy, None)
            if success is not None:
                clean_reason = redact_proxy_text(reason, self._proxies)[:200]
                country = self._record_result_locked(lease.proxy, bool(success))
                if success:
                    self._mark_good_locked(lease.proxy)
                else:
                    self._mark_bad_locked(
                        lease.proxy,
                        cooldown_seconds=cooldown_seconds,
                    )
                log_record = (
                    country,
                    lease.proxy,
                    "success" if success else "fail",
                    clean_reason,
                )
            else:
                if (
                    lease.proxy not in self._bad_proxies
                    and lease.proxy in self._proxy_set
                ):
                    self._available.add(lease.proxy)
        if log_record is not None:
            country, proxy, result, clean_reason = log_record
            self._append_log(country, proxy, result, clean_reason)
        return True

    def available_lease_count(self) -> int:
        """Number of healthy entries that can be leased immediately."""
        with self._lock:
            self._recover_expired_leases_locked()
            return len(self._list_available_locked())

    def active_lease_count(self) -> int:
        with self._lock:
            self._recover_expired_leases_locked()
            return len(self._leases_by_token)

    def lease_status(self) -> List[Dict[str, Any]]:
        """Secret-free active lease diagnostics."""
        with self._lock:
            self._recover_expired_leases_locked()
            return [
                {
                    "proxy": mask_proxy(lease.proxy),
                    "owner": redact_proxy_text(lease.owner, self._proxies),
                    "expires": bool(lease.expires_at > 0),
                }
                for lease in self._leases_by_token.values()
            ]

    def next(self) -> Optional[str]:
        if not self._proxies:
            return None
        with self._lock:
            now = time.time()
            available = self._list_available_locked(now)
            if available:
                return self._pick_weighted_locked(available)
            # Fall back to first cooling entry (legacy behaviour).
            first_bad: Optional[str] = None
            for proxy in self._proxies:
                if proxy in self._lease_token_by_proxy:
                    continue
                if first_bad is None:
                    first_bad = proxy
            return first_bad

    def next_batch(self, n: int) -> List[str]:
        """Pick up to n distinct proxies (best-effort, may repeat if pool tiny)."""
        count = max(0, int(n or 0))
        if count <= 0 or not self._proxies:
            return []
        picked: List[str] = []
        seen = set()
        for _ in range(count * 3):
            item = self.next()
            if not item:
                break
            if item in seen:
                continue
            seen.add(item)
            picked.append(item)
            if len(picked) >= count:
                break
        with self._lock:
            repeatable = [
                proxy
                for proxy in self._proxies
                if proxy not in self._lease_token_by_proxy
            ]
        while len(picked) < count and repeatable:
            picked.append(random.choice(repeatable))
        return picked[:count]

    def get_status(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._expire_cooldowns_locked(now)
            rows: List[Dict[str, Any]] = []
            for proxy_str in self._proxies:
                deadline = self._bad_proxies.get(proxy_str)
                if deadline is None or now >= deadline:
                    status = "ok"
                    cooldown_left = 0
                else:
                    status = "bad"
                    cooldown_left = int(deadline - now)
                rows.append(
                    {
                        "proxy": mask_proxy(proxy_str),
                        "status": status,
                        "cooldown_left": cooldown_left,
                        "country": self._proxy_country.get(proxy_str, "??"),
                        "leased": proxy_str in self._lease_token_by_proxy,
                    }
                )
            return rows

    def get_country_stats(self) -> List[Dict[str, Any]]:
        with self._lock:
            now = time.time()
            self._expire_cooldowns_locked(now)
            rows: List[Dict[str, Any]] = []
            for country, st in sorted(self._country_stats.items()):
                total = int(st["success"]) + int(st["fail"])
                rate = (float(st["success"]) / float(total) * 100.0) if total > 0 else 0.0
                active = 0
                cooldown = 0
                for proxy, c in self._proxy_country.items():
                    if c != country:
                        continue
                    if self._is_available(proxy, now):
                        active += 1
                    else:
                        cooldown += 1
                rows.append(
                    {
                        "country": country,
                        "success": int(st["success"]),
                        "fail": int(st["fail"]),
                        "rate": round(rate, 1),
                        "weight": round(self._country_weight(country), 1),
                        "consecutive_fail": int(st["consecutive_fail"]),
                        "active_proxies": active,
                        "cooldown_proxies": cooldown,
                    }
                )
            return rows


# Process-level rotator used by registration workers / web tests.
_global_rotator: Optional[ProxyRotator] = None
_global_rotator_lock = threading.Lock()
_global_rotator_key = ""


def configure_global_rotator(
    proxy_list: Sequence[str],
    *,
    stats_file: str = "",
    force: bool = False,
) -> ProxyRotator:
    global _global_rotator, _global_rotator_key
    normalized = normalize_proxy_pool(list(proxy_list or []))
    key = f"{stats_file or default_stats_path()}|{len(normalized)}|{hash(tuple(normalized[:50]))}"
    with _global_rotator_lock:
        if (not force) and _global_rotator is not None and key == _global_rotator_key:
            return _global_rotator
        _global_rotator = ProxyRotator(normalized, stats_file=stats_file or default_stats_path())
        _global_rotator_key = key
        return _global_rotator


def get_global_rotator() -> Optional[ProxyRotator]:
    return _global_rotator


def ensure_rotator_from_file(filepath: str, *, stats_file: str = "") -> ProxyRotator:
    proxies = load_proxy_lines(filepath)
    return configure_global_rotator(proxies, stats_file=stats_file)


def pick_proxy(
    proxy_list: Sequence[str] | None = None,
    *,
    stats_file: str = "",
    prefer_rotator: bool = True,
) -> str:
    """Pick one proxy URL; empty string if none."""
    if prefer_rotator:
        rotator = get_global_rotator()
        if rotator is None and proxy_list is not None:
            rotator = configure_global_rotator(proxy_list, stats_file=stats_file)
        if rotator is not None and len(rotator) > 0:
            return str(rotator.next() or "")
    pool = normalize_proxy_pool(list(proxy_list or []))
    if not pool:
        return ""
    return random.choice(pool)


def report_outcome(proxy_str: str, success: bool, reason: str = "") -> None:
    """Record success/failure on the process-level rotator if present."""
    target = str(proxy_str or "").strip()
    if not target:
        return
    rotator = get_global_rotator()
    if rotator is None:
        return
    try:
        pool = list(rotator.proxies())
        key = target
        if target not in pool:
            norm, _err = validate_proxy_line(target)
            if norm and norm in pool:
                key = norm
            else:
                host = proxy_host_of(target)
                if host:
                    for item in pool:
                        if host in item:
                            key = item
                            break
        rotator.record_result(key, bool(success), reason=str(reason or "")[:120])
        if success:
            rotator.mark_good(key)
        else:
            rotator.mark_bad(key)
    except Exception:
        pass
