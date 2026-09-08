import importlib.util
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


SPEC = importlib.util.spec_from_file_location(
    "send_state_to_phone", "scripts/send_state_to_phone.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SendStateTests(unittest.TestCase):
    def test_redacted_summary_omits_personal_fields(self):
        summary = MODULE._redacted_summary({
            "status": {"forwards": [{"active": True}, {"active": False}]},
            "servers": {"servers": [{"reachable": True, "ssh_host": "private.example"}]},
            "syncs": {"syncs": [{"last_status": "ok", "local_path": "/private/path"}]},
            "docker": {"available": True, "containers": [{"name": "private-container"}]},
            "usage": {
                "accounts": [{"id": "private-account", "name": "Personal"}],
                "snapshots": {"private-account": {"ok": True}},
            },
        })
        self.assertIn("Selected server: offline (1 configured)", summary)
        self.assertNotIn("private.example", summary)
        self.assertNotIn("/private/path", summary)
        self.assertNotIn("Personal", summary)
        self.assertNotIn("private-container", summary)

    def test_send_ntfy_posts_topic_and_message(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received["path"] = self.path
                received["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
                received["title"] = self.headers.get("Title")
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            MODULE.send_ntfy("hello", f"http://127.0.0.1:{server.server_port}", "private topic", "DevBoost")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
        self.assertEqual(urlparse(received["path"]).path, "/private%20topic")
        self.assertEqual(received["body"], "hello")
        self.assertEqual(received["title"], "DevBoost")


if __name__ == "__main__":
    unittest.main()
