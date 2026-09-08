import json
import os
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import devboost


class TestUsageMonitoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_file, self.old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
        devboost.CONFIG_DIR = self.tmp.name
        devboost.CONFIG_FILE = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        devboost.CONFIG_FILE, devboost.CONFIG_DIR = self.old_file, self.old_dir
        self.tmp.cleanup()

    def test_multiple_accounts_and_secret_metadata_only(self):
        one = devboost.save_usage_account({"provider": "codex", "name": "work", "usage_command": ["python3", "-c", "import json; print(json.dumps({'quotas':[{'used':2,'limit':10}]}))"]})
        two = devboost.save_usage_account({"provider": "claude", "name": "personal", "balance_url": "https://example.test/balance", "token_env": "CLAUDE_KEY"})
        self.assertNotEqual(one["id"], two["id"])
        accounts = devboost.get_usage_accounts()
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["usage_command"][0], "python3")
        self.assertEqual(accounts[1]["token_env"], "CLAUDE_KEY")

    def test_existing_account_can_be_updated_without_changing_id(self):
        account = devboost.save_usage_account({
            "provider": "custom", "name": "old", "balance_url": "https://example.test/old",
        })
        devboost.save_usage_account({
            "id": account["id"], "provider": "custom", "name": "new",
            "balance_url": "https://example.test/new", "token_env": "PROVIDER_KEY",
        })
        accounts = devboost.get_usage_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], account["id"])
        self.assertEqual(accounts[0]["name"], "new")
        self.assertEqual(accounts[0]["balance_url"], "https://example.test/new")
        self.assertEqual(accounts[0]["token_env"], "PROVIDER_KEY")

    def test_usage_accounts_can_be_reordered_and_edit_keeps_position(self):
        first = devboost.save_usage_account({"provider": "custom", "name": "first", "balance_url": "https://example.test/first"})
        second = devboost.save_usage_account({"provider": "custom", "name": "second", "balance_url": "https://example.test/second"})
        third = devboost.save_usage_account({"provider": "custom", "name": "third", "balance_url": "https://example.test/third"})
        self.assertEqual([a["id"] for a in devboost.reorder_usage_accounts([third["id"], first["id"], second["id"]])],
                         [third["id"], first["id"], second["id"]])
        devboost.save_usage_account({"id": first["id"], "provider": "custom", "name": "updated",
                                     "balance_url": "https://example.test/updated"})
        self.assertEqual([a["id"] for a in devboost.get_usage_accounts()],
                         [third["id"], first["id"], second["id"]])

    def test_edit_with_unknown_id_cannot_create_an_account(self):
        original = devboost.save_usage_account({
            "provider": "custom", "name": "original", "balance_url": "https://example.test/original",
        })
        with self.assertRaisesRegex(ValueError, "usage account not found"):
            devboost.save_usage_account({
                "id": "stale-id", "provider": "custom", "name": "accidental-new-row",
                "balance_url": "https://example.test/new",
            })
        accounts = devboost.get_usage_accounts()
        self.assertEqual([account["id"] for account in accounts], [original["id"]])

    def test_command_snapshot_is_normalized_and_persisted(self):
        account = devboost.save_usage_account({"provider": "opencode", "name": "local", "usage_command": ["python3", "-c", "import json; print(json.dumps({'usage': {'daily': {'used': 3, 'limit': 7}}, 'balance': 4.5}))"]})
        status = devboost.get_usage_status(refresh=True)
        snapshot = status["snapshots"][account["id"]]
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["quotas"][0]["remaining"], 4)
        self.assertEqual(snapshot["balances"][0]["remaining"], 4.5)
        self.assertEqual(snapshot["last_valid_query_at"], snapshot["updated_at"])

    def test_failed_refresh_keeps_last_valid_query_time(self):
        account = devboost.save_usage_account({
            "provider": "custom", "name": "test",
            "usage_command": ["python3", "-c", "print('{}')"],
        })
        first = devboost.get_usage_status(refresh=True)["snapshots"][account["id"]]
        with patch.object(devboost, "_read_usage_command", side_effect=ValueError("temporary failure")):
            second = devboost.get_usage_status(refresh=True)["snapshots"][account["id"]]
        self.assertFalse(second["ok"])
        self.assertEqual(second["last_valid_query_at"], first["last_valid_query_at"])

    def test_usage_status_filters_disabled_and_account_id(self):
        enabled = devboost.save_usage_account({"provider": "custom", "name": "enabled",
                                                "usage_command": ["python3", "-c", "print('{}')"]})
        disabled = devboost.save_usage_account({"provider": "custom", "name": "disabled",
                                                 "enabled": False, "usage_command": ["python3", "-c", "print('{}')"]})
        status = devboost.get_usage_status(account_id=disabled["id"])
        self.assertEqual(status["accounts"], [])
        status = devboost.get_usage_status(account_id=enabled["id"])
        self.assertEqual([a["id"] for a in status["accounts"]], [enabled["id"]])

    @patch.object(devboost, "_read_codex_upstream", side_effect=ValueError("offline"))
    @patch.object(devboost, "_read_codex_api", return_value={"quotas": [{"remaining": 7, "unit": "%"}]})
    def test_usage_status_refreshes_against_selected_server(self, remote, _upstream):
        devboost.save_usage_account({"provider": "codex", "name": "remote"})
        cfg = devboost.load_config()
        server = devboost.add_server("box", name="Remote box")
        status = devboost.get_usage_status(refresh=True, server_ref=server["id"])
        self.assertEqual(status["server_id"], server["id"])
        self.assertEqual(status["server_host"], "box")
        self.assertEqual(status["snapshots"]["codex-remote"]["quotas"][0]["remaining"], 7)
        self.assertEqual(remote.call_args.args[0]["id"], "codex-remote")
        self.assertEqual(remote.call_args.kwargs["server"]["ssh_host"], "box")

    def test_remove_usage_account_removes_snapshot(self):
        account = devboost.save_usage_account({"provider": "custom", "name": "remove",
                                                "usage_command": ["python3", "-c", "print('{}')"]})
        devboost.get_usage_status(refresh=True)
        self.assertTrue(devboost.remove_usage_account(account["id"]))
        self.assertFalse(devboost.remove_usage_account(account["id"]))
        cfg = devboost.load_config()
        self.assertNotIn(account["id"], cfg["usage_snapshots"])

    def test_http_requires_safe_url(self):
        with self.assertRaises(ValueError):
            devboost._read_usage_http({"balance_url": "http://remote.example/balance"})

    @patch.object(devboost.urllib.request, "urlopen")
    def test_codex_upstream_maps_windows_and_auth_headers(self, urlopen):
        auth_path = os.path.join(self.tmp.name, "auth.json")
        with open(auth_path, "w", encoding="utf-8") as stream:
            json.dump({"tokens": {"access_token": "access-secret", "account_id": "account-1"}}, stream)

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({
                    "plan_type": "plus",
                    "rate_limit": {"primary_window": {"used_percent": 63, "limit_window_seconds": 18000, "reset_at": 1788904848},
                                    "secondary_window": {"used_percent": 10, "limit_window_seconds": 604800, "reset_at": 1789491648}},
                    "credits": {"balance": "0", "unlimited": False},
                    "rate_limit_reset_credits": {"available_count": 1},
                }).encode()
        urlopen.return_value = Response()
        result = devboost._read_codex_upstream({"codex_home": self.tmp.name})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(request.get_header("Authorization"), "Bearer access-secret")
        self.assertEqual(request.get_header("Chatgpt-account-id"), "account-1")
        self.assertEqual(result["source"], "[HTTPS] OpenAI upstream")
        self.assertEqual(result["quotas"][0]["remaining"], 37)
        self.assertEqual(result["quotas"][1]["window_minutes"], 10080)
        self.assertEqual(result["available_resets"], 1)

    def test_codex_upstream_failure_falls_back_to_local_records_with_label(self):
        records = os.path.join(self.tmp.name, "sessions")
        os.makedirs(records)
        with open(os.path.join(records, "rollout.jsonl"), "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"payload": {"thread_token_usage": {"input_tokens": 10, "output_tokens": 4}}}) + "\n")
        account = {"provider": "codex", "name": "local", "codex_home": self.tmp.name}
        with patch.object(devboost, "_read_codex_upstream", side_effect=ValueError("offline")):
            snapshot = devboost.refresh_usage_account(account)
        self.assertTrue(snapshot["ok"])
        self.assertTrue(snapshot["stale"])
        self.assertTrue(snapshot["source"].startswith("[Local]"))
        self.assertIn("Showing local usage", snapshot["message"])

    def test_opencode_stats_text_is_normalized(self):
        result = devboost._parse_opencode_stats("Total Cost                                        $1.25\nInput                                              3.9M\nOutput                                           217.1K\n")
        self.assertEqual(result["balances"][0]["spent"], 1.25)
        self.assertAlmostEqual(result["quotas"][0]["used"], 4117100)

    @patch.dict(os.environ, {"ANTHROPIC_KEY": "secret"}, clear=False)
    @patch.object(devboost.urllib.request, "urlopen")
    def test_claude_api_uses_anthropic_auth_header(self, urlopen):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"balance": 2}'
        urlopen.return_value = Response()
        devboost._read_usage_http({"provider": "claude", "balance_url": "https://api.example.test/usage", "token_env": "ANTHROPIC_KEY"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "secret")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")

    @patch.object(devboost.urllib.request, "urlopen")
    def test_http_rate_limit_headers_become_remaining_quota(self, urlopen):
        class Headers(dict):
            def items(self): return super().items()
        class Response:
            headers = Headers({"x-ratelimit-remaining-requests": "7", "x-ratelimit-limit-requests": "10"})
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"ok": true}'
        urlopen.return_value = Response()
        result = devboost._normalize_usage_payload(devboost._read_usage_http({"balance_url": "https://api.example.test/status"}))
        self.assertEqual(result["quotas"][0]["remaining"], 7)
        self.assertEqual(result["quotas"][0]["limit"], 10)

    def test_admin_report_buckets_are_normalized(self):
        result = devboost._normalize_usage_payload({"data": [{"results": [{"input_tokens": 10, "output_tokens": 5, "input_cached_tokens": 2}]}]})
        self.assertEqual(result["quotas"][0]["used"], 17)
        self.assertEqual(result["source"], "provider admin usage API")

    def test_agy_quota_json_is_normalized(self):
        result = devboost._normalize_usage_payload({
            "models": [{"model_id": "gemini", "remaining_fraction": 0.72, "reset_time": "tomorrow"}],
            "providers": [{"provider": "google", "pool_remaining_percent": 61}],
        })
        self.assertEqual(result["source"], "agy-quota")
        self.assertEqual(result["quotas"][0]["remaining"], 72)
        self.assertEqual(result["quotas"][1]["remaining"], 61)

    def test_agy_native_usage_envelope_is_normalized(self):
        result = devboost._normalize_usage_payload({"command": {"name": "usage", "data": {"groups": [{
            "name": "Gemini Models", "buckets": [{"name": "Weekly Limit Remaining", "window": "weekly",
            "remaining_fraction": .72, "reset_time": "2030-01-01T00:00:00Z"}]}]}}})
        self.assertEqual(result["source"], "Agy live usage")
        self.assertEqual(result["quotas"][0]["remaining"], 72)
        self.assertEqual(result["quotas"][0]["reset_at"], "2030-01-01T00:00:00Z")

    def test_agy_failure_keeps_last_live_values_as_stale(self):
        account = devboost.save_usage_account({"provider": "agy", "name": "work"})
        live = {"source": "Agy live usage", "quotas": [{"remaining": 72, "unit": "%"}], "balances": []}
        with patch.object(devboost, "_read_agy_usage", return_value=live):
            first = devboost.get_usage_status()["snapshots"][account["id"]]
        with patch.object(devboost, "_read_agy_usage", side_effect=ValueError("offline")):
            second = devboost.get_usage_status(refresh=True)["snapshots"][account["id"]]
        self.assertTrue(second["stale"])
        self.assertEqual(second["quotas"], first["quotas"])
        self.assertEqual(second["last_valid_query_at"], first["last_valid_query_at"])

    def test_balance_is_derived_from_granted_and_used(self):
        result = devboost._normalize_usage_payload({"total_granted": 10, "total_used": 3})
        self.assertEqual(result["balances"][0]["remaining"], 7)
        self.assertEqual(result["balances"][0]["spent"], 3)

    @patch.object(devboost.urllib.request, "urlopen")
    @patch.dict(os.environ, {"OPENAI_ADMIN_KEY": "secret"}, clear=False)
    def test_codex_provider_api_builds_time_window(self, urlopen):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"data": []}'
        urlopen.return_value = Response()
        devboost._read_provider_api({"provider": "codex", "token_env": "OPENAI_ADMIN_KEY", "api_days": 2})
        request = urlopen.call_args.args[0]
        self.assertIn("start_time=", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    @patch.object(devboost, "_read_codex_upstream", side_effect=ValueError("offline"))
    @patch.object(devboost.shutil, "which", return_value=None)
    def test_provider_without_machine_adapter_is_explicit(self, _which, _upstream):
        snapshot = devboost.refresh_usage_account({"provider": "codex", "name": "work", "local_path": os.path.join(self.tmp.name, "missing")})
        self.assertFalse(snapshot["ok"])
        self.assertIn("no codex usage records", snapshot["message"])

    @patch.object(devboost.usage_helpers.shutil, "which", return_value="/usr/local/bin/opencode")
    def test_provider_default_command_detects_installed_cli(self, _which):
        self.assertEqual(devboost._provider_default_command("opencode"), ["opencode", "stats"])

    @patch.object(devboost.usage_helpers.shutil, "which", return_value=None)
    def test_usage_refresh_without_adapter_has_actionable_error(self, _which):
        snapshot = devboost.refresh_usage_account({"provider": "opencode", "name": "local"})
        self.assertFalse(snapshot["ok"])
        self.assertIn("no safe machine-readable usage adapter", snapshot["message"])
        self.assertNotIn("shutil", snapshot["message"])

    @patch.object(devboost, "_read_codex_upstream", side_effect=ValueError("offline"))
    def test_codex_local_rate_limits_are_native_quota_data(self, _upstream):
        records = os.path.join(self.tmp.name, "codex")
        os.makedirs(records)
        with open(os.path.join(records, "rollout.jsonl"), "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"payload": {"thread_token_usage": {"input_tokens": 10, "output_tokens": 4}, "rate_limits": {"primary": {"used_percent": 25, "resets_at": 4102444800, "window_minutes": 300}, "secondary": {"used_percent": 40, "resets_at": 4102444800, "window_minutes": 10080}, "credits": {"balance": "3"}, "plan_type": "plus"}}}) + "\n")
        snapshot = devboost.refresh_usage_account({"provider": "codex", "name": "local", "local_path": records})
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["quotas"][0]["remaining"], 75)
        self.assertEqual(snapshot["balances"][0]["remaining"], 3)

    @patch.object(devboost, "_read_codex_upstream", side_effect=ValueError("offline"))
    @patch.object(devboost.usage_service.time, "time", return_value=2000)
    def test_codex_expired_record_does_not_invent_a_reset(self, _time, _upstream):
        records = os.path.join(self.tmp.name, "codex-expired")
        os.makedirs(records)
        with open(os.path.join(records, "rollout.jsonl"), "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"payload": {"rate_limits": {
                "primary": {"used_percent": 91, "resets_at": 1000, "window_minutes": 300},
                "secondary": {"used_percent": 40, "resets_at": 3000, "window_minutes": 10080},
            }}}) + "\n")
        snapshot = devboost.refresh_usage_account({"provider": "codex", "name": "local", "local_path": records})
        self.assertIsNone(snapshot["quotas"][0]["used"])
        self.assertIsNone(snapshot["quotas"][0]["remaining"])
        self.assertIn("expired", snapshot["quotas"][0]["name"])
        self.assertTrue(snapshot["stale"])
        self.assertNotIn("last_valid_query_at", snapshot)
        self.assertEqual(snapshot["quotas"][1]["used"], 40)


if __name__ == "__main__":
    unittest.main()
