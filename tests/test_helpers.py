import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost
from devboost_app import server_helpers, sync_helpers, usage
from remote_transport import run_ssh_command


class TestPureHelpers(unittest.TestCase):
    def test_server_sorting_and_lookup(self):
        servers = [
            {"id": "two", "ssh_host": "two", "name": "Bravo", "order": 0},
            {"id": "one", "ssh_host": "one", "name": "Alpha", "order": 9, "pinned": True},
        ]
        self.assertEqual([s["id"] for s in server_helpers.sort_servers(servers)], ["one", "two"])
        self.assertIs(server_helpers.get_server({"servers": servers}, "one"), servers[1])
        self.assertIsNone(server_helpers.get_server({"servers": servers}, "missing"))

    def test_server_labels_merge_legacy_and_override(self):
        cfg = {"labels": {"3000": "legacy", "4000": "other"},
               "server_labels": {"one": {"3000": "override", "5000": "new"}}}
        self.assertEqual(server_helpers.get_server_labels(cfg, "one"),
                         {"3000": "override", "4000": "other", "5000": "new"})

    def test_sync_path_normalization_and_validation(self):
        self.assertEqual(sync_helpers.normalize_sync_path(" /tmp/a/ "), "/tmp/a")
        self.assertEqual(sync_helpers.normalize_sync_path("relative", is_remote=True), "/relative")
        self.assertEqual(sync_helpers.normalize_sync_path("~/remote", is_remote=True), "~/remote")
        self.assertEqual(sync_helpers.validate_sync_paths("/tmp/a/", "~/b"), ("/tmp/a", "~/b"))
        for local, remote in (("", "/x"), ("/", "/x"), ("/x", "/"), ("/x", "host:/x")):
            with self.assertRaises(ValueError):
                sync_helpers.validate_sync_paths(local, remote)

    def test_parse_ssh_config_and_include(self):
        with tempfile.TemporaryDirectory() as root:
            included = os.path.join(root, "extra.conf")
            config = os.path.join(root, "config")
            with open(included, "w", encoding="utf-8") as stream:
                stream.write("Host included\n HostName included.example\n User bob\n Port 2200\n")
            with open(config, "w", encoding="utf-8") as stream:
                stream.write("Host *\n  User ignored\nInclude extra.conf\nHost main alias\n HostName main.example\n")
            hosts = sync_helpers.parse_ssh_config_file(config)
        self.assertEqual([h["ssh_host"] for h in hosts], ["included", "main", "alias"])
        self.assertEqual(hosts[0]["port"], 2200)
        self.assertEqual(hosts[1]["hostname"], "main.example")

    def test_usage_helpers_cover_safe_values_and_ids(self):
        self.assertEqual(usage.safe_number("2.5"), 2.5)
        self.assertIsNone(usage.safe_number("nope"))
        self.assertEqual(usage.first({"a": 0, "b": 2}, "a", "b"), 0)
        self.assertEqual(usage.usage_account_id("Claude", "My Work", ["claude-my-work"]), "claude-my-work-2")
        with patch.object(usage.shutil, "which", side_effect=lambda command: "/bin/x" if command == "agy-quota" else None):
            self.assertEqual(usage.provider_default_command("agy"), ["agy-quota", "--json"])
            self.assertIsNone(usage.provider_default_command("custom"))

    def test_usage_normalizer_handles_scalar_and_invalid_rows(self):
        result = usage.normalize_usage_payload({
            "limits": {"daily": {"used": "3", "max": "10", "unit": "tokens"}, "bad": "x"},
            "balance": "4.5", "source": "test",
        })
        self.assertEqual(result["quotas"][0]["remaining"], 7)
        self.assertEqual(result["balances"][0]["remaining"], 4.5)
        self.assertEqual(result["source"], "test")
        self.assertEqual(usage.normalize_usage_payload(None), {"quotas": [], "balances": []})

    @patch("remote_transport.subprocess.run")
    def test_ssh_transport_uses_noninteractive_timeout_shape(self, run):
        run.return_value = MagicMock(returncode=0)
        result = run_ssh_command("box", "printf ok", timeout=12, connect_timeout=3)
        self.assertIs(result, run.return_value)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-2:], ["box", "printf ok"])
        self.assertIn("ConnectTimeout=3", args[0])
        self.assertEqual(kwargs["timeout"], 12)


if __name__ == "__main__":
    unittest.main()
