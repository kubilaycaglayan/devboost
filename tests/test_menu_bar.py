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
        self.assertIn('return "\\(String(format: "%.0f", clamped))% left"', self.swift)
        self.assertIn('return name + (reset.map { " · \\($0)" } ?? "")', self.swift)
        self.assertIn('indentationLevel: 2', self.swift)
        self.assertIn("compactNumber(max(0, limit - used))", self.swift)
        self.assertIn("item.isEnabled = true", self.swift)

    def test_menu_bar_follows_quota_order_and_omits_empty_accounts(self):
        self.assertIn("let orderedAccounts = accounts.filter", self.swift)
        self.assertIn("self.snapshotHasData", self.swift)
        self.assertIn("No usage data available", self.swift)
        self.assertIn('let quotas = snapshot["quotas"] as? [[String: Any]] ?? []', self.swift)

    def test_opening_menu_forces_a_quota_refresh(self):
        self.assertIn("NSMenuDelegate", self.swift)
        self.assertIn("func menuWillOpen(_ menu: NSMenu)", self.swift)
        self.assertIn("refreshUsage(force: true)", self.swift)
        self.assertIn('force ? "?refresh=1" : ""', self.swift)

    def test_status_item_shows_compact_usage_title(self):
        self.assertIn("private func menuBarUsageTitle", self.swift)
        self.assertIn('case "codex": label = "Cdx"', self.swift)
        self.assertIn('case "agy": label = "Agy"', self.swift)
        self.assertIn('summaries.append("\\(label): L:', self.swift)
        self.assertIn('summaries.joined(separator: " | ")', self.swift)
        self.assertIn('min(100, max(0, percent))', self.swift)
        self.assertIn("self.statusItem?.button?.title = self.menuBarUsageTitle", self.swift)


if __name__ == "__main__":
    unittest.main()
