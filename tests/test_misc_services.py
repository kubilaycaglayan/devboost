import contextlib
import io
import os
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost
from devboost_app import docker_service, history, local_ports


class TestHistoryAndLocalPorts(unittest.TestCase):
    def test_history_deduplicates_and_filters_per_server(self):
        cfg = {"servers": [{"id": "one"}], "history": []}
        runtime = types.SimpleNamespace(
            load_config=lambda: cfg,
            save_config=lambda value: None,
            resolve_server=lambda value, ref: {"id": ref or "one"},
            get_legacy_server=lambda value: {"id": "one"},
            DEFAULT_LABELS={3000: "App"},
        )
        history.record_port_history(runtime, 3000, 3001, server_id="one")
        history.record_port_history(runtime, 3000, 3002, label="New", server_id="one")
        history.record_port_history(runtime, 4000, 4000, server_id="two")
        self.assertEqual(len(history.get_port_history(runtime, server_id="one")), 1)
        self.assertEqual(history.get_port_history(runtime, server_id="one")[0]["remote_port"], 3002)
        self.assertEqual(history.get_port_history(runtime, exclude_ports=[3000], server_id="one"), [])

    def test_local_port_rows_mark_devboost_tunnels(self):
        runtime = types.SimpleNamespace(
            get_listening_ports=lambda: {3000: {"pid": 9, "cmd": "ssh", "node": "x"}, 4000: "bad"},
            get_ssh_forwards=lambda: [{"pid": 9, "is_listening": True}],
        )
        self.assertEqual(local_ports.get_listening_ports(runtime), [{
            "port": 3000, "pid": 9, "cmd": "ssh", "node": "x", "devboost": True,
        }])

    @patch("devboost_app.local_ports.time.sleep")
    @patch("devboost_app.local_ports._pid_is_dead", return_value=True)
    @patch("devboost_app.local_ports.os.kill")
    def test_local_port_kill_escalates_when_term_does_not_finish(self, kill, _dead, _sleep):
        runtime = types.SimpleNamespace(get_listening_ports=lambda: {9: {"pid": 9}})
        result = local_ports.kill_listening_process(runtime, 9)
        self.assertTrue(result["ok"])
        kill.assert_called_once_with(9, local_ports.signal.SIGTERM)

    @patch("devboost_app.local_ports.time.sleep")
    @patch("devboost_app.local_ports._pid_is_dead", side_effect=[False, True])
    @patch("devboost_app.local_ports.os.kill")
    def test_local_port_kill_uses_sigkill_if_needed(self, kill, _dead, _sleep):
        runtime = types.SimpleNamespace(get_listening_ports=lambda: {9: {"pid": 9}})
        result = local_ports.kill_listening_process(runtime, 9)
        self.assertTrue(result["ok"])
        self.assertEqual(kill.call_args_list[1].args, (9, local_ports.signal.SIGKILL))


class TestDockerService(unittest.TestCase):
    def _runtime(self, snapshot=None):
        return types.SimpleNamespace(
            load_config=lambda: {"servers": [{"id": "one", "ssh_host": "box", "name": "Box"}],
                                 "docker_labels": [{"name": "Web", "match": "web", "enabled": True}]},
            resolve_server=lambda cfg, ref: cfg["servers"][0],
            DOCKER_MONITOR=types.SimpleNamespace(get=lambda *args: snapshot or {"containers": []}),
            DEFAULT_LABELS={8080: "Web"},
        )

    def test_status_attaches_server_metadata_and_labels(self):
        runtime = self._runtime({"containers": [{"name": "WEB-api"}], "ok": True})
        result = docker_service.get_status(runtime)
        self.assertEqual(result["server_id"], "one")
        self.assertEqual(result["containers"][0]["labels"][0]["name"], "Web")

    @patch("devboost_app.docker_service.subprocess.run")
    def test_scan_services_filters_system_ports_and_extracts_process(self, run):
        run.return_value = MagicMock(returncode=0, stdout=(
            "LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:((\"sshd\",pid=1))\n"
            "LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:((\"python\",pid=2))\n"))
        result = docker_service.scan_services(self._runtime(), "one")
        self.assertEqual(result[0]["port"], 8080)
        self.assertEqual(result[0]["process"], "python")


class TestCLI(unittest.TestCase):
    def test_sync_age_formatting_covers_units_and_invalid_values(self):
        now = 100000
        with patch("devboost_app.cli.time.time", return_value=now), \
                patch("devboost_app.cli.time.strftime", return_value="date"):
            self.assertEqual(devboost._format_sync_age(now - 2), "2s ago")
            self.assertEqual(devboost._format_sync_age(now - 120), "2m ago")
            self.assertEqual(devboost._format_sync_age(now - 7200), "2h ago")
            self.assertEqual(devboost._format_sync_age(now - 90000), "date")
            self.assertEqual(devboost._format_sync_age("bad"), "never")
            self.assertEqual(devboost._format_sync_age(None), "never")

    @patch.object(devboost, "get_usage_status", return_value={"accounts": [], "snapshots": {}})
    def test_cli_usage_reports_empty_configuration(self, _status):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            devboost.cli_usage()
        self.assertIn("No AI usage accounts configured", output.getvalue())

    @patch.object(devboost, "get_usage_status", return_value={
        "accounts": [{"id": "one", "name": "Work", "provider": "codex"}],
        "snapshots": {"one": {"ok": True, "quotas": [{"name": "daily", "remaining": 5, "unit": "%"}],
                              "balances": [{"remaining": 2, "currency": "USD"}]}}
    })
    def test_cli_usage_prints_quota_and_balance(self, _status):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            devboost.cli_usage()
        self.assertIn("Work (codex)", output.getvalue())
        self.assertIn("daily: 5 % remaining", output.getvalue())
        self.assertIn("balance: 2 USD", output.getvalue())


if __name__ == "__main__":
    unittest.main()
