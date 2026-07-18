from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path


def secure_private_path(path: Path) -> None:
    """Restrict a managed credential path to its owner and system administrators."""

    path = Path(path)
    if not path.exists():
        return
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return

    username = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    identity = f"{domain}\\{username}" if domain and username else username
    if not identity:
        return
    inherit = "(OI)(CI)" if path.is_dir() else ""
    grants = (
        f"{identity}:{inherit}(F)",
        f"*S-1-5-18:{inherit}(F)",
        f"*S-1-5-32-544:{inherit}(F)",
    )
    try:
        subprocess.run(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r", *grants],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass
