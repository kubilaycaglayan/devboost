import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import devboost
from devboost_app import codex_usage, usage_service


def api_payload():
    return {"accountId": "must-not-be-exposed", "rateLimits": {"primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {
                "codex": {"planType": "plus", "primary": {
                    "usedPercent": 25, "windowDurationMins": 300, "resetsAt": 4102444800},
                    "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 4102444900},
                    "credits": {"balance": "3", "unlimited": False}},
                "other": {"limitName": "Other model", "primary": {
                    "usedPercent": 0, "windowDurationMins": 60, "resetsAt": 4102444800}}},
            "rateLimitResetCredits": {"availableCount": 2, "credits": []}}


class TestCodexNormalization(unittest.TestCase):
    def test_multiple_buckets_plan_credits_and_actual_earned_resets(self):
        result = usage_service.normalize_codex_api(api_payload())
        self.assertEqual([q["remaining"] for q in result["quotas"]], [75, 60, 100])
        self.assertIn("5-hour", result["quotas"][0]["name"])
        self.assertIn("weekly", result["quotas"][1]["name"])
        self.assertIn("Other model", result["quotas"][2]["name"])
        self.assertEqual(result["balances"][0]["remaining"], 3)
        self.assertEqual(result["plan_type"], "plus")
        self.assertEqual(result["available_resets"], 2)
        self.assertNotIn("must-not-be-exposed", json.dumps(result))

    def test_legacy_and_null_fields_and_server_reset_are_preserved(self):
        result = usage_service.normalize_codex_api({"rateLimits": {
            "primary": {"usedPercent": 91, "resetsAt": 1}, "secondary": None, "credits": None}})
        self.assertEqual(result["quotas"][0]["remaining"], 9)
        self.assertEqual(result["quotas"][0]["reset_at"], 1)
        self.assertNotIn("available_resets", result)
        self.assertEqual(result["balances"], [])

    def test_empty_or_invalid_values_are_not_success(self):
        for payload in ({}, {"rateLimits": None}, {"rateLimits": {"primary": {"usedPercent": "NaN"}}}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "no subscription quota"):
                usage_service.normalize_codex_api(payload)

    def test_unlimited_credits_without_percentage(self):
        result = usage_service.normalize_codex_api({"rateLimits": {"credits": {"unlimited": True}}})
        self.assertTrue(result["credits_unlimited"])
        self.assertEqual(result["quotas"], [])


