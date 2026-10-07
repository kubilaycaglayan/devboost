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
    @patch.object(devboost, "is_server_reachable", return_value=False)
    @patch.object(devboost, "get_port_history", return_value=[])
    @patch.object(devboost, "get_launchagents_for_server", return_value={})
    @patch.object(devboost, "get_ssh_forwards", return_value=[])
    def test_status_does_not_leak_bootstrap_labels_or_forwards_to_new_server(
            self, _forwards, _agents, _history, _reachable):
        with tempfile.TemporaryDirectory() as root:
            old_file, old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
            devboost.CONFIG_DIR = root
            devboost.CONFIG_FILE = os.path.join(root, "config.json")
            try:
                cfg = devboost.load_config()
                first = cfg["servers"][0]
                second = devboost.add_server("other", "Other")
                cfg = devboost.load_config()
                cfg["labels"] = {"3000": "Legacy App"}
                cfg["server_labels"] = {}
                cfg["forward_configs"] = [{
                    "server_id": first["id"], "local_port": 3000,
                    "remote_port": 3001, "always": False,
                }]
                devboost.save_config(cfg)

                first_status = devboost.get_all_forwards_status(first["id"])
                second_status = devboost.get_all_forwards_status(second["id"])

                self.assertEqual([row["local_port"] for row in first_status["forwards"]], [3000])
                self.assertEqual(first_status["forwards"][0]["label"], "Legacy App")
                self.assertEqual(second_status["forwards"], [])
            finally:
                devboost.CONFIG_FILE, devboost.CONFIG_DIR = old_file, old_dir

    def test_extract_ssh_destination_skips_forward_values_and_options(self):
        cmd = "123 ssh -N -L 3000:127.0.0.1:3000 -o ServerAlive=yes my-box"
        self.assertEqual(devboost._extract_ssh_destination(cmd), "my-box")
        self.assertEqual(devboost._extract_ssh_destination("!"), "")

    def test_ssh_discovery_requires_exact_destination_match(self):
        with patch.object(devboost, "get_listening_ports", return_value={3000: {"pid": 42}}), \
                patch("devboost_app.forwarding.subprocess.run") as run:
            run.return_value = MagicMock(stdout="42 ssh -N -L 3000:127.0.0.1:3000 prod-box\n")
            self.assertEqual(devboost.get_ssh_forwards(server={"ssh_host": "box"}), [])

    def test_ssh_discovery_uses_ip_only_when_unique(self):
        with tempfile.TemporaryDirectory() as root:
            old_file, old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
            devboost.CONFIG_DIR = root
            devboost.CONFIG_FILE = os.path.join(root, "config.json")
            try:
                first = devboost.load_config()["servers"][0]
                second = devboost.add_server("other")
                cfg = devboost.load_config()
                cfg["servers"][0]["ip"] = "192.0.2.8"
                cfg["servers"][1]["ip"] = "192.0.2.8"
                devboost.save_config(cfg)
                first, second = cfg["servers"]
                with patch.object(devboost, "get_listening_ports", return_value={3000: {"pid": 42}}), \
                        patch("devboost_app.forwarding.subprocess.run") as run:
                    run.return_value = MagicMock(stdout="42 ssh -N -L 3000:127.0.0.1:3000 192.0.2.8\n")
                    self.assertEqual(devboost.get_ssh_forwards(server=first), [])
                    self.assertEqual(devboost.get_ssh_forwards(server=cfg["servers"][1]), [])
            finally:
                devboost.CONFIG_FILE, devboost.CONFIG_DIR = old_file, old_dir

    def test_toggle_always_cannot_create_missing_server_forward(self):
        server = {"id": "server-b", "ssh_host": "box-b"}
        with patch.object(devboost, "load_config", return_value={}), \
                patch.object(devboost, "get_server", return_value=server), \
                patch.object(devboost, "get_all_forwards_status", return_value={"forwards": []}), \
                patch.object(devboost, "add_forward") as add:
            with self.assertRaisesRegex(ValueError, "not configured"):
                devboost.toggle_always(3000, True, server_ref="server-b")
            add.assert_not_called()

    def test_unknown_explicit_server_does_not_enumerate_global_forwards(self):
        with patch.object(devboost, "load_config", return_value={"servers": []}), \
                patch("devboost_app.forwarding.subprocess.run") as run, \
                patch.object(devboost, "get_listening_ports", return_value={}):
            self.assertEqual(devboost.get_ssh_forwards(server_id="missing"), [])
            run.assert_not_called()

    def test_disconnected_always_forward_without_agent_has_valid_status(self):
        with tempfile.TemporaryDirectory() as root:
            old_file, old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
            devboost.CONFIG_DIR = root
            devboost.CONFIG_FILE = os.path.join(root, "config.json")
            try:
                server = devboost.load_config()["servers"][0]
                cfg = devboost.load_config()
                cfg["forward_configs"] = [{
                    "server_id": server["id"], "local_port": 3000,
                    "remote_port": 3001, "always": True,
                }]
                devboost.save_config(cfg)
                with patch.object(devboost, "is_server_connected", return_value=False), \
                        patch.object(devboost, "get_launchagents_for_server", return_value={}), \
                        patch.object(devboost, "get_ssh_forwards", return_value=[]), \
                        patch.object(devboost, "get_port_history", return_value=[]):
                    status = devboost.get_all_forwards_status(server["id"])
                self.assertEqual(status["forwards"][0]["local_port"], 3000)
                self.assertIsNone(status["forwards"][0]["plist_label"])
            finally:
                devboost.CONFIG_FILE, devboost.CONFIG_DIR = old_file, old_dir

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

    @patch.object(devboost, "create_launchagent")
    @patch.object(devboost, "remove_launchagent")
    @patch.object(devboost, "get_launchagents_for_server")
    def test_restore_always_forward_agents_reconciles_config(self, agents, remove, create):
        with tempfile.TemporaryDirectory() as root:
            old_file, old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
            devboost.CONFIG_DIR = root
            devboost.CONFIG_FILE = os.path.join(root, "config.json")
            try:
                cfg = devboost.load_config()
                server = cfg["servers"][0]
                cfg["forward_configs"] = [{
                    "server_id": server["id"], "local_port": 3030,
                    "remote_port": 3031, "always": True,
                }]
                devboost.save_config(cfg)
                agents.return_value = {
                    3030: {"port": 3030},
                    8080: {"port": 8080},
                }

                result = devboost.restore_always_forward_agents()

                self.assertEqual(result, {"repaired": 1, "removed": 1})
                remove.assert_called_once_with(8080, server_ref=server["id"])
                create.assert_called_once_with(3030, 3031, server_ref=server["id"])
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
