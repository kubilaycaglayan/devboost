import json
import os
import tempfile
import unittest
from unittest.mock import patch

from devboost_app import claude_usage


class TestClaudeUsage(unittest.TestCase):
    def test_normalize_usage_maps_detected_windows_and_omits_null_windows(self):
        result = claude_usage.normalize_usage({
            "five_hour": {"utilization": 2.5, "resets_at": "2030-01-01T00:00:00Z"},
            "seven_day": {"utilization": 50, "resets_at": "2030-01-07T00:00:00Z"},
            "seven_day_opus": None,
        })
        self.assertEqual([q["name"] for q in result["quotas"]], [
            "Current session (5-hour window)", "Current week (all models)",
        ])
        self.assertEqual([q["remaining"] for q in result["quotas"]], [97.5, 50])

    def test_normalize_usage_keeps_server_percentages_below_one(self):
        result = claude_usage.normalize_usage({
            "five_hour": {"utilization": 0.5, "resets_at": None},
        })
        self.assertEqual(result["quotas"][0]["remaining"], 99.5)

    @patch.object(claude_usage.urllib.request, "urlopen")
    @patch.object(claude_usage, "read_access_token", return_value="token")
    @patch.object(claude_usage, "detect_claude", return_value="/usr/local/bin/claude")
    def test_read_usage_uses_claude_oauth_headers(self, _cli, _token, urlopen):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"five_hour":{"utilization":12}}'
        urlopen.return_value = Response()
        result = claude_usage.read_usage(timeout=2)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, claude_usage.USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertEqual(request.get_header("Anthropic-beta"), claude_usage.OAUTH_BETA)
        self.assertEqual(result["five_hour"]["utilization"], 12)

    def test_read_file_token_does_not_return_other_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, ".credentials.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"claudeAiOauth": {"accessToken": "secret", "refreshToken": "other"}}, stream)
            self.assertEqual(claude_usage._read_file_token(directory), "secret")


if __name__ == "__main__":
    unittest.main()
