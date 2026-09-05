import os
import socket
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _pid_listening_on(port, timeout=6.0):
    """Returns the pid listening on port, polling (server startup takes a beat)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for info in devboost.get_listening_ports().values():
            if isinstance(info, dict) and info.get("pid"):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        pass
                except OSError:
                    continue
                # Confirm THIS pid holds OUR port (not just any listener).
                rows = devboost.get_local_listening_ports()
                for r in rows:
                    if r["port"] == port:
                        return r["pid"]
        time.sleep(0.2)
    return None


class TestLocalPorts(unittest.TestCase):
    def test_listing_shape(self):
        rows = devboost.get_local_listening_ports()
        self.assertIsInstance(rows, list)
        ports = []
        for r in rows:
            for key in ("port", "pid", "cmd", "node", "devboost"):
                self.assertIn(key, r)
            self.assertIsInstance(r["port"], int)
            ports.append(r["port"])
        self.assertEqual(ports, sorted(ports))

    def test_kill_refuses_pid_1(self):
        res = devboost.kill_listening_process(1)
        self.assertFalse(res["ok"])
        self.assertIn("pid 1", res["message"])

    def test_kill_refuses_self(self):
        res = devboost.kill_listening_process(os.getpid())
        self.assertFalse(res["ok"])
        self.assertIn("dashboard itself", res["message"])

    def test_kill_refuses_non_listener(self):
        res = devboost.kill_listening_process(99999999)
        self.assertFalse(res["ok"])

    def test_kill_refuses_garbage(self):
        res = devboost.kill_listening_process("not-a-pid")
        self.assertFalse(res["ok"])

    def test_kill_real_listener(self):
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            pid = _pid_listening_on(port)
            self.assertIsNotNone(pid, "test server never appeared in lsof")
            res = devboost.kill_listening_process(pid)
            self.assertTrue(res["ok"], res["message"])
            # Port actually freed.
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if port not in devboost.get_listening_ports():
                    break
                time.sleep(0.2)
            self.assertNotIn(port, devboost.get_listening_ports())
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
