from __future__ import annotations

import tempfile
from pathlib import Path

from gdl_backend.config import AppSettings


WORKSPACE = Path(__file__).resolve().parents[2]


def make_settings(root: Path) -> AppSettings:
    settings = AppSettings.load(root / "missing-config.json")
    settings.runtime_dir = root / "runtime"
    settings.database_path = settings.runtime_dir / "backend.sqlite3"
    settings.default_output_root = settings.runtime_dir / "downloads"
    settings.allowed_output_roots = [settings.default_output_root]
    settings.allowed_config_roots = [root / "credentials"]
    settings.allowed_cookie_roots = [root / "credentials"]
    settings.gallery.repo_path = WORKSPACE / "gallery-dl-codeberg"
    settings.gallery.cache_file = root / "credentials" / "managed" / "gallery-dl-cache.sqlite3"
    settings.gallery.migrate_default_auth = False
    settings.proxy.enabled = False
    settings.proxy.auto_start = False
    settings.server.allow_private_targets = True
    settings.scheduler.poll_interval_seconds = 0.05
    settings.scheduler.retry_jitter_seconds = 0.0
    settings.ensure_directories()
    (root / "credentials").mkdir(parents=True, exist_ok=True)
    return settings
