import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestCLIDispatch(unittest.TestCase):
    def run_cli(self, *args):
        output = io.StringIO()
        with patch.object(sys, "argv", ["devboost", *args]), contextlib.redirect_stdout(output):
            devboost.main()
        return output.getvalue()

    def test_server_flag_parser_supports_separate_and_equals_forms(self):
        self.assertEqual(devboost._extract_server_flag(["--server", "box", "--all"]),
                         ("box", ["--all"]))
        self.assertEqual(devboost._extract_server_flag(["--server=box", "x"]),
                         ("box", ["x"]))

    def test_bare_command_dispatches_to_list(self):
        calls = []
        def fake_cli_list(*args, **kwargs):
            calls.append((args, kwargs))
        with patch("devboost_app.cli.cli_list", fake_cli_list):
            self.run_cli("ls", "--server", "box", "--all")
        self.assertEqual(calls, [((), {"server_ref": "box", "show_all": True})])

    @patch.object(devboost, "add_forward")
    @patch.object(devboost, "resolve_server", return_value={"ssh_host": "box"})
    @patch.object(devboost, "load_config", return_value={})
    def test_add_command_parses_remote_port_label_and_mode(self, _load, _resolve, add):
        output = self.run_cli("add", "3000", "3001", "Web", "App", "--always", "--server=box")
        add.assert_called_once_with(3000, 3001, label="Web App", always=True, server_ref="box")
        self.assertIn("box:3001", output)
        self.assertIn("persistent (ALWAYS)", output)

    @patch.object(devboost, "run_sync", return_value={"ok": False, "message": "sync failed"})
    def test_sync_run_returns_nonzero_for_failed_sync(self, run):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("sync-run", "abc")
        self.assertEqual(ctx.exception.code, 1)
        run.assert_called_once_with("abc")

    def test_missing_required_arguments_exit_with_helpful_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("add")
        self.assertEqual(ctx.exception.code, 1)

    def test_help_and_unknown_command_show_usage(self):
        help_text = self.run_cli("--help")
        unknown_text = self.run_cli("unknown")
        self.assertIn("Folder sync modes", help_text)
        self.assertIn("DevBoost", unknown_text)


if __name__ == "__main__":
    unittest.main()
