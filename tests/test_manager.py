import os
import plistlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_launch_dir = devboost.LAUNCH_AGENTS_DIR
        self.orig_log_dir = devboost.LOG_DIR
        devboost.LAUNCH_AGENTS_DIR = self.temp_dir.name
        devboost.LOG_DIR = self.temp_dir.name

    def tearDown(self):
        devboost.LAUNCH_AGENTS_DIR = self.orig_launch_dir
        devboost.LOG_DIR = self.orig_log_dir
        self.temp_dir.cleanup()

    def test_plist_label_and_path(self):
        label = devboost.get_plist_label(3030)
        self.assertTrue(label.endswith("-3030"))
        path = devboost.get_plist_path(3030)
        self.assertTrue(path.endswith("-3030.plist"))
        self.assertTrue(path.startswith(self.temp_dir.name))

    @patch("subprocess.run")
    def test_create_launchagent_structure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        plist_path = devboost.create_launchagent(8080, 8080)
        self.assertTrue(os.path.exists(plist_path))

        with open(plist_path, "rb") as f:
            data = plistlib.load(f)

        self.assertEqual(data["Label"], devboost.get_plist_label(8080))
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(data["ThrottleInterval"], 10)

        # Verify arguments (scoped DevBoost wrapper, not raw /usr/bin/ssh)
        args = data["ProgramArguments"]
        self.assertTrue(args[0].endswith("DevBoost-tunnel"))
        self.assertNotIn("/usr/bin/ssh", args)
        self.assertIn("-N", args)
        self.assertIn("-L", args)
        self.assertIn("8080:127.0.0.1:8080", args)
        self.assertIn(devboost.SSH_HOST, args)

    def test_get_launchagents_discovery(self):
        # Create 2 mock plists
        p1 = os.path.join(self.temp_dir.name, f"{devboost.AGENT_PREFIX}-3000.plist")
        p2 = os.path.join(self.temp_dir.name, f"{devboost.AGENT_PREFIX}-9000.plist")

        for port, path in [(3000, p1), (9000, p2)]:
            data = {
                "Label": devboost.get_plist_label(port),
                "ProgramArguments": ["ssh", "-N", "-L", f"{port}:127.0.0.1:{port}", "remote-host"]
            }
            with open(path, "wb") as f:
                plistlib.dump(data, f)

        agents = devboost.get_launchagents()
        self.assertIn(3000, agents)
        self.assertIn(9000, agents)
        self.assertEqual(agents[3000]["remote_port"], 3000)
        self.assertEqual(agents[9000]["remote_port"], 9000)

    @patch("subprocess.run")
    @patch("devboost.get_packaged_app_executable")
    def test_packaged_app_migrates_legacy_tunnel_agent(self, mock_packaged, mock_run):
        packaged = "/Applications/DevBoost.app/Contents/MacOS/DevBoost"
        mock_packaged.return_value = packaged
        plist_path = os.path.join(self.temp_dir.name, f"{devboost.AGENT_PREFIX}-3000.plist")
        with open(plist_path, "wb") as f:
            plistlib.dump({
                "Label": devboost.get_plist_label(3000),
                "ProgramArguments": ["/old/devboost/bin/DevBoost-tunnel", "-N", "host"],
                "StandardOutPath": "/old/logs/devboost-forward-server-3000.log",
                "StandardErrorPath": "/old/logs/devboost-forward-server-3000.err",
            }, f)

        self.assertEqual(devboost.restore_packaged_forward_agents(), 1)
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        self.assertEqual(data["ProgramArguments"][:2], [packaged, "--tunnel"])
        self.assertEqual(data["StandardOutPath"],
                         os.path.join(self.temp_dir.name, "devboost-forward-server-3000.log"))
        self.assertEqual(data["StandardErrorPath"],
                         os.path.join(self.temp_dir.name, "devboost-forward-server-3000.err"))

    @patch("os.kill")
    @patch("devboost.get_ssh_forwards")
    def test_clean_orphaned_tunnels(self, mock_forwards, mock_kill):
        mock_forwards.return_value = [
            {"pid": 1001, "local_port": 8080, "is_listening": True},
            {"pid": 1002, "local_port": 8080, "is_listening": False},
            {"pid": 1003, "local_port": 43127, "is_listening": False},
        ]
        killed = devboost.clean_orphaned_tunnels()
        self.assertEqual(killed, 2)
        mock_kill.assert_any_call(1002, 9)
        mock_kill.assert_any_call(1003, 9)


if __name__ == "__main__":
    unittest.main()
