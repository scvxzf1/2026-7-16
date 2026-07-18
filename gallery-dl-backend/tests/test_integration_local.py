from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from gdl_backend.app import ServiceContainer, create_app

from tests.helpers import make_settings


class LocalGalleryIntegrationTests(unittest.TestCase):
    def test_real_gallery_subprocess_downloads_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            served = root / "served"
            served.mkdir()
            payload = b"\xff\xd8\xff\xe0backend-integration\xff\xd9"
            (served / "sample.jpg").write_bytes(payload)

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(served), **kwargs)

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                settings = make_settings(root)
                container = ServiceContainer(settings)
                app = create_app(settings, container=container, start_background=True)
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/tasks",
                        json={
                            "url": f"http://127.0.0.1:{server.server_port}/sample.jpg",
                            "proxy_mode": "direct",
                            "max_attempts": 1,
                        },
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    task_id = response.json()["id"]
                    deadline = time.time() + 8
                    while time.time() < deadline:
                        task = client.get(f"/api/v1/tasks/{task_id}").json()
                        if task["status"] in {"succeeded", "failed", "cancelled"}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(task["status"], "succeeded", task)
                    self.assertEqual(task["artifact_count"], 1)
                    self.assertEqual(task["artifact_bytes"], len(payload))
            finally:
                server.shutdown()
                server.server_close()

    def test_real_gallery_download_uses_native_http_proxy_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            served = root / "served"
            served.mkdir()
            payload = b"\xff\xd8\xff\xe0proxy-integration\xff\xd9"
            (served / "proxied.jpg").write_bytes(payload)

            class OriginHandler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(served), **kwargs)

                def log_message(self, *args):
                    pass

            origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
            origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
            origin_thread.start()

            class ProxyHandler(BaseHTTPRequestHandler):
                hits = 0
                lock = threading.Lock()

                def do_GET(self):
                    with self.lock:
                        type(self).hits += 1
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    request = urllib.request.Request(
                        self.path,
                        headers={"User-Agent": self.headers.get("User-Agent", "")},
                    )
                    with opener.open(request, timeout=5) as response:
                        body = response.read()
                        self.send_response(response.status)
                        self.send_header(
                            "Content-Type",
                            response.headers.get("Content-Type", "application/octet-stream"),
                        )
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                def log_message(self, *args):
                    pass

            proxy_server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
            proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
            proxy_thread.start()
            old_no_proxy = os.environ.get("NO_PROXY")
            old_no_proxy_lower = os.environ.get("no_proxy")
            os.environ["NO_PROXY"] = ""
            os.environ["no_proxy"] = ""
            try:
                settings = make_settings(root)
                settings.proxy.enabled = True
                settings.proxy.auto_start = True
                settings.proxy.inline_nodes = [
                    f"http://127.0.0.1:{proxy_server.server_port}#LOCAL"
                ]
                container = ServiceContainer(settings)
                container.proxy.probe = lambda **_: {
                    "total": 1,
                    "healthy": 1,
                    "results": [],
                }
                app = create_app(settings, container=container, start_background=True)
                with TestClient(app) as client:
                    response = client.post(
                        "/api/v1/tasks",
                        json={
                            "url": f"http://127.0.0.1:{origin.server_port}/proxied.jpg",
                            "proxy_mode": "required",
                            "max_attempts": 1,
                        },
                    )
                    self.assertEqual(response.status_code, 202, response.text)
                    task_id = response.json()["id"]
                    deadline = time.time() + 8
                    while time.time() < deadline:
                        task = client.get(f"/api/v1/tasks/{task_id}").json()
                        if task["status"] in {"succeeded", "failed", "cancelled"}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(task["status"], "succeeded", task)
                    self.assertGreater(ProxyHandler.hits, 0)
                    self.assertEqual(task["artifact_bytes"], len(payload))
            finally:
                if old_no_proxy is None:
                    os.environ.pop("NO_PROXY", None)
                else:
                    os.environ["NO_PROXY"] = old_no_proxy
                if old_no_proxy_lower is None:
                    os.environ.pop("no_proxy", None)
                else:
                    os.environ["no_proxy"] = old_no_proxy_lower
                proxy_server.shutdown()
                proxy_server.server_close()
                origin.shutdown()
                origin.server_close()


if __name__ == "__main__":
    unittest.main()
