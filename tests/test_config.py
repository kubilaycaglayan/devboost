import os
import json
import tempfile
import unittest
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asus_ports


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.orig_config_dir = asus_ports.CONFIG_DIR
        self.orig_config_file = asus_ports.CONFIG_FILE
        asus_ports.CONFIG_DIR = self.temp_dir.name
        asus_ports.CONFIG_FILE = os.path.join(self.temp_dir.name, "config.json")

    def tearDown(self):
        asus_ports.CONFIG_DIR = self.orig_config_dir
        asus_ports.CONFIG_FILE = self.orig_config_file
        self.temp_dir.cleanup()

    def test_load_default_config_when_missing(self):
        cfg = asus_ports.load_config()
        self.assertIn("labels", cfg)
        self.assertIn("rules", cfg)

    def test_save_and_load_config(self):
        data = {
            "labels": {"3030": "My Custom Grafana", "8080": "Custom App"},
            "rules": {"3030": {"always": True}}
        }
        asus_ports.save_config(data)

        loaded = asus_ports.load_config()
        self.assertEqual(loaded["labels"]["3030"], "My Custom Grafana")
        self.assertEqual(loaded["labels"]["8080"], "Custom App")
        self.assertTrue(loaded["rules"]["3030"]["always"])

    def test_default_labels_mapping(self):
        self.assertEqual(asus_ports.DEFAULT_LABELS.get(3030), "Grafana Dashboard")
        self.assertEqual(asus_ports.DEFAULT_LABELS.get(11434), "Ollama LLM API")
        self.assertEqual(asus_ports.DEFAULT_LABELS.get(9090), "Prometheus Metrics")


if __name__ == "__main__":
    unittest.main()
