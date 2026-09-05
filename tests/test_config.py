import os
import json
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_config_dir = devboost.CONFIG_DIR
        self.orig_config_file = devboost.CONFIG_FILE
        devboost.CONFIG_DIR = self.temp_dir.name
        devboost.CONFIG_FILE = os.path.join(self.temp_dir.name, "config.json")

    def tearDown(self):
        devboost.CONFIG_DIR = self.orig_config_dir
        devboost.CONFIG_FILE = self.orig_config_file
        self.temp_dir.cleanup()

    def test_load_default_config_when_missing(self):
        cfg = devboost.load_config()
        self.assertIn("labels", cfg)
        self.assertIn("rules", cfg)

    def test_save_and_load_config(self):
        data = {
            "labels": {"3030": "My Custom Grafana", "8080": "Custom App"},
            "rules": {"3030": {"always": True}}
        }
        devboost.save_config(data)

        loaded = devboost.load_config()
        self.assertEqual(loaded["labels"]["3030"], "My Custom Grafana")
        self.assertEqual(loaded["labels"]["8080"], "Custom App")
        self.assertTrue(loaded["rules"]["3030"]["always"])

    def test_config_backup_recovers_previous_labels_after_corruption(self):
        first = {"labels": {"3030": "Grafana"}, "docker_labels": [{"name": "Work", "match": "work"}]}
        second = {"labels": {"8080": "App"}, "docker_labels": [{"name": "Other", "match": "other"}]}
        devboost.save_config(first)
        devboost.save_config(second)
        with open(devboost.CONFIG_FILE, "w", encoding="utf-8") as stream:
            stream.write("{corrupt")
        recovered = devboost.load_config()
        self.assertEqual(recovered["labels"], {"3030": "Grafana"})
        self.assertEqual(recovered["docker_labels"][0]["name"], "Work")

    def test_default_labels_mapping(self):
        self.assertEqual(devboost.DEFAULT_LABELS.get(3030), "Grafana Dashboard")
        self.assertEqual(devboost.DEFAULT_LABELS.get(11434), "Ollama LLM API")
        self.assertEqual(devboost.DEFAULT_LABELS.get(9090), "Prometheus Metrics")

    def test_default_state_is_outside_the_source_checkout(self):
        self.assertEqual(
            devboost._default_app_dir(),
            os.path.join(os.path.expanduser("~"), "Library", "Application Support", "DevBoost"),
        )

    def test_load_invalid_json_returns_complete_defaults(self):
        os.makedirs(self.temp_dir.name, exist_ok=True)
        with open(devboost.CONFIG_FILE, "w", encoding="utf-8") as stream:
            stream.write("{not json")
        cfg = devboost.load_config()
        for key in ("labels", "rules", "docker_labels", "history", "servers", "syncs", "usage_accounts"):
            self.assertIn(key, cfg)

    def test_legacy_config_is_migrated_only_from_app_directory(self):
        legacy = os.path.join(self.temp_dir.name, "legacy.json")
        with open(legacy, "w", encoding="utf-8") as stream:
            json.dump({"labels": {"3030": "Old"}}, stream)
        with patch.object(devboost, "APP_DIR", self.temp_dir.name), \
                patch.object(devboost, "LEGACY_CONFIG_FILES", [legacy]):
            cfg = devboost.load_config()
        self.assertEqual(cfg["labels"]["3030"], "Old")
        self.assertTrue(os.path.exists(devboost.CONFIG_FILE))

    def test_legacy_config_is_not_migrated_across_directories(self):
        with tempfile.TemporaryDirectory() as other:
            legacy = os.path.join(other, "legacy.json")
            with open(legacy, "w", encoding="utf-8") as stream:
                json.dump({"labels": {"3030": "Old"}}, stream)
            with patch.object(devboost, "APP_DIR", other), \
                    patch.object(devboost, "LEGACY_CONFIG_FILES", [legacy]):
                cfg = devboost.load_config()
        self.assertNotEqual(cfg["labels"].get("3030"), "Old")


if __name__ == "__main__":
    unittest.main()
