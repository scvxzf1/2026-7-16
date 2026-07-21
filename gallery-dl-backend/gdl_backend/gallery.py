from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from .config import GallerySettings
from .process_control import terminate_process
from .redaction import redact_text
from .schemas import EHDownloadOptions


LineCallback = Callable[[str, str], Awaitable[None]]
StartedCallback = Callable[[int, str], Awaitable[None]]
_TWITTER_WEB_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def _is_twitter_web_url(url: str) -> bool:
    lower = url.lower()
    positions = [position for position in (lower.find("http://"), lower.find("https://")) if position >= 0]
    plain_url = url[min(positions) :] if positions else url
    return (urlsplit(plain_url).hostname or "").lower() in _TWITTER_WEB_HOSTS


@dataclass(slots=True)
class GalleryRunResult:
    exit_code: int | None
    output_tail: str
    timed_out: bool
    marker: str
    pid: int


@dataclass(slots=True)
class GalleryCaptureResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    marker: str
    pid: int


class _CaptureLimitExceeded(RuntimeError):
    pass


class GalleryRunner:
    def __init__(self, settings: GallerySettings, backend_root: Path) -> None:
        self.settings = settings
        self.backend_root = backend_root.resolve()
        self._active: dict[str, tuple[asyncio.subprocess.Process, str]] = {}
        self._lock = asyncio.Lock()

    def validate_args(self, args: list[str]) -> list[str]:
        values = [str(arg) for arg in args]
        forbidden = list(self.settings.forbidden_args)
        for arg in values:
            for item in forbidden:
                if arg == item or arg.startswith(item + "="):
                    raise ValueError(f"gallery-dl 参数由后端管理: {item}")
                if item in {"-d", "-D", "-S", "-o", "-c", "-C", "-u", "-p"} and arg.startswith(item):
                    raise ValueError(f"gallery-dl 参数由后端管理: {item}")
        return values

    @staticmethod
    def _config_args(config_file: str | None) -> list[str]:
        if not config_file:
            return []
        suffix = Path(config_file).suffix.lower()
        if suffix in {".yaml", ".yml"}:
            return ["--config-yaml", config_file]
        if suffix == ".toml":
            return ["--config-toml", config_file]
        if suffix == ".json":
            return ["--config-json", config_file]
        return ["--config", config_file]

    @staticmethod
    def _credentials(credentials_ref: str | None) -> tuple[str, str]:
        if not credentials_ref:
            return "", ""
        key = re.sub(r"[^A-Za-z0-9]", "_", credentials_ref).upper()
        prefix = f"GDL_CREDENTIAL_{key}_"
        username = os.environ.get(prefix + "USERNAME", "")
        password = os.environ.get(prefix + "PASSWORD", "")
        if not username and not password:
            raise ValueError(f"credentials_ref 未配置对应环境变量: {credentials_ref}")
        return username, password

    @staticmethod
    def _eh_option_args(
        site: str | None,
        options: EHDownloadOptions | None,
    ) -> list[str]:
        if site != "exhentai" or options is None:
            return []
        original = options.image_mode == "original"
        args = [
            "--option",
            f"extractor.exhentai.original={'true' if original else 'false'}",
        ]
        if original:
            args.extend(
                ["--option", f"extractor.exhentai.gp={options.gp_policy}"]
            )
        return args

    def build_command(
        self,
        *,
        marker: str,
        url: str,
        output_dir: str,
        proxy_url: str | None,
        http_timeout: float,
        gallery_retries: int,
        cookies_file: str | None,
        config_file: str | None,
        extra_args: list[str],
        site: str | None = None,
        eh_download: EHDownloadOptions | None = None,
    ) -> list[str]:
        command = [
            self.settings.python_executable or sys.executable,
            "-m",
            "gdl_backend.worker_entry",
            "--marker",
            marker,
            "--gallery-root",
            str(self.settings.repo_path),
            "--",
            "--config-ignore",
            "--cache-file",
            str(self.settings.cache_file),
            "--no-colors",
            "--no-input",
            "--destination",
            output_dir,
            "--http-timeout",
            str(float(http_timeout)),
            "--retries",
            str(int(gallery_retries)),
        ]
        command.extend(self._config_args(config_file))
        if cookies_file:
            command.extend(["--cookies", cookies_file])
        if proxy_url:
            command.extend(["--proxy", proxy_url])
        command.extend(self.validate_args(extra_args))
        command.extend(self._eh_option_args(site, eh_download))
        if cookies_file and _is_twitter_web_url(url):
            command.extend(["--option", "extractor.twitter.cookies-update=false"])
        command.append(url)
        return command

    async def run(
        self,
        task_id: str,
        *,
        url: str,
        output_dir: str,
        proxy_url: str | None,
        http_timeout: float,
        gallery_retries: int,
        task_timeout: float,
        cookies_file: str | None,
        config_file: str | None,
        credentials_ref: str | None,
        extra_args: list[str],
        on_line: LineCallback,
        on_started: StartedCallback,
        site: str | None = None,
        eh_download: EHDownloadOptions | None = None,
    ) -> GalleryRunResult:
        if not (self.settings.repo_path / "gallery_dl" / "__init__.py").is_file():
            raise FileNotFoundError(f"gallery-dl 源码目录无效: {self.settings.repo_path}")
        marker = f"{task_id}-{uuid.uuid4().hex}"
        command = self.build_command(
            marker=marker,
            url=url,
            output_dir=output_dir,
            proxy_url=proxy_url,
            http_timeout=http_timeout,
            gallery_retries=gallery_retries,
            cookies_file=cookies_file,
            config_file=config_file,
            extra_args=extra_args,
            site=site,
            eh_download=eh_download,
        )
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        pythonpath = [str(self.backend_root), str(self.settings.repo_path)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        username, password = self._credentials(credentials_ref)
        if username:
            env["GDL_WORKER_USERNAME"] = username
        if password:
            env["GDL_WORKER_PASSWORD"] = password

        kwargs: dict[str, object] = {
            "cwd": str(self.settings.repo_path),
            "env": env,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*command, **kwargs)
        async with self._lock:
            self._active[task_id] = (process, marker)
        await on_started(process.pid, marker)

        tail: deque[str] = deque(maxlen=250)

        async def read_stream(stream: asyncio.StreamReader | None, name: str) -> None:
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                safe = redact_text(line, secrets=(username, password), limit=self.settings.max_log_line_chars)
                tail.append(f"[{name}] {safe}")
                await on_line(name, safe)

        readers = [
            asyncio.create_task(read_stream(process.stdout, "stdout")),
            asyncio.create_task(read_stream(process.stderr, "stderr")),
        ]
        timed_out = False
        try:
            if task_timeout and task_timeout > 0:
                try:
                    await asyncio.wait_for(process.wait(), timeout=task_timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    await terminate_process(process, self.settings.terminate_grace_seconds)
            else:
                await process.wait()
        finally:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*readers, return_exceptions=True),
                    timeout=max(2.0, self.settings.terminate_grace_seconds),
                )
            except asyncio.TimeoutError:
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
            async with self._lock:
                current = self._active.get(task_id)
                if current and current[0] is process:
                    self._active.pop(task_id, None)
        return GalleryRunResult(process.returncode, "\n".join(tail), timed_out, marker, process.pid)

    async def capture(
        self,
        operation_id: str,
        *,
        url: str,
        output_dir: str,
        proxy_url: str | None,
        http_timeout: float,
        gallery_retries: int,
        task_timeout: float,
        cookies_file: str | None,
        config_file: str | None,
        credentials_ref: str | None,
        extra_args: list[str],
        max_output_bytes: int = 64 * 1024 * 1024,
    ) -> GalleryCaptureResult:
        """Run a metadata-only gallery-dl job and return its complete protocol output."""
        if not (self.settings.repo_path / "gallery_dl" / "__init__.py").is_file():
            raise FileNotFoundError(f"gallery-dl 源码目录无效: {self.settings.repo_path}")
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        marker = f"{operation_id}-{uuid.uuid4().hex}"
        command = self.build_command(
            marker=marker,
            url=url,
            output_dir=output_dir,
            proxy_url=proxy_url,
            http_timeout=http_timeout,
            gallery_retries=gallery_retries,
            cookies_file=cookies_file,
            config_file=config_file,
            extra_args=extra_args,
        )
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        pythonpath = [str(self.backend_root), str(self.settings.repo_path)]
        if env.get("PYTHONPATH"):
            pythonpath.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        username, password = self._credentials(credentials_ref)
        if username:
            env["GDL_WORKER_USERNAME"] = username
        if password:
            env["GDL_WORKER_PASSWORD"] = password

        kwargs: dict[str, object] = {
            "cwd": str(self.settings.repo_path),
            "env": env,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*command, **kwargs)
        async with self._lock:
            self._active[operation_id] = (process, marker)

        captured_bytes = 0
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def read_limited(
            stream: asyncio.StreamReader | None,
            chunks: list[bytes],
        ) -> None:
            nonlocal captured_bytes
            if stream is None:
                return
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return
                captured_bytes += len(chunk)
                chunks.append(chunk)
                if captured_bytes > max_output_bytes:
                    raise _CaptureLimitExceeded

        readers = [
            asyncio.create_task(read_limited(process.stdout, stdout_chunks)),
            asyncio.create_task(read_limited(process.stderr, stderr_chunks)),
        ]
        waiter = asyncio.create_task(process.wait())
        group = asyncio.gather(waiter, *readers)
        cleanup_timeout = max(
            1.0,
            min(5.0, float(self.settings.terminate_grace_seconds)),
        )

        async def settle_group() -> None:
            try:
                await asyncio.wait_for(asyncio.shield(group), timeout=cleanup_timeout)
            except BaseException:
                pass
            tasks = [waiter, *readers]
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                done, still_pending = await asyncio.wait(pending, timeout=cleanup_timeout)
                for task in still_pending:
                    task.cancel()
                for task in done:
                    if not task.cancelled():
                        try:
                            task.exception()
                        except BaseException:
                            pass
            if not group.done():
                group.cancel()
            try:
                group.exception()
            except BaseException:
                pass

        timed_out = False
        try:
            if task_timeout and task_timeout > 0:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(group),
                        timeout=task_timeout,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    await terminate_process(process, self.settings.terminate_grace_seconds)
                    await settle_group()
            else:
                await group
        except _CaptureLimitExceeded as exc:
            await terminate_process(process, self.settings.terminate_grace_seconds)
            await settle_group()
            raise ValueError(f"gallery-dl 元数据输出超过 {max_output_bytes} 字节上限") from exc
        except asyncio.CancelledError:
            await terminate_process(process, self.settings.terminate_grace_seconds)
            await settle_group()
            raise
        except Exception:
            await terminate_process(process, self.settings.terminate_grace_seconds)
            await settle_group()
            raise
        finally:
            async with self._lock:
                current = self._active.get(operation_id)
                if current and current[0] is process:
                    self._active.pop(operation_id, None)

        stdout_raw = b"".join(stdout_chunks)
        stderr_raw = b"".join(stderr_chunks)
        stdout = stdout_raw.decode("utf-8", "replace")
        for secret in (username, password):
            if secret:
                stdout = stdout.replace(secret, "***")
        stderr = redact_text(
            stderr_raw.decode("utf-8", "replace"),
            secrets=(username, password),
            limit=max_output_bytes,
        )
        return GalleryCaptureResult(
            process.returncode,
            stdout,
            stderr,
            timed_out,
            marker,
            process.pid,
        )

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            active = self._active.get(task_id)
        if active is None:
            return False
        await terminate_process(active[0], self.settings.terminate_grace_seconds)
        return True

    async def stop_all(self) -> None:
        async with self._lock:
            active = list(self._active.values())
        await asyncio.gather(
            *(terminate_process(process, self.settings.terminate_grace_seconds) for process, _ in active),
            return_exceptions=True,
        )

    async def active_count(self) -> int:
        async with self._lock:
            return len(self._active)
