from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .redaction import redact_data, redact_text


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_STATUSES = {"starting", "running", "cancelling"}


class Database:
    def __init__(self, path: Path, *, max_logs_per_task: int = 5000) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_logs_per_task = max(100, int(max_logs_per_task))
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._initialize()

    def _initialize(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                url TEXT NOT NULL,
                site TEXT NOT NULL,
                subcategory TEXT NOT NULL DEFAULT '',
                extractor TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                output_dir TEXT NOT NULL,
                proxy_mode TEXT NOT NULL,
                max_attempts INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_run_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                pid INTEGER,
                process_marker TEXT,
                exit_code INTEGER,
                last_error_class TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                cookies_file TEXT,
                config_file TEXT,
                credentials_ref TEXT,
                extra_args_json TEXT NOT NULL DEFAULT '[]',
                policy_json TEXT NOT NULL DEFAULT '{}',
                tried_proxy_ids_json TEXT NOT NULL DEFAULT '[]',
                artifact_count INTEGER NOT NULL DEFAULT 0,
                artifact_bytes INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_queue
                ON tasks(status, next_run_at, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_site_status
                ON tasks(site, status);

            CREATE TABLE IF NOT EXISTS attempts (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                attempt_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                exit_code INTEGER,
                proxy_node_id TEXT,
                proxy_endpoint TEXT,
                error_class TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                retryable INTEGER NOT NULL DEFAULT 0,
                pid INTEGER,
                process_marker TEXT,
                UNIQUE(task_id, attempt_no)
            );
            CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id, attempt_no);

            CREATE TABLE IF NOT EXISTS leases (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                site TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                heartbeat_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                attempt_id TEXT,
                ts REAL NOT NULL,
                stream TEXT NOT NULL,
                line TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_logs_task_id ON task_logs(task_id, id);

            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_events_task_id ON task_events(task_id, id);

            CREATE TABLE IF NOT EXISTS site_policies (
                site TEXT PRIMARY KEY,
                policy_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @staticmethod
    def _task(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key, default in (
            ("extra_args_json", []),
            ("policy_json", {}),
            ("tried_proxy_ids_json", []),
        ):
            target = key.removesuffix("_json")
            try:
                data[target] = json.loads(data.pop(key) or "null")
            except Exception:
                data[target] = default
                data.pop(key, None)
        data["cancel_requested"] = bool(data.get("cancel_requested"))
        return data

    @staticmethod
    def _attempt(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["retryable"] = bool(data.get("retryable"))
        return data

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1").fetchone()[0] == 1

    def _event(self, conn: sqlite3.Connection, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        conn.execute(
            "INSERT INTO task_events(task_id, ts, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (task_id, time.time(), event_type, json.dumps(redact_data(payload or {}), ensure_ascii=False)),
        )

    def create_task(self, values: dict[str, Any], *, idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
        now = time.time()
        with self._transaction() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    return self._task(existing), False  # type: ignore[return-value]
            task_id = str(values.get("id") or uuid.uuid4())
            conn.execute(
                """
                INSERT INTO tasks(
                    id, idempotency_key, url, site, subcategory, extractor, status,
                    priority, output_dir, proxy_mode, max_attempts, attempt_count,
                    next_run_at, created_at, updated_at, cookies_file, config_file,
                    credentials_ref, extra_args_json, policy_json, tried_proxy_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, '[]')
                """,
                (
                    task_id,
                    idempotency_key,
                    values["url"],
                    values["site"],
                    values.get("subcategory", ""),
                    values.get("extractor", ""),
                    int(values.get("priority", 0)),
                    values["output_dir"],
                    values["proxy_mode"],
                    int(values["max_attempts"]),
                    now,
                    now,
                    now,
                    values.get("cookies_file"),
                    values.get("config_file"),
                    values.get("credentials_ref"),
                    json.dumps(values.get("extra_args", []), ensure_ascii=False),
                    json.dumps(values.get("policy", {}), ensure_ascii=False),
                ),
            )
            self._event(conn, task_id, "queued", {"site": values["site"]})
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return self._task(row), True  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            task = self._task(row)
            if task is None:
                return None
            attempt = self._conn.execute(
                "SELECT * FROM attempts WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1", (task_id,)
            ).fetchone()
            task["latest_attempt"] = self._attempt(attempt)
            lease = self._conn.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            task["lease"] = dict(lease) if lease else None
            return task

    def get_task_by_idempotency(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT id FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
        return self.get_task(row["id"]) if row else None

    def list_tasks(
        self,
        *,
        status: str | None = None,
        site: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if site:
            clauses.append("site=?")
            params.append(site)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params
            ).fetchall()
            return [self._task(row) for row in rows]  # type: ignore[misc]

    def queued_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE status='queued' AND cancel_requested=0 AND next_run_at<=?
                ORDER BY priority DESC, created_at ASC LIMIT ?
                """,
                (time.time(), max(1, min(int(limit), 1000))),
            ).fetchall()
            return [self._task(row) for row in rows]  # type: ignore[misc]

    def claim_task(self, task_id: str) -> bool:
        now = time.time()
        with self._transaction() as conn:
            cur = conn.execute(
                """
                UPDATE tasks SET status='starting', updated_at=?,
                    started_at=COALESCE(started_at, ?), version=version+1
                WHERE id=? AND status='queued' AND cancel_requested=0
                """,
                (now, now, task_id),
            )
            if cur.rowcount:
                self._event(conn, task_id, "starting")
                return True
            return False

    def begin_attempt(self, task_id: str) -> dict[str, Any]:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] != "starting":
                raise RuntimeError(f"任务状态不是 starting: {row['status']}")
            attempt_no = int(row["attempt_count"]) + 1
            attempt_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO attempts(id, task_id, attempt_no, status, started_at)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (attempt_id, task_id, attempt_no, now),
            )
            conn.execute(
                """
                UPDATE tasks SET status='running', attempt_count=?, updated_at=?,
                    pid=NULL, process_marker=NULL, version=version+1 WHERE id=?
                """,
                (attempt_no, now, task_id),
            )
            self._event(conn, task_id, "attempt_started", {"attempt": attempt_no, "attempt_id": attempt_id})
            task = self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            return {"id": attempt_id, "attempt_no": attempt_no, "task": task}

    def set_process(self, task_id: str, attempt_id: str, pid: int, marker: str) -> None:
        now = time.time()
        with self._transaction() as conn:
            cur = conn.execute(
                "UPDATE attempts SET pid=?, process_marker=? WHERE id=? AND task_id=? AND status='running'",
                (int(pid), marker, attempt_id, task_id),
            )
            if cur.rowcount:
                conn.execute(
                    """
                    UPDATE tasks SET pid=?, process_marker=?, updated_at=?
                    WHERE id=? AND status='running' AND EXISTS (
                        SELECT 1 FROM attempts
                        WHERE attempts.id=? AND attempts.task_id=tasks.id AND attempts.status='running'
                    )
                    """,
                    (int(pid), marker, now, task_id, attempt_id),
                )
                self._event(conn, task_id, "process_started", {"pid": int(pid), "attempt_id": attempt_id})

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        exit_code: int | None,
        status: str,
        error_class: str = "",
        error_message: str = "",
        retryable: bool = False,
        proxy_node_id: str | None = None,
        proxy_endpoint: str | None = None,
    ) -> None:
        with self._transaction() as conn:
            row = conn.execute("SELECT task_id, attempt_no FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                return
            cur = conn.execute(
                """
                UPDATE attempts SET status=?, finished_at=?, exit_code=?, proxy_node_id=?,
                    proxy_endpoint=?, error_class=?, error_message=?, retryable=?
                WHERE id=? AND status='running'
                """,
                (
                    status,
                    time.time(),
                    exit_code,
                    proxy_node_id,
                    proxy_endpoint,
                    error_class,
                    redact_text(error_message, limit=2000),
                    int(bool(retryable)),
                    attempt_id,
                ),
            )
            if cur.rowcount:
                self._event(
                    conn,
                    row["task_id"],
                    "attempt_finished",
                    {"attempt": row["attempt_no"], "status": status, "exit_code": exit_code, "error_class": error_class},
                )

    def complete_task(
        self,
        task_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        error_class: str = "",
        error_message: str = "",
        expected_attempt_id: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(status)
        now = time.time()
        with self._transaction() as conn:
            attempt_guard = ""
            params: list[Any] = [
                status,
                now,
                now,
                exit_code,
                error_class,
                redact_text(error_message, limit=2000),
                status,
                task_id,
            ]
            if expected_attempt_id:
                attempt_guard = """
                    AND status IN ('running','cancelling')
                    AND (SELECT id FROM attempts WHERE task_id=tasks.id ORDER BY attempt_no DESC LIMIT 1)=?
                """
                params.append(expected_attempt_id)
            cur = conn.execute(
                f"""
                UPDATE tasks SET status=?, finished_at=?, updated_at=?, pid=NULL,
                    process_marker=NULL, exit_code=?, last_error_class=?, last_error=?,
                    cancel_requested=CASE WHEN ?='cancelled' THEN 1 ELSE cancel_requested END,
                    version=version+1 WHERE id=? {attempt_guard}
                """,
                params,
            )
            if cur.rowcount:
                conn.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
                self._event(conn, task_id, status, {"exit_code": exit_code, "error_class": error_class})
            return self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def requeue_task(
        self,
        task_id: str,
        *,
        next_run_at: float,
        exit_code: int | None,
        error_class: str,
        error_message: str,
        tried_proxy_ids: list[str],
        expected_attempt_id: str | None = None,
    ) -> bool:
        now = time.time()
        with self._transaction() as conn:
            attempt_guard = ""
            params: list[Any] = [
                float(next_run_at),
                now,
                exit_code,
                error_class,
                redact_text(error_message, limit=2000),
                json.dumps(list(dict.fromkeys(tried_proxy_ids))),
                task_id,
            ]
            if expected_attempt_id:
                attempt_guard = """
                    AND status IN ('running','cancelling')
                    AND (SELECT id FROM attempts WHERE task_id=tasks.id ORDER BY attempt_no DESC LIMIT 1)=?
                """
                params.append(expected_attempt_id)
            cur = conn.execute(
                f"""
                UPDATE tasks SET status='queued', next_run_at=?, updated_at=?, pid=NULL,
                    process_marker=NULL, exit_code=?, last_error_class=?, last_error=?,
                    tried_proxy_ids_json=?, version=version+1
                WHERE id=? AND cancel_requested=0 {attempt_guard}
                """,
                params,
            )
            if cur.rowcount:
                conn.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
                self._event(conn, task_id, "retry_scheduled", {"next_run_at": next_run_at, "error_class": error_class})
            return bool(cur.rowcount)

    def request_cancel(self, task_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            status = row["status"]
            if status in TERMINAL_STATUSES:
                return self._task(row)
            if status == "queued":
                conn.execute(
                    """
                    UPDATE tasks SET status='cancelled', cancel_requested=1,
                        finished_at=?, updated_at=?, version=version+1 WHERE id=?
                    """,
                    (now, now, task_id),
                )
                self._event(conn, task_id, "cancelled")
            else:
                conn.execute(
                    """
                    UPDATE tasks SET status='cancelling', cancel_requested=1,
                        updated_at=?, version=version+1 WHERE id=?
                    """,
                    (now, task_id),
                )
                self._event(conn, task_id, "cancelling")
            return self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def retry_task(self, task_id: str, additional_attempts: int) -> dict[str, Any] | None:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            if row["status"] not in TERMINAL_STATUSES:
                raise RuntimeError("仅终态任务支持重新排队")
            max_attempts = int(row["attempt_count"]) + max(1, int(additional_attempts))
            conn.execute(
                """
                UPDATE tasks SET status='queued', cancel_requested=0, next_run_at=?,
                    finished_at=NULL, updated_at=?, max_attempts=?, pid=NULL,
                    process_marker=NULL, exit_code=NULL, last_error_class='', last_error='',
                    tried_proxy_ids_json='[]', version=version+1 WHERE id=?
                """,
                (now, now, max_attempts, task_id),
            )
            self._event(conn, task_id, "manually_retried", {"max_attempts": max_attempts})
            return self._task(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def set_lease(self, task_id: str, attempt_id: str, node_id: str, endpoint: str, site: str) -> None:
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO leases(task_id, attempt_id, node_id, endpoint, site, acquired_at, heartbeat_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET attempt_id=excluded.attempt_id,
                    node_id=excluded.node_id, endpoint=excluded.endpoint, site=excluded.site,
                    acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at
                """,
                (task_id, attempt_id, node_id, endpoint, site, now, now),
            )
            self._event(conn, task_id, "proxy_acquired", {"node_id": node_id, "endpoint": endpoint})

    def clear_lease(self, task_id: str, attempt_id: str | None = None) -> None:
        with self._transaction() as conn:
            if attempt_id is None:
                conn.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
            else:
                conn.execute(
                    "DELETE FROM leases WHERE task_id=? AND attempt_id=?",
                    (task_id, attempt_id),
                )

    def append_log(self, task_id: str, attempt_id: str | None, stream: str, line: str) -> int:
        safe = redact_text(line, limit=8000)
        with self._transaction() as conn:
            cur = conn.execute(
                "INSERT INTO task_logs(task_id, attempt_id, ts, stream, line) VALUES (?, ?, ?, ?, ?)",
                (task_id, attempt_id, time.time(), stream, safe),
            )
            log_id = int(cur.lastrowid)
            if log_id % 100 == 0:
                conn.execute(
                    """
                    DELETE FROM task_logs WHERE task_id=? AND id NOT IN (
                        SELECT id FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (task_id, task_id, self.max_logs_per_task),
                )
            return log_id

    def get_logs(self, task_id: str, *, since: int = 0, tail: int | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            if tail is not None:
                rows = self._conn.execute(
                    "SELECT * FROM task_logs WHERE task_id=? ORDER BY id DESC LIMIT ?",
                    (task_id, max(1, min(int(tail), 5000))),
                ).fetchall()
                return [dict(row) for row in reversed(rows)]
            rows = self._conn.execute(
                "SELECT * FROM task_logs WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (task_id, max(0, int(since)), max(1, min(int(limit), 5000))),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_events(self, task_id: str, *, since: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_events WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (task_id, max(0, int(since)), max(1, min(int(limit), 5000))),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                data = dict(row)
                try:
                    data["payload"] = json.loads(data.pop("payload_json") or "{}")
                except Exception:
                    data["payload"] = {}
                result.append(data)
            return result

    def update_artifacts(self, task_id: str, count: int, total_bytes: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE tasks SET artifact_count=?, artifact_bytes=?, updated_at=? WHERE id=?",
                (max(0, int(count)), max(0, int(total_bytes)), time.time(), task_id),
            )

    def put_site_policy(self, site: str, policy: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO site_policies(site, policy_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(site) DO UPDATE SET policy_json=excluded.policy_json, updated_at=excluded.updated_at
                """,
                (site, json.dumps(policy, ensure_ascii=False), now),
            )
        return {"site": site, "policy": policy, "updated_at": now}

    def get_site_policy(self, site: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM site_policies WHERE site=?", (site,)).fetchone()
            if row is None:
                return None
            return {"site": row["site"], "policy": json.loads(row["policy_json"]), "updated_at": row["updated_at"]}

    def list_site_policies(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM site_policies ORDER BY site").fetchall()
            return [
                {"site": row["site"], "policy": json.loads(row["policy_json"]), "updated_at": row["updated_at"]}
                for row in rows
            ]

    def delete_site_policy(self, site: str) -> bool:
        with self._transaction() as conn:
            return bool(conn.execute("DELETE FROM site_policies WHERE site=?", (site,)).rowcount)

    def incomplete_processes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, pid, process_marker, status, attempt_count, max_attempts FROM tasks WHERE status IN ('starting','running','cancelling')"
            ).fetchall()
            return [dict(row) for row in rows]

    def recover_incomplete(self) -> int:
        now = time.time()
        recovered = 0
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN ('starting','running','cancelling')"
            ).fetchall()
            for row in rows:
                task_id = row["id"]
                if row["cancel_requested"]:
                    conn.execute(
                        """
                        UPDATE tasks SET status='cancelled', finished_at=?, updated_at=?,
                            pid=NULL, process_marker=NULL, last_error_class='backend_restart',
                            last_error='后端重启时任务处于取消流程' WHERE id=?
                        """,
                        (now, now, task_id),
                    )
                    self._event(conn, task_id, "cancelled_after_restart")
                elif int(row["attempt_count"]) < int(row["max_attempts"]):
                    conn.execute(
                        """
                        UPDATE tasks SET status='queued', next_run_at=?, updated_at=?,
                            pid=NULL, process_marker=NULL, last_error_class='backend_restart',
                            last_error='后端重启，任务重新排队' WHERE id=?
                        """,
                        (now, now, task_id),
                    )
                    self._event(conn, task_id, "requeued_after_restart")
                else:
                    conn.execute(
                        """
                        UPDATE tasks SET status='failed', finished_at=?, updated_at=?,
                            pid=NULL, process_marker=NULL, last_error_class='backend_restart',
                            last_error='后端重启且重试次数已用尽' WHERE id=?
                        """,
                        (now, now, task_id),
                    )
                    self._event(conn, task_id, "failed_after_restart")
                conn.execute(
                    """
                    UPDATE attempts SET status='orphaned', finished_at=?,
                        error_class='backend_restart', error_message='后端重启'
                    WHERE task_id=? AND status='running'
                    """,
                    (now, task_id),
                )
                conn.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
                recovered += 1
        return recovered
