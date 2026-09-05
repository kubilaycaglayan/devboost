import os
import plistlib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost
from devboost_app import forwarding


class TestForwardingDiscovery(unittest.TestCase):
    def test_extract_ssh_destination_skips_forward_values_and_options(self):
        cmd = "123 ssh -N -L 3000:127.0.0.1:3000 -o ServerAlive=yes my-box"
        self.assertEqual(devboost._extract_ssh_destination(cmd), "my-box")
        self.assertEqual(devboost._extract_ssh_destination("!"), "")

    @patch("devboost_app.forwarding.subprocess.run")
    def test_get_listening_ports_parses_lsof_and_ignores_bad_rows(self, run):
        run.return_value = MagicMock(stdout=(
            "python 12 user 9u IPv4 0x 0t0 TCP 127.0.0.1:3000 (LISTEN)\n"
            "bad row\n"))
        result = forwarding.get_listening_ports()
        self.assertEqual(result[3000], {"cmd": "python", "pid": 12, "node": "127.0.0.1:3000"})

    @patch.object(devboost, "get_listening_ports", return_value={3000: {"pid": 42}})
    @patch("devboost_app.forwarding.subprocess.run")
    def test_get_ssh_forwards_parses_and_marks_listening(self, run, _ports):
        run.return_value = MagicMock(stdout="42 ssh -N -L 3000:127.0.0.1:3001 box\n")
        result = devboost.get_ssh_forwards(server={"ssh_host": "box"})
        self.assertEqual(result[0]["remote_port"], 3001)
        self.assertTrue(result[0]["is_listening"])
        self.assertEqual(result[0]["ssh_dest"], "box")

    def test_namespaced_launchagent_is_attributed_to_server(self):
        with tempfile.TemporaryDirectory() as root:
            old_dir = devboost.LAUNCH_AGENTS_DIR
            old_file, old_cfg_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
            devboost.LAUNCH_AGENTS_DIR = root
            devboost.CONFIG_DIR = root
            devboost.CONFIG_FILE = os.path.join(root, "config.json")
            try:
                first = devboost.load_config()["servers"][0]
                second = devboost.add_server("box")
                path = devboost.get_plist_path(3000, second["id"])
                with open(path, "wb") as stream:
                    plistlib.dump({"Label": devboost.get_plist_label(3000, second["id"]),
                                   "ProgramArguments": ["ssh", "-L", "3000:127.0.0.1:3001", "box"]}, stream)
                agents = devboost.get_launchagents_for_server(second)
                self.assertEqual(agents[3000]["remote_port"], 3001)
                self.assertEqual(agents[3000]["server_id"], second["id"])
                self.assertNotIn(3000, devboost.get_launchagents_for_server(first))
            finally:
                devboost.LAUNCH_AGENTS_DIR = old_dir
                devboost.CONFIG_FILE, devboost.CONFIG_DIR = old_file, old_cfg_dir

    @patch.object(devboost, "get_ssh_forwards", return_value=[])
    @patch.object(devboost, "remove_launchagent")
    @patch.object(devboost, "kill_port_processes")
    def test_remove_forward_removes_agent_process_and_label(self, kill, remove, _forwards):
        with tempfile.TemporaryDirectory() as root:
            old_file, old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
            devboost.CONFIG_DIR = root
            devboost.CONFIG_FILE = os.path.join(root, "config.json")
            try:
                sid = devboost.load_config()["servers"][0]["id"]
                cfg = devboost.load_config()
                cfg["server_labels"][sid] = {"3000": "App"}
                devboost.save_config(cfg)
                devboost.remove_forward(3000, server_ref=sid)
                kill.assert_called_once_with(3000, server_ref=sid)
                remove.assert_called_once_with(3000, server_ref=sid)
                self.assertNotIn("3000", devboost.load_config()["server_labels"].get(sid, {}))
            finally:
                devboost.CONFIG_FILE, devboost.CONFIG_DIR = old_file, old_dir

    @patch.object(devboost, "is_server_reachable", return_value=False)
    @patch.object(devboost, "get_port_history", return_value=[])
    @patch.object(devboost, "get_launchagents_for_server", return_value={})
    @patch.object(devboost, "get_ssh_forwards")
    def test_status_reports_cross_server_local_port_conflict(self, forwards, _agents, _history, _reachable):
        with tempfile.TemporaryDirectory() as root:
            old_file, old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
            devboost.CONFIG_DIR = root
            devboost.CONFIG_FILE = os.path.join(root, "config.json")
            try:
                first = devboost.load_config()["servers"][0]
                second = devboost.add_server("other", "Other")
                forwards.side_effect = [[{
                    "local_port": 3000, "remote_port": 3000, "pid": 8,
                    "is_listening": False, "ssh_dest": "box", "cmd": "ssh box",
                }], [{
                    "local_port": 3000, "remote_port": 3000, "pid": 9,
                    "is_listening": True, "ssh_dest": "other", "cmd": "ssh other",
                }]]
                result = devboost.get_all_forwards_status(first["id"])
                row = next(item for item in result["forwards"] if item["local_port"] == 3000)
                self.assertTrue(row["conflict"])
                self.assertEqual(row["conflict_with"]["name"], "Other")
            finally:
                devboost.CONFIG_FILE, devboost.CONFIG_DIR = old_file, old_dir


if __name__ == "__main__":
    unittest.main()
