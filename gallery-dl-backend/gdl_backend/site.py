from __future__ import annotations

import importlib
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(slots=True)
class SiteInfo:
    site: str
    subcategory: str
    extractor: str
    supported: bool
    host: str


class SiteResolver:
    """Resolve gallery-dl category without initializing or running an extractor."""

    def __init__(self, gallery_repo: Path) -> None:
        self.gallery_repo = gallery_repo.resolve()
        self._lock = threading.Lock()
        self._extractor_module = None

    @staticmethod
    def _plain_url(value: str) -> str:
        text = value.strip()
        lower = text.lower()
        pos = lower.find("http://")
        pos2 = lower.find("https://")
        starts = [p for p in (pos, pos2) if p >= 0]
        return text[min(starts)] if starts else text

    @staticmethod
    def _host(value: str) -> str:
        try:
            host = (urlsplit(SiteResolver._plain_url(value)).hostname or "").lower()
        except Exception:
            host = ""
        return host[4:] if host.startswith("www.") else host

    def _module(self):
        if self._extractor_module is not None:
            return self._extractor_module
        if not self.gallery_repo.is_dir():
            return None
        repo_text = str(self.gallery_repo)
        if repo_text not in sys.path:
            sys.path.insert(0, repo_text)
        module = importlib.import_module("gallery_dl.extractor")
        module_path = Path(module.__file__).resolve()
        if not module_path.is_relative_to(self.gallery_repo):
            raise RuntimeError(f"加载到错误的 gallery-dl: {module_path}")
        self._extractor_module = module
        return module

    def resolve(self, url: str) -> SiteInfo:
        host = self._host(url)
        fallback = host or "unknown"
        with self._lock:
            module = self._module()
            if module is None:
                return SiteInfo(fallback, "", "", False, host)
            extractor = module.find(url)
        if extractor is None:
            return SiteInfo(fallback, "", "", False, host)
        category = str(getattr(extractor, "category", "") or fallback).lower()
        subcategory = str(getattr(extractor, "subcategory", "") or "").lower()
        class_name = extractor.__class__.__name__
        if category in {"generic", "directlink", "recursive"} and host:
            category = host
        return SiteInfo(category, subcategory, class_name, True, host)