class TestCodexTransport(unittest.TestCase):
    def test_nvm_cli_and_matching_interpreter_work_without_interactive_path(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            bin_dir = Path(root) / "versions/node/v24.19.0/bin"
            bin_dir.mkdir(parents=True)
            # A Python fixture stands in for Node; env must resolve it from
            # the same installation as the npm-style Codex launcher.
            (bin_dir / "node").symlink_to(sys.executable)
            launcher = bin_dir / "codex"
            launcher.write_text('''#!/usr/bin/env node
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    result = {} if message['method'] == 'initialize' else {'rateLimits': {'primary': {'usedPercent': 12}}}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
''')
            launcher.chmod(0o700)
            real_access = os.access
            with patch.dict(os.environ, {"NVM_DIR": root, "PATH": "/usr/bin:/bin"}), \
                    patch.object(codex_usage.shutil, "which", return_value=None), \
                    patch.object(codex_usage.os, "access", side_effect=lambda p, mode: str(p).startswith(root) and real_access(p, mode)):
                result = codex_usage.read_rate_limits(timeout=2)
            self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 12)

    def test_remote_preserves_known_actionable_errors_but_not_arbitrary_output(self):
        known = "Codex CLI not found on this host. Install Codex and sign in with ChatGPT."
        for error in (known, "secret-token from unknown diagnostic"):
            response = subprocess.CompletedProcess([], 0, json.dumps({"error": error}), "")
            with self.subTest(error=error), patch.object(usage_service, "run_ssh_command", return_value=response):
                with self.assertRaises(ValueError) as raised:
                    usage_service.read_codex_api(devboost, {}, {"ssh_host": "remote"})
            if error == known:
                self.assertEqual(str(raised.exception), known)
            else:
                self.assertNotIn("secret-token", str(raised.exception))

    def run_fake(self, script, timeout=2, codex_home=None):
        real_popen = subprocess.Popen
        self.processes = []

        def spawn(argv, **kwargs):
            self.assertEqual(argv[1:], ["app-server"])
            self.assertEqual(kwargs["cwd"], os.path.expanduser("~"))
            if codex_home:
                self.assertEqual(kwargs["env"]["CODEX_HOME"], os.path.abspath(codex_home))
            process = real_popen([sys.executable, "-u", "-c", script], **kwargs)
            self.processes.append(process)
            return process

        with patch.object(codex_usage.shutil, "which", return_value=sys.executable), \
                patch.object(codex_usage.subprocess, "Popen", side_effect=spawn):
            return codex_usage.read_rate_limits(codex_home, timeout=timeout)

    def test_real_stdio_handshake_notifications_and_cleanup(self):
        script = '''import json, sys
initialized = False
for line in sys.stdin:
    m = json.loads(line)
    if m['method'] == 'initialized':
        initialized = True
        continue
    if m['method'] == 'initialize':
        assert m['params']['clientInfo']['name'] == 'devboost'
        result = {}
    else:
        assert initialized and m['method'] == 'account/rateLimits/read'
        result = {'rateLimits': {'primary': {'usedPercent': 25}}}
    print(json.dumps({'method': 'account/updated', 'params': {}}), flush=True)
    print(json.dumps({'id': m['id'], 'result': result}), flush=True)
'''
        result = self.run_fake(script, codex_home="/tmp/test codex profile")
        self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 25)
        self.assertIsNotNone(self.processes[0].poll())

    def test_timeout_reaps_process_even_with_partial_json(self):
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            self.run_fake("import sys, time; sys.stdout.write('{'); sys.stdout.flush(); time.sleep(30)", timeout=0.1)
        self.assertIsNotNone(self.processes[0].poll())

    def test_eof_and_invalid_json(self):
        for script, message in (("pass", "exited"), ("print('bad JSON')", "invalid JSON")):
            with self.subTest(script=script), self.assertRaisesRegex(ValueError, message):
                self.run_fake(script)
            self.assertIsNotNone(self.processes[0].poll())

    def test_protocol_error_does_not_leak_auth_diagnostics(self):
        script = '''import json, sys
m = json.loads(sys.stdin.readline())
print(json.dumps({'id': m['id'], 'error': {'code': -32000, 'message': 'secret-token'}}), flush=True)
'''
        with self.assertRaises(ValueError) as raised:
            self.run_fake(script)
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertIn("ChatGPT sign-in", str(raised.exception))

    def test_remote_exec_uses_selected_host_profile_and_normalizes_locally(self):
        response = subprocess.CompletedProcess([], 0, json.dumps({"result": api_payload()}), "")
        with patch.object(usage_service, "run_ssh_command", return_value=response) as ssh:
            result = usage_service.read_codex_api(devboost, {"codex_home": "~/profile 'work'"}, {"ssh_host": "work"})
        self.assertEqual(ssh.call_args.args[0], "work")
        args = shlex.split(ssh.call_args.args[1])
        self.assertEqual(args[:2], ["python3", "-c"])
        self.assertEqual(args[3], "~/profile 'work'")
        compile(args[2], "remote_codex_helper", "exec")
        self.assertEqual(result["quotas"][0]["remaining"], 75)

    def test_remote_fallback_respects_hosts_codex_home(self):
        response = subprocess.CompletedProcess([], 0, 'DEVBOOST_USAGE_FILE:record.jsonl\n', '')
        with patch.object(usage_service, "run_ssh_command", return_value=response) as ssh:
            result = usage_service.read_remote_transcript_usage(devboost, {"provider": "codex"}, "remote")
        self.assertIn('${CODEX_HOME:-$HOME/.codex}/sessions', ssh.call_args.args[1])
        self.assertTrue(result["stale"])


class TestCodexSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = patch.object(devboost, "CONFIG_DIR", self.tmp.name)
        self.config_file = patch.object(devboost, "CONFIG_FILE", os.path.join(self.tmp.name, "config.json"))
        self.config_dir.start()
        self.config_file.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.config_dir.stop)
        self.addCleanup(self.config_file.stop)
        self.account = devboost.save_usage_account({"provider": "codex", "name": "work", "codex_home": "/tmp/work-codex"})

    def test_default_reads_live_and_recent_cache_then_force_refresh(self):
        with patch.object(devboost, "_read_codex_upstream", return_value={**usage_service.normalize_codex_api(api_payload()), "source": "[HTTPS] OpenAI upstream"}) as live, \
                patch.object(devboost, "_read_local_transcript_usage") as records:
            first = devboost.get_usage_status()["snapshots"][self.account["id"]]
            devboost.get_usage_status()
            self.assertEqual(live.call_count, 1)
            devboost.get_usage_status(refresh=True)
            self.assertEqual(live.call_count, 2)
            records.assert_not_called()
        self.assertEqual(first["source"], "[HTTPS] OpenAI upstream")
        self.assertEqual(first["last_valid_query_at"], first["updated_at"])
        self.assertEqual(first["available_resets"], 2)
        self.assertEqual(live.call_args.args[0]["codex_home"], "/tmp/work-codex")

    def test_expired_cache_refreshes_without_manual_refresh(self):
        cfg = devboost.load_config()
        cfg["usage_snapshots"] = {self.account["id"]: {"updated_at": "2000-01-01T00:00:00+00:00"}}
        devboost.save_config(cfg)
        with patch.object(devboost, "_read_codex_upstream", return_value={**usage_service.normalize_codex_api(api_payload()), "source": "[HTTPS] OpenAI upstream"}) as live:
            devboost.get_usage_status()
        live.assert_called_once()

    def test_profile_edit_invalidates_local_and_remote_snapshots(self):
        cfg = devboost.load_config()
        aid = self.account["id"]
        cfg["usage_snapshots"] = {aid: {}, "remote:" + aid: {}, "other-account": {}}
        devboost.save_config(cfg)
        saved = devboost.save_usage_account(dict(self.account, codex_home="~/new-profile"))
        self.assertEqual(saved["codex_home"], "~/new-profile")
        self.assertEqual(devboost.load_config()["usage_snapshots"], {"other-account": {}})

    def test_failure_keeps_last_api_values_and_original_success_age(self):
        with patch.object(devboost, "_read_codex_upstream", return_value={**usage_service.normalize_codex_api(api_payload()), "source": "[HTTPS] OpenAI upstream"}):
            first = devboost.get_usage_status()["snapshots"][self.account["id"]]
        with patch.object(devboost, "_read_codex_upstream", side_effect=ValueError("offline")), \
                patch.object(devboost, "_read_local_transcript_usage", return_value={"quotas": [{"remaining": 99}], "stale": True}):
            second = devboost.get_usage_status(refresh=True)["snapshots"][self.account["id"]]
        self.assertTrue(second["stale"])
        self.assertEqual(second["quotas"][0]["remaining"], 99)
        self.assertNotIn("last_valid_query_at", second)
        self.assertIn("offline", second["message"])

    def test_first_offline_fallback_is_historical_not_a_valid_api_query(self):
        with patch.object(devboost, "_read_codex_upstream", side_effect=ValueError("offline")), \
                patch.object(devboost, "_read_local_transcript_usage", return_value={"quotas": [{"remaining": 33}], "source": "codex local records"}):
            result = devboost.get_usage_status()["snapshots"][self.account["id"]]
        self.assertTrue(result["stale"])
        self.assertNotIn("last_valid_query_at", result)

    def test_explicit_sources_keep_precedence(self):
        for fields, adapter in (({"api_mode": True}, "_read_provider_api"),
                                ({"balance_url": "https://example.test"}, "_read_usage_http"),
                                ({"usage_command": ["custom"]}, "_read_usage_command"),
                                ({"local_path": "/records"}, "_read_local_transcript_usage")):
            with self.subTest(fields=fields), patch.object(devboost, "_read_codex_upstream") as live, \
                    patch.object(devboost, adapter, return_value={"quotas": []}) as explicit:
                result = devboost.refresh_usage_account(dict(self.account, **fields))
                self.assertTrue(result["ok"])
                explicit.assert_called_once()
                live.assert_not_called()

    def test_other_providers_never_use_codex_adapter(self):
        with patch.object(devboost, "_read_codex_api") as live, \
                patch.object(devboost, "_read_local_transcript_usage", return_value={"quotas": []}):
            devboost.refresh_usage_account({"provider": "claude"})
            live.assert_not_called()


if __name__ == "__main__":
    unittest.main()
