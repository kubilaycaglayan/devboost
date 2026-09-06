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
        self.assertIn("func menuDidClose(_ menu: NSMenu)", self.swift)
        self.assertIn("usageMenuIsOpen", self.swift)
        self.assertIn("Syncing usage…", self.swift)
        self.assertIn("usageSyncStatus()", self.swift)
        self.assertIn("RunLoop.main.add(statusTimer, forMode: .common)", self.swift)
        self.assertIn('return "Synced \\(elapsed / 60)m ago"', self.swift)

    def test_status_item_shows_compact_usage_title(self):
        self.assertIn("private func menuBarUsageTitle", self.swift)
        self.assertIn('case "codex": return "Cdx"', self.swift)
        self.assertIn('case "agy": return "Agy"', self.swift)
        self.assertIn('summaries.append("\\(label): \\(value)")', self.swift)
        self.assertIn('summaries.joined(separator: " | ")', self.swift)
        self.assertIn('min(100, max(0, percent))', self.swift)
        self.assertIn("setStatusItemTitle(self.menuBarUsageTitle", self.swift)
        self.assertIn("button.attributedTitle = NSAttributedString", self.swift)
        self.assertIn(".foregroundColor: NSColor.white", self.swift)
        self.assertIn("statusItem?.isVisible = true", self.swift)
        self.assertIn("button.alphaValue = 1", self.swift)

    def test_status_item_supports_non_percentage_providers(self):
        self.assertIn('case "claude": return "Cl"', self.swift)
        self.assertIn('value = "U:\\(compactNumber(used))', self.swift)
        self.assertIn('value = "B:\\(compactNumber(remaining))', self.swift)
        self.assertIn('value = "B:∞"', self.swift)

    def test_usage_accounts_are_checkable_and_persisted(self):
        self.assertIn("menu.autoenablesItems = false", self.swift)
        self.assertIn("selectedUsageAccountIDs", self.swift)
        self.assertIn("toggleUsageAccount", self.swift)
        self.assertIn("heading.state = self.selectedUsageAccountIDs.contains(aid) ? .on : .off", self.swift)
        self.assertIn("UserDefaults.standard.set(Array(selectedUsageAccountIDs).sorted()", self.swift)


if __name__ == "__main__":
    unittest.main()
