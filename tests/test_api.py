import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
import sys
import os
import types
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestDashboardAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start server on dynamic random port
        cls.server = HTTPServer(("127.0.0.1", 0), devboost.DashboardHandler)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_get_dashboard_html(self):
        req = urllib.request.Request(self.base_url)
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type"))
            html = resp.read().decode("utf-8")
            self.assertIn("DevBoost", html)
            self.assertIn('/dashboard/styles.css', html)
            self.assertIn('/dashboard/app.js', html)

    def test_dashboard_assets_are_served_separately(self):
        for path, marker, content_type in (
            ("/dashboard/styles.css", "--bg:", "text/css"),
            ("/dashboard/app.js", "fetchServers", "text/javascript"),
        ):
            with urllib.request.urlopen(self.base_url + path) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(content_type, resp.headers.get("Content-Type"))
                self.assertIn(marker, resp.read().decode("utf-8"))

    def test_api_status(self):
        req = urllib.request.Request(f"{self.base_url}/api/status")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("server_reachable", data)
            self.assertIn("forwards", data)
            self.assertIn("server_name", data)
            self.assertIn("server_host", data)

    def test_service_binder_ignores_main_wrappers(self):
        """Running the packaged script as __main__ must not recurse in /api/status."""
        def wrapper(*args, **kwargs):
            return None

        wrapper.__module__ = "__main__"
        runtime = types.SimpleNamespace(__name__="__main__", get_all_forwards_status=wrapper)
        bound = devboost.forwarding._bound_functions(runtime)
        self.assertIsNot(bound["get_all_forwards_status"], wrapper)
        self.assertEqual(bound["get_all_forwards_status"].__module__, "devboost_app.forwarding")

    def test_api_scan(self):
        req = urllib.request.Request(f"{self.base_url}/api/scan")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("services", data)
            self.assertIsInstance(data["services"], list)

    @patch.object(devboost.DOCKER_MONITOR, "get")
    def test_api_docker(self, mock_docker):
        mock_docker.return_value = {
            "ok": True,
            "available": True,
            "containers": [{"name": "web", "id": "abc", "stats": {"cpu_percent": "1%"}}],
            "stats": [],
            "updated_at": 123,
            "refreshing": False,
        }
        req = urllib.request.Request(f"{self.base_url}/api/docker")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["available"])
            self.assertEqual(data["containers"][0]["name"], "web")

    def test_api_clean(self):
        req = urllib.request.Request(f"{self.base_url}/api/clean", data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data.get("ok"))
            self.assertIn("killed", data)

    def test_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base_url}/non-existent-endpoint")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
