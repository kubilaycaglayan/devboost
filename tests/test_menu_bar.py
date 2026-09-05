import os
import unittest


class TestMenuBarUsage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "macos", "DevBoost.swift"))
        with open(path, encoding="utf-8") as source:
            cls.swift = source.read()

    def test_usage_is_flattened_into_root_menu(self):
        self.assertIn("private var statusMenu: NSMenu?", self.swift)
        self.assertNotIn('NSMenuItem(title: "AI Usage"', self.swift)
        self.assertNotIn("usageItem.submenu", self.swift)
        self.assertIn('menu.addItem(withTitle: "Open Dashboard"', self.swift)
        self.assertIn('menu.addItem(withTitle: "Quit DevBoost"', self.swift)

    def test_root_menu_contains_quota_balance_and_reset_details(self):
        for text in ("Balance:", "Resets ", "You have \\(resetCount) usage limit"):
            self.assertIn(text, self.swift)
        self.assertIn("addIndentedItem", self.swift)


if __name__ == "__main__":
    unittest.main()
