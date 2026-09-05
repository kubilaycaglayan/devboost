"""Real mouse-drag regression, using Chrome and standard-library tooling only."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse


ROOT = Path(__file__).resolve().parents[1]


def chrome_executable():
    candidates = [os.environ.get("DEVBOOST_TEST_CHROME"),
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  shutil.which("google-chrome"), shutil.which("chromium"),
                  shutil.which("chromium-browser")]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


class TestServerDragBrowser(unittest.TestCase):
    def test_native_mouse_drag_reorders_cards_and_tabs(self):
        chrome = chrome_executable()
        if not chrome or not shutil.which("node"):
            self.skipTest("Real drag regression requires Chrome/Chromium and Node")
        with tempfile.TemporaryDirectory(prefix="devboost-browser-state-") as app_dir:
            env = dict(os.environ, DEVBOOST_APP_DIR=app_dir)
            server = subprocess.Popen(
                [sys.executable, __file__, "--serve"], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                url = server.stdout.readline().strip()
                self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                result = subprocess.run(
                    ["node", str(ROOT / "tests" / "server_drag_browser.js"), chrome, url],
                    cwd=ROOT, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                server.terminate()
                server.communicate(timeout=10)

    def test_usage_actions_remain_visible_without_horizontal_scroll(self):
        chrome = chrome_executable()
        if not chrome or not shutil.which("node"):
            self.skipTest("Visual Quotas regression requires Chrome/Chromium and Node")
        with tempfile.TemporaryDirectory(prefix="devboost-usage-browser-state-") as app_dir:
            env = dict(os.environ, DEVBOOST_APP_DIR=app_dir)
            server = subprocess.Popen(
                [sys.executable, __file__, "--serve"], cwd=ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                url = server.stdout.readline().strip()
                self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                result = subprocess.run(
                    ["node", str(ROOT / "tests" / "usage_visual_browser.js"), chrome, url],
                    cwd=ROOT, capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                server.terminate()
                server.communicate(timeout=10)


def serve_fixture():
    """Use actual dashboard/reorder persistence; never start SSH or scan the host."""
    app_dir = Path(os.environ["DEVBOOST_APP_DIR"])
    servers = [dict(id=name, ssh_host=f"{name}.invalid", name=name.title(),
                    ip="", order=index, pinned=False)
               for index, name in enumerate(("alpha", "beta", "gamma"))]
    (app_dir / "config.json").write_text(json.dumps({"servers": servers}), encoding="utf-8")
    sys.path.insert(0, str(ROOT))
    import devboost

    class FixtureHandler(devboost.DashboardHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            if path == "/api/servers":
                return self._send_json({"servers": devboost.get_servers()})
            if path == "/api/status":
                return self._send_json({"servers": devboost.get_servers(),
                                        "server_id": query.get("server", ["alpha"])[0],
                                        "server_reachable": False, "forwards": [],
                                        "history": [], "orphaned_count": 0})
            if path == "/api/usage":
                return self._send_json({
                    "accounts": [{"id": "visual", "provider": "codex", "name": "Visual quota"}],
                    "snapshots": {"visual": {"ok": True, "quotas": [
                        {"name": "Primary", "used": 42, "limit": 100, "unit": "%"}
                    ], "balances": [{"remaining": 12, "currency": "USD"}], "updated_at": "2026-09-06T00:00:00Z"}},
                    "server_id": query.get("server", ["alpha"])[0],
                })
            if path.startswith("/api/"):
                return self._send_json({"accounts": [], "snapshots": {}, "containers": [],
                                        "syncs": [], "ports": [], "history": []})
            baseline_dir = os.environ.get("DEVBOOST_TEST_DASHBOARD_DIR")
            if baseline_dir and path in ("/dashboard/app.js", "/dashboard/styles.css"):
                data = (Path(baseline_dir) / path.rsplit("/", 1)[-1]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript" if path.endswith(".js") else "text/css")
                self.end_headers()
                self.wfile.write(data)
                return
            super().do_GET()

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/api/servers/reorder":
                return self._send_json({"ok": False}, status=403)
            super().do_POST()

    server = devboost.ReusableHTTPServer(("127.0.0.1", 0), FixtureHandler)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve_fixture()
    else:
        unittest.main()
