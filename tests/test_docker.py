import subprocess
import unittest
from unittest.mock import patch

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from docker_monitor import collect_docker_snapshot, parse_docker_output


class TestDockerMonitor(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
