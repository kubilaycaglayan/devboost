import os
import plistlib
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestFolderSync(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_config_dir = devboost.CONFIG_DIR
        self.orig_config_file = devboost.CONFIG_FILE
        self.orig_launch_dir = devboost.LAUNCH_AGENTS_DIR
        self.orig_log_dir = devboost.LOG_DIR
        devboost.CONFIG_DIR = self.temp_dir.name
        devboost.CONFIG_FILE = os.path.join(self.temp_dir.name, "config.json")
        devboost.LAUNCH_AGENTS_DIR = os.path.join(self.temp_dir.name, "agents")
        devboost.LOG_DIR = self.temp_dir.name
        os.makedirs(devboost.LAUNCH_AGENTS_DIR, exist_ok=True)

    def tearDown(self):
        devboost.CONFIG_DIR = self.orig_config_dir
        devboost.CONFIG_FILE = self.orig_config_file
        devboost.LAUNCH_AGENTS_DIR = self.orig_launch_dir
        devboost.LOG_DIR = self.orig_log_dir
        self.temp_dir.cleanup()

    def _server_id(self):
        return devboost.load_config()["servers"][0]["id"]

    def test_migration_adds_sync_keys(self):
        cfg = devboost.load_config()
        self.assertIn("syncs", cfg)
        self.assertIn("folder_history", cfg)

    def test_sync_migration_normalizes_invalid_entries_and_history(self):
        cfg = {
            "servers": [{"id": "one"}],
            "syncs": [{"id": "same", "direction": "invalid", "mirror": True,
                        "interval": 9999}, {"id": "same"}, "bad"],
            "folder_history": [{"local_path": "/tmp/a"}, {}, "bad"],
        }
        migrated, changed = devboost.ensure_syncs_migrated(cfg)
        self.assertTrue(changed)
        self.assertEqual(migrated["syncs"][0]["direction"], "two-way")
        self.assertFalse(migrated["syncs"][0]["mirror"])
        self.assertEqual(migrated["syncs"][0]["interval"], 600)
        self.assertNotEqual(migrated["syncs"][0]["id"], migrated["syncs"][1]["id"])
        self.assertEqual(len(migrated["folder_history"]), 1)

    def test_validate_rejects_root_and_empty(self):
        with self.assertRaises(ValueError):
            devboost.validate_sync_paths("/", "/data")
        with self.assertRaises(ValueError):
            devboost.validate_sync_paths("", "")
        with self.assertRaises(ValueError):
            devboost.validate_sync_paths("/tmp/a", "/x:y")

    def test_two_way_ignores_mirror(self):
        sync = {"local_path": "/tmp/a", "remote_path": "/tmp/b",
                "direction": "two-way", "mirror": True}
        cmds = devboost.build_rsync_commands(sync, "myhost")
        self.assertEqual(len(cmds), 2)  # push + pull
        self.assertTrue(all("--delete" not in c for c in cmds))

    def test_push_mirror_uses_delete(self):
        sync = {"local_path": "/tmp/a", "remote_path": "/tmp/b",
                "direction": "push", "mirror": True}
        cmds = devboost.build_rsync_commands(sync, "myhost")
        self.assertEqual(len(cmds), 1)
        self.assertIn("--delete", cmds[0])

    def test_push_no_mirror_safe(self):
        sync = {"local_path": "/tmp/a", "remote_path": "/tmp/b",
                "direction": "push", "mirror": False}
        cmds = devboost.build_rsync_commands(sync, "myhost")
        self.assertEqual(len(cmds), 1)
        self.assertNotIn("--delete", cmds[0])

    def test_add_sync_once_has_no_agent(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", direction="push",
                               always=False, run_now=False)
        self.assertTrue(res["ok"])
        self.assertFalse(os.path.exists(
            devboost.get_sync_plist_path(res["sync"]["id"])))

    @patch("subprocess.run")
    def test_add_sync_auto_installs_agent(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", direction="pull",
                               mirror=True, always=True, interval=30,
                               run_now=False)
        self.assertTrue(res["ok"])
        plist_path = devboost.get_sync_plist_path(res["sync"]["id"])
        self.assertTrue(os.path.exists(plist_path))
        with open(plist_path, "rb") as f:
            data = plistlib.load(f)
        self.assertEqual(data["StartInterval"], 30)
        self.assertTrue(data["RunAtLoad"])
        self.assertIn("sync-run", data["ProgramArguments"])

    def test_add_sync_forces_mirror_off_for_two_way(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", direction="two-way",
                               mirror=True, always=False, run_now=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["sync"]["mirror"])

    def test_toggle_always(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", always=False, run_now=False)
        sync_id = res["sync"]["id"]
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            devboost.toggle_sync_always(sync_id, True)
            self.assertTrue(os.path.exists(devboost.get_sync_plist_path(sync_id)))
            devboost.toggle_sync_always(sync_id, False)
            self.assertFalse(os.path.exists(devboost.get_sync_plist_path(sync_id)))

    def test_remove_sync(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", run_now=False)
        self.assertTrue(devboost.remove_sync_entry(res["sync"]["id"]))
        self.assertFalse(devboost.remove_sync_entry("does-not-exist"))

    def test_folder_history_remembers_pairs(self):
        sid = self._server_id()
        devboost.record_folder_history(sid, "/tmp/a", "~/b")
        hist = devboost.get_folder_history(server_id=sid)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["local_path"], "/tmp/a")
        # Duplicate re-insert stays at front, capped
        devboost.record_folder_history(sid, "/tmp/a", "~/b")
        self.assertEqual(len(devboost.get_folder_history(server_id=sid)), 1)

    def test_browse_local(self):
        r = devboost.browse_local(os.path.expanduser("~"))
        self.assertTrue(r["ok"])
        self.assertIn("path", r)
        self.assertIn("entries", r)
        r2 = devboost.browse_local("/nonexistent-devboost-xyz-123")
        self.assertFalse(r2["ok"])

    def test_browse_local_shows_hidden_folders(self):
        base = os.path.join(self.temp_dir.name, "proj")
        os.makedirs(os.path.join(base, ".output", "sub"))
        os.makedirs(os.path.join(base, "visible"))
        r = devboost.browse_local(base)
        self.assertTrue(r["ok"])
        by_name = {e["name"]: e for e in r["entries"]}
        self.assertIn(".output", by_name)
        self.assertTrue(by_name[".output"]["hidden"])
        self.assertIn("visible", by_name)
        self.assertFalse(by_name["visible"]["hidden"])

    def test_browse_local_flags_tcc_denial(self):
        with patch("os.listdir", side_effect=PermissionError(1, "Operation not permitted", "/protected")):
            with patch("os.path.exists", return_value=True), patch("os.path.isdir", return_value=True):
                r = devboost.browse_local("/protected")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("denied"))

    def test_mkdir_local_creates_folder(self):
        base = os.path.join(self.temp_dir.name, "proj")
        os.makedirs(base)
        r = devboost.mkdir_local(base, "newdir")
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isdir(os.path.join(base, "newdir")))
        # Existing dir is fine (idempotent)
        r2 = devboost.mkdir_local(base, "newdir")
        self.assertTrue(r2["ok"])

    def test_mkdir_local_rejects_bad_names(self):
        base = os.path.join(self.temp_dir.name, "proj")
        os.makedirs(base)
        for bad in ["", "  ", ".", "..", "a/b", "a\\b"]:
            with self.assertRaises(ValueError, msg=bad):
                devboost.mkdir_local(base, bad)

    def test_explain_rsync_output_hints(self):
        self.assertIn("apt install rsync", devboost.explain_rsync_output(
            "bash: rsync: command not found\nrsync error: code 12", "myhost"))
        self.assertIn("SSH key", devboost.explain_rsync_output(
            "Permission denied (publickey,password).", "myhost"))
        self.assertIn("never started", devboost.explain_rsync_output(
            "rsync: connection unexpectedly closed (0 bytes received so far) [sender]\n"
            "rsync error: error in rsync protocol data stream (code 12) at io.c(232)",
            "myhost"))
        # Unknown output passes through unchanged
        self.assertEqual(devboost.explain_rsync_output("some odd failure"), "some odd failure")

    def test_explain_rsync_output_tcc_is_exclusive(self):
        # Local TCC denial explains the whole failure: no remote-rsync guess
        # may be appended on top of it.
        out = devboost.explain_rsync_output(
            "rsync: open \"/Users/x/Documents/a/\" failed: Operation not permitted (1)\n"
            "rsync: connection unexpectedly closed (4 bytes received so far) [sender]\n"
            "rsync error: error in rsync protocol data stream (code 12) at io.c(232)",
            "myhost")
        self.assertIn("Full Disk Access", out)
        self.assertNotIn("never started", out)
        self.assertNotIn("apt install", out)

    def test_check_rsync_prereqs_local_missing(self):
        with patch("shutil.which", return_value=None):
            msg = devboost.check_rsync_prereqs("myhost")
        self.assertIn("this Mac", msg)

    def test_check_rsync_prereqs_remote_missing(self):
        fake = MagicMock(returncode=1, stdout="", stderr="")
        with patch("shutil.which", return_value="/usr/bin/rsync"), \
                patch.object(devboost, "_ssh_remote_cmd", return_value=fake):
            msg = devboost.check_rsync_prereqs("myhost")
        self.assertIn("myhost", msg)
        self.assertIn("apt install rsync", msg)

    def test_check_rsync_prereqs_ok(self):
        fake = MagicMock(returncode=0, stdout="/usr/bin/rsync\n", stderr="")
        with patch("shutil.which", return_value="/usr/bin/rsync"), \
                patch.object(devboost, "_ssh_remote_cmd", return_value=fake):
            self.assertIsNone(devboost.check_rsync_prereqs("myhost"))

    def test_run_sync_fails_fast_without_remote_rsync(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", run_now=False)
        sync_id = res["sync"]["id"]
        fake = MagicMock(returncode=1, stdout="", stderr="")
        with patch.object(devboost, "_ensure_remote_dir", return_value=True), \
                patch("shutil.which", return_value="/usr/bin/rsync"), \
                patch.object(devboost, "_ssh_remote_cmd", return_value=fake), \
                patch.object(devboost, "build_rsync_commands") as mock_build:
            out = devboost.run_sync(sync_id)
        self.assertFalse(out["ok"])
        self.assertIn("apt install rsync", out["message"])
        mock_build.assert_not_called()

    def test_run_sync_surfaces_cause_lines(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", run_now=False)
        sync_id = res["sync"]["id"]
        rsync_fail = MagicMock(
            returncode=12,
            stdout="",
            stderr=("rsync: connection unexpectedly closed (0 bytes received so far) [sender]\n"
                    "rsync error: error in rsync protocol data stream (code 12) at io.c(232) [sender=3.4.1]\n"))
        with patch.object(devboost, "_ensure_remote_dir", return_value=True), \
                patch.object(devboost, "check_rsync_prereqs", return_value=None), \
                patch("subprocess.run", return_value=rsync_fail):
            out = devboost.run_sync(sync_id)
        self.assertFalse(out["ok"])
        # Cause line kept, not just the code-12 summary — plus a hint.
        self.assertIn("connection unexpectedly closed", out["message"])
        self.assertIn("never started", out["message"])

    def test_syncs_status_shape(self):
        sid = self._server_id()
        devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                          remote_path="~/b", run_now=False)
        st = devboost.get_syncs_status(sid)
        self.assertIn("syncs", st)
        self.assertEqual(len(st["syncs"]), 1)
        row = st["syncs"][0]
        for key in ("id", "local_path", "remote_path", "direction",
                    "mirror", "always", "last_status"):
            self.assertIn(key, row)

    def test_update_sync_paths_and_params(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", direction="push",
                               run_now=False)
        sync_id = res["sync"]["id"]
        updated = devboost.update_sync(sync_id, remote_path="~/c",
                                      direction="pull", mirror=True,
                                      interval=45)
        self.assertIsNotNone(updated)
        self.assertEqual(updated["remote_path"], "~/c")
        self.assertEqual(updated["local_path"], "/tmp/a")  # unchanged
        self.assertEqual(updated["direction"], "pull")
        self.assertTrue(updated["mirror"])
        self.assertEqual(updated["interval"], 45)

    def test_update_sync_forces_mirror_off_for_two_way(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", direction="push",
                               mirror=True, run_now=False)
        updated = devboost.update_sync(res["sync"]["id"], direction="two-way")
        self.assertEqual(updated["direction"], "two-way")
        self.assertFalse(updated["mirror"])

    def test_update_sync_rejects_bad_direction(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", run_now=False)
        with self.assertRaises(ValueError):
            devboost.update_sync(res["sync"]["id"], direction="sideways")

    def test_update_sync_unknown_id(self):
        self.assertIsNone(devboost.update_sync("does-not-exist", direction="push"))

    def test_is_tcc_protected_path(self):
        home = os.path.expanduser("~")
        for sub in ("Documents", "Desktop", "Downloads"):
            self.assertTrue(devboost.is_tcc_protected_path(os.path.join(home, sub, "proj")))
            self.assertTrue(devboost.is_tcc_protected_path(os.path.join(home, sub)))
        self.assertTrue(devboost.is_tcc_protected_path(
            os.path.join(home, "Library", "Mobile Documents", "x")))
        self.assertFalse(devboost.is_tcc_protected_path(home))
        self.assertFalse(devboost.is_tcc_protected_path("/tmp/proj"))
        self.assertFalse(devboost.is_tcc_protected_path(
            os.path.join(home, ".config", "devboost")))
        self.assertFalse(devboost.is_tcc_protected_path(""))

    def _make_wrapper(self, directory):
        os.makedirs(os.path.join(directory, "bin"), exist_ok=True)
        wrapper = os.path.join(directory, "bin", devboost.DASHBOARD_WRAPPER_NAME)
        with open(wrapper, "w") as f:
            f.write("#!/bin/sh\nexec true\n")
        os.chmod(wrapper, 0o755)
        return wrapper

    def test_sync_executable_prefers_running_copy(self):
        repo_dir = os.path.join(self.temp_dir.name, "repo")
        repo_wrapper = self._make_wrapper(repo_dir)
        with patch.object(devboost, "BIN_DIR", os.path.join(repo_dir, "bin")), \
                patch.object(devboost, "_installed_code_dir",
                             return_value=os.path.join(self.temp_dir.name, "nonexistent")):
            args = devboost.get_sync_executable_args("abc123")
        self.assertEqual(args, [repo_wrapper, "sync-run", "abc123"])

    def test_sync_executable_avoids_protected_running_copy(self):
        repo_dir = os.path.join(self.temp_dir.name, "repo")
        repo_wrapper = self._make_wrapper(repo_dir)
        inst_dir = os.path.join(self.temp_dir.name, "installed")
        inst_wrapper = self._make_wrapper(inst_dir)
        real_protected = devboost.is_tcc_protected_path
        with patch.object(devboost, "BIN_DIR", os.path.join(repo_dir, "bin")), \
                patch.object(devboost, "_installed_code_dir", return_value=inst_dir), \
                patch.object(devboost, "is_tcc_protected_path",
                             side_effect=lambda p: os.path.abspath(p) == os.path.abspath(repo_wrapper) or real_protected(p)):
            args = devboost.get_sync_executable_args("abc123")
        self.assertEqual(args, [inst_wrapper, "sync-run", "abc123"])

    def test_packaged_sync_executable_uses_native_app_launcher(self):
        contents = os.path.join(self.temp_dir.name, "DevBoost.app", "Contents")
        resources = os.path.join(contents, "Resources")
        executable = os.path.join(contents, "MacOS", "DevBoost")
        os.makedirs(os.path.dirname(executable), exist_ok=True)
        with open(executable, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(executable, 0o755)
        with patch.object(devboost, "CODE_DIR", resources):
            args = devboost.get_sync_executable_args("abc123")
        self.assertEqual(args, [os.path.realpath(executable), "sync-run", "abc123"])

    def test_syncs_status_flags_protected_local(self):
        sid = self._server_id()
        home = os.path.expanduser("~")
        devboost.add_sync(server_ref=sid, local_path=os.path.join(home, "Documents", "proj"),
                          remote_path="~/r", run_now=False)
        devboost.add_sync(server_ref=sid, local_path="/tmp/plain",
                          remote_path="~/r2", run_now=False)
        rows = {r["local_path"]: r for r in devboost.get_syncs_status(sid)["syncs"]}
        self.assertTrue(rows[os.path.join(home, "Documents", "proj")]["local_protected"])
        self.assertFalse(rows["/tmp/plain"]["local_protected"])

    def test_update_sync_refreshes_agent_interval(self):
        sid = self._server_id()
        res = devboost.add_sync(server_ref=sid, local_path="/tmp/a",
                               remote_path="~/b", always=True,
                               interval=15, run_now=False)
        sync_id = res["sync"]["id"]
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            devboost.update_sync(sync_id, interval=60)
            with open(devboost.get_sync_plist_path(sync_id), "rb") as f:
                data = plistlib.load(f)
        self.assertEqual(data["StartInterval"], 60)

    def test_restore_auto_sync_agents_recreates_only_auto_entries(self):
        created = []
        def create(sync):
            created.append(sync)
        sid = self._server_id()
        devboost.add_sync(server_ref=sid, local_path="/tmp/auto-a", remote_path="~/a",
                          always=True, run_now=False)
        devboost.add_sync(server_ref=sid, local_path="/tmp/once-b", remote_path="~/b",
                          always=False, run_now=False)
        with patch("devboost_app.folder_sync.create_sync_agent", create):
            devboost.restore_auto_sync_agents()
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0]["always"])

    def test_remove_sync_agents_for_server_removes_its_entries(self):
        removed = []
        def remove(sync_id):
            removed.append(sync_id)
        sid = self._server_id()
        result = devboost.add_sync(server_ref=sid, local_path="/tmp/a", remote_path="~/a",
                                   run_now=False)
        with patch("devboost_app.folder_sync.remove_sync_agent", remove):
            devboost.remove_sync_agents_for_server(sid)
        self.assertEqual(removed, [result["sync"]["id"]])

    @patch.object(devboost, "_ssh_remote_cmd")
    def test_browse_remote_expands_home_and_lists_hidden_directories(self, remote):
        remote.side_effect = [
            MagicMock(returncode=0, stdout="/home/test\n", stderr=""),
            MagicMock(returncode=0, stdout=".config/\nvisible/\nfile.txt\n", stderr=""),
        ]
        result = devboost.browse_remote(self._server_id(), "~")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/home/test")
        self.assertEqual([e["name"] for e in result["entries"]], [".config", "visible"])
        self.assertTrue(result["entries"][0]["hidden"])
        self.assertIn("ls -1pa", remote.call_args_list[1].args[1])

    @patch.object(devboost, "_ssh_remote_cmd")
    def test_browse_remote_reports_unreachable_server(self, remote):
        remote.return_value = MagicMock(returncode=255, stdout="", stderr="")
        result = devboost.browse_remote(self._server_id(), "~")
        self.assertFalse(result["ok"])
        self.assertIn("Cannot reach", result["message"])

    @patch.object(devboost, "_ssh_remote_cmd")
    def test_mkdir_remote_expands_home_and_quotes_path(self, remote):
        remote.side_effect = [
            MagicMock(returncode=0, stdout="/home/test\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = devboost.mkdir_remote(self._server_id(), "~/projects", "new folder")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "/home/test/projects/new folder")
        self.assertIn("mkdir -p", remote.call_args_list[1].args[1])
        self.assertIn("new folder", remote.call_args_list[1].args[1])

    def test_run_sync_success_updates_state_and_history(self):
        sid = self._server_id()
        result = devboost.add_sync(server_ref=sid, local_path="/tmp/devboost-sync-test",
                                   remote_path="~/remote", direction="two-way",
                                   run_now=False)
        with patch.object(devboost, "_ensure_remote_dir", return_value=True), \
                patch.object(devboost, "check_rsync_prereqs", return_value=None), \
                patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
            outcome = devboost.run_sync(result["sync"]["id"])
        self.assertTrue(outcome["ok"])
        self.assertEqual(run.call_count, 2)
        saved = devboost.get_syncs_status(sid)["syncs"][0]
        self.assertEqual(saved["last_status"], "ok")
        self.assertIsNotNone(saved["last_sync"])
        self.assertTrue(devboost.get_folder_history(server_id=sid))

    def test_run_sync_timeout_is_persisted_as_error(self):
        sid = self._server_id()
        result = devboost.add_sync(server_ref=sid, local_path="/tmp/devboost-sync-test",
                                   remote_path="~/remote", run_now=False)
        with patch.object(devboost, "_ensure_remote_dir", return_value=True), \
                patch.object(devboost, "check_rsync_prereqs", return_value=None), \
                patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("rsync", 1)):
            outcome = devboost.run_sync(result["sync"]["id"], timeout=7)
        self.assertFalse(outcome["ok"])
        self.assertIn("timed out after 7s", outcome["message"])
        saved = devboost.get_syncs_status(sid)["syncs"][0]
        self.assertEqual(saved["last_status"], "error")


if __name__ == "__main__":
    unittest.main()
