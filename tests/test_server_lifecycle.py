import os
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestServerLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_file, self.old_dir = devboost.CONFIG_FILE, devboost.CONFIG_DIR
        devboost.CONFIG_DIR = self.temp.name
        devboost.CONFIG_FILE = os.path.join(self.temp.name, "config.json")

    def tearDown(self):
        devboost.CONFIG_FILE, devboost.CONFIG_DIR = self.old_file, self.old_dir
        self.temp.cleanup()

    def test_add_update_pin_reorder_and_duplicate_server(self):
        first = devboost.load_config()["servers"][0]
        second = devboost.add_server("staging.example", "Staging", "10.0.0.2")
        self.assertEqual(second["name"], "Staging")
        with self.assertRaises(ValueError):
            devboost.add_server("staging.example")
        updated = devboost.update_server_entry(second["id"], name="", ip=" 10.0.0.3 ")
        self.assertEqual(updated["name"], "staging.example")
        self.assertEqual(updated["ip"], "10.0.0.3")
        devboost.set_server_pinned(second["id"], True)
        self.assertEqual(devboost.get_servers()[0]["id"], second["id"])
        ordered = devboost.reorder_servers([first["id"], second["id"]])
        self.assertEqual([s["id"] for s in ordered], [second["id"], first["id"]])

    def test_migration_deduplicates_ids_and_moves_legacy_metadata(self):
        cfg = {
            "servers": [{"id": "same", "ssh_host": "one"}, {"id": "same", "ssh_host": "two"}],
            "labels": {"3000": "App"}, "history": [{"local_port": 3000}],
        }
        migrated, changed = devboost.ensure_servers_migrated(cfg)
        self.assertTrue(changed)
        self.assertEqual([s["id"] for s in migrated["servers"]], ["same", "same-2"])
        self.assertEqual(migrated["server_labels"]["same"]["3000"], "App")
        self.assertEqual(migrated["history"][0]["server_id"], "same")

    def test_labels_keep_legacy_bootstrap_copy_in_sync(self):
        cfg = devboost.load_config()
        sid = cfg["servers"][0]["id"]
        devboost.set_server_label(cfg, sid, 3030, "Dashboard")
        self.assertEqual(cfg["server_labels"][sid]["3030"], "Dashboard")
        self.assertEqual(cfg["labels"]["3030"], "Dashboard")
        devboost.remove_server_label(cfg, sid, 3030)
        self.assertNotIn("3030", cfg["server_labels"][sid])
        self.assertNotIn("3030", cfg["labels"])

    def test_remove_server_cleans_related_state_and_agents(self):
        first = devboost.load_config()["servers"][0]
        second = devboost.add_server("staging")
        cfg = devboost.load_config()
        cfg["server_labels"][second["id"]] = {"3000": "x"}
        cfg["history"] = [{"server_id": second["id"]}, {"server_id": first["id"]}]
        cfg["folder_history"] = [{"server_id": second["id"]}]
        cfg["syncs"] = [{"id": "sync-1", "server_id": second["id"]}, {"id": "sync-2", "server_id": first["id"]}]
        devboost.save_config(cfg)
        with patch.object(devboost, "remove_launchagents_for_server"), \
                patch.object(devboost, "kill_server_processes"), \
                patch.object(devboost, "remove_sync_agent") as remove_agent:
            self.assertTrue(devboost.remove_server_entry(second["id"]))
        result = devboost.load_config()
        self.assertEqual(len(result["servers"]), 1)
        self.assertNotIn(second["id"], result["server_labels"])
        self.assertEqual(result["history"], [{"server_id": first["id"]}])
        self.assertEqual(len(result["syncs"]), 1)
        self.assertEqual(result["syncs"][0]["id"], "sync-2")
        self.assertEqual(result["syncs"][0]["server_id"], first["id"])
        remove_agent.assert_called_once_with("sync-1")

    def test_cannot_remove_last_server_or_unknown_server(self):
        with self.assertRaises(ValueError):
            devboost.remove_server_entry(devboost.load_config()["servers"][0]["id"])
        self.assertFalse(devboost.remove_server_entry("unknown"))


if __name__ == "__main__":
    unittest.main()
