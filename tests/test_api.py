import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import HTTPServer
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asus_ports


class TestDashboardAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start server on dynamic random port
        cls.server = HTTPServer(("127.0.0.1", 0), asus_ports.DashboardHandler)
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
            self.assertIn("Port Forward Manager", html)

    def test_api_status(self):
        req = urllib.request.Request(f"{self.base_url}/api/status")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("server_reachable", data)
            self.assertIn("forwards", data)
            self.assertIn("server_name", data)
            self.assertIn("server_host", data)

    def test_api_scan(self):
        req = urllib.request.Request(f"{self.base_url}/api/scan")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("services", data)
            self.assertIsInstance(data["services"], list)

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
