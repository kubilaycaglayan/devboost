import subprocess
import unittest
from unittest.mock import patch

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from docker_monitor import collect_docker_snapshot, parse_docker_output, collect_docker_logs
import devboost


class TestDockerMonitor(unittest.TestCase):
    def test_apply_docker_labels_matches_case_insensitively(self):
        containers = [{"name": "api-dev-production"}]
        labels = [
            {"id": "dev", "name": "Dev", "match": "dev", "color": "#58a6ff", "enabled": True},
            {"id": "prod", "name": "Prod", "match": "production", "color": "#f85149", "enabled": True},
        ]
        self.assertEqual(
            [label["name"] for label in devboost.apply_docker_labels(containers, labels)[0]["labels"]],
            ["Dev", "Prod"],
        )

    @patch("docker_monitor.run_ssh_command")
    def test_collect_docker_logs_uses_container_name_and_timestamps(self, mock_ssh):
        mock_ssh.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="2026-01-01T00:00:00Z hello\n", stderr="")
        result = collect_docker_logs("myhost", "api-dev", tail=50)
        self.assertTrue(result["ok"])
        self.assertIn("hello", result["logs"])
        self.assertIn("docker logs --timestamps --tail 50 api-dev", mock_ssh.call_args.args[1])

    def test_parse_docker_output(self):
        stdout = (
            '{"ID":"abc123","Names":"web","Image":"nginx:latest","Status":"Up 2 minutes"}\n'
            '__DEVBOOST_DOCKER_STATS__\n'
            '{"Container":"abc123","Name":"web","CPUPerc":"1.20%","MemUsage":"10MiB / 1GiB","MemPerc":"1.00%","NetIO":"1kB / 2kB"}\n'
        )
        containers, stats = parse_docker_output(stdout)
        self.assertEqual(containers[0]["Names"], "web")
        self.assertEqual(stats[0]["CPUPerc"], "1.20%")

    @patch("docker_monitor.run_ssh_command")
    def test_collect_merges_container_and_stats(self, mock_ssh):
        mock_ssh.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '{"ID":"abc123","Names":"web","Image":"nginx:latest","Status":"Up 2 minutes"}\n'
                '__DEVBOOST_DOCKER_STATS__\n'
                '{"Container":"abc123","Name":"web","CPUPerc":"1.20%","MemUsage":"10MiB / 1GiB","MemPerc":"1.00%","NetIO":"1kB / 2kB","BlockIO":"0B / 0B","PIDs":"4"}\n'
            ),
            stderr="",
        )
        result = collect_docker_snapshot("myhost")
        self.assertTrue(result["ok"])
        self.assertTrue(result["available"])
        self.assertEqual(result["containers"][0]["name"], "web")
        self.assertEqual(result["containers"][0]["stats"]["cpu_percent"], "1.20%")
        mock_ssh.assert_called_once()

    @patch("docker_monitor.run_ssh_command")
    def test_collect_reports_missing_docker(self, mock_ssh):
        mock_ssh.return_value = subprocess.CompletedProcess(
            args=[], returncode=127, stdout="", stderr="Docker CLI is not installed on the server\n"
        )
        result = collect_docker_snapshot("myhost")
        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertIn("not installed", result["message"])

    @patch("devboost.subprocess.run")
    @patch("devboost.load_config")
    def test_lazydocker_uses_interactive_ssh(self, mock_load_config, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        mock_load_config.return_value = {
            "servers": [{"id": "myhost", "ssh_host": "myhost", "name": "My host", "order": 0}],
            "labels": {}, "server_labels": {}, "syncs": [], "history": [],
        }
        devboost.cli_lazydocker("myhost")
        args = mock_run.call_args.args[0]
        self.assertEqual(args[-2:], ["myhost", "lazydocker"])
        self.assertIn("-t", args)


if __name__ == "__main__":
    unittest.main()
