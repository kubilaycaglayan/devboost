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
        self.assertIn("acquireSingleInstanceLock()", self.swift)
        self.assertIn("flock(descriptor, LOCK_EX | LOCK_NB)", self.swift)
        self.assertNotIn('NSMenuItem(title: "AI Usage"', self.swift)
        self.assertNotIn("usageItem.submenu", self.swift)
        self.assertIn('menu.addItem(withTitle: "Open Dashboard"', self.swift)
        self.assertIn('menu.addItem(withTitle: "Quit DevBoost"', self.swift)

    def test_root_menu_contains_quota_balance_and_reset_details(self):
        for text in ("Balance:", "Resets in ", "You have \\(resetCount) usage limit"):
            self.assertIn(text, self.swift)
        self.assertIn('resetCount = max(0, (snapshot?["available_resets"] as? NSNumber)?.intValue ?? 0)', self.swift)
        self.assertNotIn('resetCount += 1', self.swift)
        self.assertIn('if provider.lowercased() == "codex", resetCount > 0', self.swift)
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
        self.assertIn('if force { queryItems.append(URLQueryItem(name: "refresh", value: "1")) }', self.swift)
        self.assertIn("func menuDidClose(_ menu: NSMenu)", self.swift)
        self.assertIn("usageMenuIsOpen", self.swift)
        self.assertIn("Syncing usage…", self.swift)
        self.assertIn("usageSyncStatus()", self.swift)
        self.assertIn("RunLoop.main.add(statusTimer, forMode: .common)", self.swift)
        self.assertIn('return "Synced \\(elapsed / 60)m ago"', self.swift)

    def test_menubar_usage_targets_the_configured_remote_server(self):
        self.assertIn('URL(string: "\\(baseURL)/api/servers/active")!', self.swift)
        self.assertIn('let webActiveID = root?["active_server_id"] as? String', self.swift)
        self.assertIn('URLQueryItem(name: "server", value: sourceID)', self.swift)
        self.assertIn('private func requestUsage(url: URL)', self.swift)

    def test_usage_request_authenticates_with_backend_session(self):
        self.assertIn("private let dashboardSessionToken = UUID().uuidString", self.swift)
        self.assertIn('request.setValue("devboost_session=\\(dashboardSessionToken)"', self.swift)
        self.assertIn('environment["DEVBOOST_SESSION_TOKEN"] = dashboardSessionToken', self.swift)

    def test_status_item_shows_compact_usage_title(self):
        self.assertIn("private func menuBarUsageTitle", self.swift)
        self.assertIn('case "codex": return "Cdx"', self.swift)
        self.assertIn('case "agy": return "Agy"', self.swift)
        self.assertIn('summaries.append("\\(label): \\(value)")', self.swift)
        self.assertIn('summaries.joined(separator: " | ")', self.swift)
        self.assertIn('min(100, max(0, percent))', self.swift)
        self.assertIn("setStatusItemTitle(self.menuBarUsageTitle", self.swift)
        self.assertNotIn("StatusTitleField", self.swift)
        self.assertIn('button.title = title', self.swift)
        self.assertIn("statusItem?.isVisible = menubarEnabled", self.swift)
        self.assertIn("button.alphaValue = 1", self.swift)
        self.assertIn("button.appearsDisabled = false", self.swift)
        self.assertIn("statusItem?.length = NSStatusItem.variableLength", self.swift)

    def test_status_item_supports_non_percentage_providers(self):
        self.assertIn('case "claude": return "Cl"', self.swift)
        self.assertIn('value = "U:\\(compactNumber(used))', self.swift)
        self.assertIn('value = "B:\\(compactNumber(remaining))', self.swift)
        self.assertIn('value = "B:∞"', self.swift)

    def test_percentage_quota_rows_use_compact_colored_bars(self):
        self.assertIn("private final class UsageBarView: NSView", self.swift)
        self.assertIn("item.view = UsageBarView(remainingPercent: remaining, title: title)", self.swift)
        self.assertIn("private var usageColor: NSColor", self.swift)
        self.assertIn("let used = 100 - remainingPercent", self.swift)
        self.assertIn("width: 220, height: 18", self.swift)
        self.assertIn("if !darkText { attributes[.shadow] = textShadow }", self.swift)
        self.assertNotIn("NSNull()", self.swift)

    def test_menubar_can_select_this_mac_or_active_remote_sources(self):
        self.assertIn('private static let selectedUsageSourceKey', self.swift)
        self.assertIn('UsageSource(id: "local", name: "This Mac")', self.swift)
        self.assertIn('URL(string: "\\(baseURL)/api/servers/active")!', self.swift)
        self.assertIn('private func addUsageSourceItems(to menu: NSMenu)', self.swift)
        self.assertIn('@objc private func selectUsageSource(_ sender: NSMenuItem)', self.swift)
        self.assertIn('URLQueryItem(name: "server", value: sourceID)', self.swift)

    def test_new_installation_selects_enabled_accounts_for_compact_title(self):
        self.assertIn("private var hasSavedUsageSelection", self.swift)
        self.assertIn("if !self.hasSavedUsageSelection", self.swift)
        self.assertIn("initialSelection = Set(accounts.compactMap", self.swift)
        self.assertIn("UserDefaults.standard.set(Array(initialSelection).sorted()", self.swift)

    def test_status_item_prefers_local_usage_for_stale_codex_fallback(self):
        self.assertIn('message.contains("Showing local usage")', self.swift)
        self.assertIn('snapshot["local_usage"] as? [String: Any]', self.swift)
        self.assertIn('value = "U:\\(compactNumber(used))', self.swift)

    def test_usage_accounts_are_checkable_and_persisted(self):
        self.assertIn("menu.autoenablesItems = false", self.swift)
        self.assertIn("selectedUsageAccountIDs", self.swift)
        self.assertIn("toggleUsageAccount", self.swift)
        self.assertIn("private final class MenuToggleButton: NSButton", self.swift)
        self.assertIn("private func usageAccountMenuItem(id: String, title: String, selected: Bool)", self.swift)
        self.assertIn("toggleUsageAccountButton", self.swift)
        self.assertIn("UserDefaults.standard.set(Array(selectedUsageAccountIDs).sorted()", self.swift)

    def test_interactive_menu_rows_use_embedded_controls_to_stay_open(self):
        self.assertIn("item.view = button", self.swift)
        self.assertIn("selectUsageSourceButton", self.swift)
        self.assertIn("toggleUsageAccountButton", self.swift)


if __name__ == "__main__":
    unittest.main()
