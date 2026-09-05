import json
import os
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost
from devboost_app import usage_service


class TestUsageAdapters(unittest.TestCase):
    def setUp(self):
        self.runtime = types.SimpleNamespace(USAGE_TIMEOUT=4)

    @patch("devboost_app.usage_service.subprocess.run")
    def test_command_reads_json_and_maps_environment(self, run):
        run.return_value = types.SimpleNamespace(returncode=0, stdout='{"balance": 2}', stderr="")
        with patch.dict(os.environ, {"SOURCE_KEY": "secret"}, clear=False):
            result = usage_service.read_usage_command(self.runtime, {
                "usage_command": "tool --json", "env": {"TARGET_KEY": "SOURCE_KEY"},
            })
        self.assertEqual(result["balance"], 2)
        self.assertEqual(run.call_args.kwargs["env"]["TARGET_KEY"], "secret")
        self.assertEqual(run.call_args.kwargs["timeout"], 4)

    @patch("devboost_app.usage_service.run_ssh_command")
    def test_command_runs_on_selected_server(self, ssh):
        ssh.return_value = types.SimpleNamespace(returncode=0, stdout='{"balance": 2}', stderr="")
        result = usage_service.read_usage_command(self.runtime, {
            "provider": "custom", "usage_command": ["tool", "--json"]
        }, server={"ssh_host": "box"})
        self.assertEqual(result["balance"], 2)
        self.assertEqual(ssh.call_args.args[0], "box")
        self.assertIn("tool --json", ssh.call_args.args[1])

    @patch("devboost_app.usage_service.run_ssh_command")
    def test_remote_transcript_reads_records_from_selected_server(self, ssh):
        ssh.return_value = types.SimpleNamespace(
            returncode=0,
            stdout=("DEVBOOST_USAGE_FILE:/home/me/rollout.jsonl\n"
                    + json.dumps({"payload": {"thread_token_usage": {
                        "input_tokens": 10, "output_tokens": 4,
                        "cached_input_tokens": 2, "cache_write_input_tokens": 1},
                        "rate_limits": {"primary": {"used_percent": 20},
                                         "credits": {"balance": "3"}}}}) + "\n"),
            stderr="",
        )
        result = usage_service.read_remote_transcript_usage(self.runtime, {
            "provider": "codex", "local_path": "~/sessions"
        }, "box")
        self.assertEqual(result["quotas"][0]["remaining"], 80)
        self.assertEqual(result["balances"][0]["remaining"], 3)
        self.assertEqual(ssh.call_args.args[0], "box")
        self.assertIn('"$HOME"/sessions', ssh.call_args.args[1])

    @patch("devboost_app.usage_service.subprocess.run")
    def test_command_rejects_bad_exit_invalid_length_and_non_json(self, run):
        run.return_value = types.SimpleNamespace(returncode=2, stdout="", stderr="bad command")
        with self.assertRaisesRegex(RuntimeError, "bad command"):
            usage_service.read_usage_command(self.runtime, {"usage_command": ["tool"]})
        with self.assertRaisesRegex(ValueError, "invalid"):
            usage_service.read_usage_command(self.runtime, {"usage_command": ["x"] * 33})
        run.return_value = types.SimpleNamespace(returncode=0, stdout="plain text", stderr="")
        with self.assertRaisesRegex(ValueError, "must print JSON"):
            usage_service.read_usage_command(self.runtime, {"usage_command": ["tool"]})

    @patch("devboost_app.usage_service.parse_opencode_stats", return_value={"quotas": []})
    @patch("devboost_app.usage_service.subprocess.run")
    def test_opencode_command_accepts_table_output(self, run, parse):
        run.return_value = types.SimpleNamespace(returncode=0, stdout="table", stderr="")
        self.assertEqual(usage_service.read_usage_command(self.runtime, {
            "provider": "opencode", "usage_command": ["opencode", "stats"]
        }), {"quotas": []})
        parse.assert_called_once_with("table")

    def test_local_transcript_sums_supported_usage_and_ignores_bad_lines(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "rollout.jsonl")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("not json\n")
                stream.write(json.dumps({"payload": {"thread_token_usage": {
                    "input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 2,
                    "cache_write_input_tokens": 1}, "rate_limits": {
                        "primary": {"used_percent": 20, "resets_at": 123},
                        "credits": {"balance": "3"}, "plan_type": "plus"}}}) + "\n")
            result = usage_service.read_local_transcript_usage(devboost, {
                "provider": "codex", "local_path": root,
            })
        self.assertEqual(result["quotas"][0]["remaining"], 80)
        self.assertEqual(result["balances"][0]["remaining"], 3)
        self.assertEqual(result["plan_type"], "plus")

    @patch("devboost_app.usage_service.urllib.request.urlopen")
    def test_http_auth_headers_and_localhost_policy(self, urlopen):
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"balance": 1}'
        urlopen.return_value = Response()
        with patch.dict(os.environ, {"KEY": "secret"}, clear=False):
            usage_service.read_usage_http(self.runtime, {
                "provider": "custom", "balance_url": "http://localhost:9999/usage",
                "token_env": "KEY", "auth_header": "X-Token",
            })
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-token"), "Bearer secret")
        with self.assertRaises(ValueError):
            usage_service.read_usage_http(self.runtime, {"balance_url": "http://remote/usage"})

    @patch("devboost_app.usage_service.read_usage_http", return_value={"data": []})
    def test_provider_api_rejects_unknown_provider_and_clamps_days(self, read_http):
        with self.assertRaises(ValueError):
            usage_service.read_provider_api(self.runtime, {"provider": "custom"})
        with patch("devboost_app.usage_service.time.time", return_value=100000):
            usage_service.read_provider_api(self.runtime, {"provider": "codex", "api_days": 999})
        request_account = read_http.call_args.args[1]
        self.assertIn("balance_url", request_account)
        self.assertIn("start_time=", request_account["balance_url"])


if __name__ == "__main__":
    unittest.main()
