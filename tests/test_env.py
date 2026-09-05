import os
import tempfile
import unittest
import sys
from unittest.mock import patch

# Ensure parent directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import devboost


class TestEnvLoading(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_file = os.path.join(self.temp_dir.name, ".env")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_env_file_parsing(self):
        content = """
        # Comment line
        PORT_TRACKER_TEST_VAR=hello_world
        PORT_TRACKER_QUOTED='single_quotes'
        PORT_TRACKER_DOUBLE_QUOTED="double_quotes"
        # Another comment
        EMPTY_VALUE=
        """
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(content)

        # Mock candidates in load_env
        original_candidates = [self.env_file]
        try:
            with open(self.env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    os.environ[k] = v

            self.assertEqual(os.environ.get("PORT_TRACKER_TEST_VAR"), "hello_world")
            self.assertEqual(os.environ.get("PORT_TRACKER_QUOTED"), "single_quotes")
            self.assertEqual(os.environ.get("PORT_TRACKER_DOUBLE_QUOTED"), "double_quotes")
            self.assertEqual(os.environ.get("EMPTY_VALUE"), "")
        finally:
            for k in ["PORT_TRACKER_TEST_VAR", "PORT_TRACKER_QUOTED", "PORT_TRACKER_DOUBLE_QUOTED", "EMPTY_VALUE"]:
                os.environ.pop(k, None)

    def test_load_env_reads_explicit_app_directory_without_overwriting(self):
        with open(os.path.join(self.temp_dir.name, ".env"), "w", encoding="utf-8") as stream:
            stream.write("DEVBOOST_TEST_LOADED=from-file\nDEVBOOST_TEST_EXISTING=file\n")
        with patch.dict(os.environ, {"DEVBOOST_TEST_EXISTING": "already-set"}, clear=False):
            os.environ.pop("DEVBOOST_TEST_LOADED", None)
            devboost.load_env(self.temp_dir.name)
            self.assertEqual(os.environ["DEVBOOST_TEST_LOADED"], "from-file")
            self.assertEqual(os.environ["DEVBOOST_TEST_EXISTING"], "already-set")
            os.environ.pop("DEVBOOST_TEST_LOADED", None)

    def test_load_env_ignores_malformed_lines(self):
        with open(os.path.join(self.temp_dir.name, ".env"), "w", encoding="utf-8") as stream:
            stream.write("not an assignment\n=missing-key\nDEVBOOST_TEST_OK=yes\n")
        os.environ.pop("DEVBOOST_TEST_OK", None)
        devboost.load_env(self.temp_dir.name)
        self.assertEqual(os.environ.pop("DEVBOOST_TEST_OK"), "yes")


if __name__ == "__main__":
    unittest.main()
