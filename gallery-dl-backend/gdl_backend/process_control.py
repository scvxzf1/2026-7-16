from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path


async def terminate_process(process: asyncio.subprocess.Process, grace_seconds: float) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except Exception:
            try:
                process.terminate()
            except ProcessLookupError:
                return
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except ProcessLookupError:
                return
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.2, float(grace_seconds)))
        return
    except asyncio.TimeoutError:
        pass
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            return


def _decode_output(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def process_command_line(pid: int) -> str:
    if int(pid) <= 0:
        return ""
    if os.name == "nt":
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {int(pid)}\"; "
            "if ($p) { [Console]::OutputEncoding=[Text.Encoding]::UTF8; $p.CommandLine }"
        )
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                check=False,
                timeout=8,
            )
            return _decode_output(result.stdout).strip()
        except Exception:
            return ""
    path = Path(f"/proc/{int(pid)}/cmdline")
    try:
        return path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def terminate_stale_process(pid: int | None, marker: str | None) -> bool:
    if not pid or not marker:
        return False
    command = process_command_line(int(pid))
    if not command or marker not in command or "gdl_backend.worker_entry" not in command:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not process_command_line(int(pid)):
                return True
            time.sleep(0.05)
        return not bool(process_command_line(int(pid)))
    try:
        os.killpg(int(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False
