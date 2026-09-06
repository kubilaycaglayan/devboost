import Cocoa
import Darwin

private let appState = NSString(string: "~/Library/Application Support/DevBoost").expandingTildeInPath
private let logState = (appState as NSString).appendingPathComponent("logs")

private final class StatusTitleField: NSTextField {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

@main
enum DevBoostMain {
    static func main() {
        let args = Array(CommandLine.arguments.dropFirst())
        if args.first == "sync-run" || args.first == "refresh-agents" {
            exit(runWorker(executable: "/usr/bin/python3", arguments: ["-u", backendScript()] + args))
        }
        if args.first == "--tunnel" {
            exit(runWorker(executable: "/usr/bin/ssh", arguments: Array(args.dropFirst())))
        }

        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let delegate = DevBoostApp()
        app.delegate = delegate
        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}

final class DevBoostApp: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private static let selectedUsageAccountsKey = "devboost-menubar-usage-selection"
    private var backend: Process?
    private var statusItem: NSStatusItem?
    private var statusTitleField: StatusTitleField?
    private var statusMenu: NSMenu?
    private var usageStatusMenuItem: NSMenuItem?
    private var usageTimer: Timer?
    private var usageStatusTimer: Timer?
    private var interruptSource: DispatchSourceSignal?
    private var usageMenuNeedsRebuild = false
    private var usageMenuIsOpen = false
    private var lastUsageSyncAt: Date?
    private var selectedUsageAccountIDs: Set<String> = Set(
        UserDefaults.standard.stringArray(forKey: DevBoostApp.selectedUsageAccountsKey) ?? []
    )

    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGINT, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        source.setEventHandler { NSApp.terminate(nil) }
        source.resume()
        interruptSource = source
        setupMenu()
        // Start the backend before the first request; the initial menu setup
        // previously raced the HTTP listener and could leave “Loading…” for a
        // full refresh interval.
        runCommand(pythonArguments: ["serve"] + Array(CommandLine.arguments.dropFirst()))
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in self?.refreshUsage() }
        usageTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.refreshUsage() }
        let statusTimer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.updateUsageMenuStatus() }
        RunLoop.main.add(statusTimer, forMode: .common)
        usageStatusTimer = statusTimer
    }

    func applicationWillTerminate(_ notification: Notification) {
        usageTimer?.invalidate()
        usageStatusTimer?.invalidate()
        backend?.terminate()
    }

    private func setupMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item
        item.isVisible = true
        if let button = item.button {
            button.title = ""
            button.alternateTitle = ""
            button.isHidden = false
            button.isEnabled = true
            let titleField = StatusTitleField(frame: .zero)
            titleField.isBezeled = false
            titleField.isEditable = false
            titleField.isSelectable = false
            titleField.drawsBackground = false
            titleField.alignment = .center
            titleField.font = NSFont.menuBarFont(ofSize: 0)
            titleField.textColor = .labelColor
            titleField.alphaValue = 1
            titleField.translatesAutoresizingMaskIntoConstraints = false
            button.addSubview(titleField)
            NSLayoutConstraint.activate([
                titleField.centerXAnchor.constraint(equalTo: button.centerXAnchor),
                titleField.centerYAnchor.constraint(equalTo: button.centerYAnchor)
            ])
            statusTitleField = titleField
        }
        setStatusItemTitle("DevBoost")
        let menu = NSMenu()
        // Every row's enabled state is managed explicitly. AppKit's automatic
        // action validation can otherwise gray rows while an open menu refreshes.
        menu.autoenablesItems = false
        menu.addItem(withTitle: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "o")
        menu.addItem(NSMenuItem.separator())
        let usageStatus = NSMenuItem(title: "Syncing usage…", action: nil, keyEquivalent: "")
        usageStatus.isEnabled = true
        usageStatusMenuItem = usageStatus
        menu.addItem(usageStatus)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit DevBoost", action: #selector(quit), keyEquivalent: "q")
        item.menu = menu
        statusMenu = menu
        menu.delegate = self
    }

    func menuWillOpen(_ menu: NSMenu) {
        // Opening the status menu is an explicit request to see current
        // quotas, so do not wait for the background one-minute refresh.
        usageMenuIsOpen = true
        setUsageMenuStatus("Syncing usage…")
        refreshUsage(force: true)
    }

    func menuDidClose(_ menu: NSMenu) {
        usageMenuIsOpen = false
        guard usageMenuNeedsRebuild else { return }
        usageMenuNeedsRebuild = false
        // Apply the latest provider rows only after the menu is closed. This
        // avoids replacing visible NSMenuItems while AppKit is tracking them.
        refreshUsage()
    }

    private func refreshUsage(force: Bool = false) {
        let refreshQuery = force ? "?refresh=1" : ""
        let url = URL(string: "http://127.0.0.1:\(dashboardPort())/api/usage\(refreshQuery)")!
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data = data,
                  let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let accounts = root["accounts"] as? [[String: Any]],
                  let snapshots = root["snapshots"] as? [String: Any] else { return }
            DispatchQueue.main.async {
                guard let self = self, let menu = self.statusMenu else { return }
                self.lastUsageSyncAt = Date()
                self.setUsageMenuStatus(self.usageSyncStatus())
                self.setStatusItemTitle(self.menuBarUsageTitle(accounts: accounts, snapshots: snapshots))
                if self.usageMenuIsOpen {
                    self.usageMenuNeedsRebuild = true
                    return
                }
                menu.removeAllItems()
                menu.addItem(withTitle: "Open Dashboard", action: #selector(DevBoostApp.openDashboard), keyEquivalent: "o")
                menu.addItem(NSMenuItem.separator())
                let usageStatus = NSMenuItem(title: self.usageSyncStatus(), action: nil, keyEquivalent: "")
                usageStatus.isEnabled = true
                self.usageStatusMenuItem = usageStatus
                menu.addItem(usageStatus)
                menu.addItem(NSMenuItem.separator())
                // The backend returns accounts in the persisted quotas-page
                // order. Keep that order here so dragging a row also moves
                // its menu-bar section.
                let orderedAccounts = accounts.filter { account in
                    ((account["enabled"] as? Bool) ?? true) && self.snapshotHasData(snapshots[account["id"] as? String ?? ""] as? [String: Any])
                }
                if orderedAccounts.isEmpty {
                    menu.addItem(withTitle: "No usage data available", action: nil, keyEquivalent: "")
                } else {
                    for account in orderedAccounts {
                        let aid = account["id"] as? String ?? ""
                        let provider = account["provider"] as? String ?? "Provider"
                        let name = account["name"] as? String ?? provider
                        let snapshot = snapshots[aid] as? [String: Any]
                        let heading = NSMenuItem(title: "\(provider) · \(name)", action: #selector(DevBoostApp.toggleUsageAccount(_:)), keyEquivalent: "")
                        heading.target = self
                        heading.representedObject = aid
                        heading.state = self.selectedUsageAccountIDs.contains(aid) ? .on : .off
                        menu.addItem(heading)
                        if self.snapshotHasData(snapshot) {
                            let quotas = snapshot?["quotas"] as? [[String: Any]] ?? []
                            var resetCount = 0
                            for quota in quotas {
                                let reset = self.formatResetTime(quota["reset_at"])
                                self.addIndentedItem(to: menu, title: self.quotaMenuDetail(quota, reset: reset))
                                if let value = self.quotaMenuValue(quota) {
                                    self.addIndentedItem(to: menu, title: value, indentationLevel: 2)
                                }
                                if reset != nil {
                                    resetCount += 1
                                }
                            }
                            if resetCount > 0 {
                                let noun = resetCount == 1 ? "reset" : "resets"
                                self.addIndentedItem(to: menu, title: "You have \(resetCount) usage limit \(noun) available.")
                            }
                            if snapshot?["credits_unlimited"] as? Bool == true {
                                self.addIndentedItem(to: menu, title: "Credits: unlimited")
                            }
                            let balances = snapshot?["balances"] as? [[String: Any]] ?? []
                            for balance in balances {
                                let currency = balance["currency"] as? String ?? "USD"
                                if let remaining = self.numberText(balance["remaining"]) {
                                    self.addIndentedItem(to: menu, title: "Balance: \(remaining) \(currency) remaining")
                                } else if let spent = self.numberText(balance["spent"]) {
                                    self.addIndentedItem(to: menu, title: "Balance: \(spent) \(currency) spent")
                                }
                            }
                        } else {
                            let message = snapshot?["message"] as? String ?? "Unavailable"
                            self.addIndentedItem(to: menu, title: message)
                        }
                        menu.addItem(NSMenuItem.separator())
                    }
                }
                menu.addItem(withTitle: "Quit DevBoost", action: #selector(DevBoostApp.quit), keyEquivalent: "q")
            }
        }.resume()
    }

    @objc private func toggleUsageAccount(_ sender: NSMenuItem) {
        guard let accountID = sender.representedObject as? String, !accountID.isEmpty else { return }
        if selectedUsageAccountIDs.contains(accountID) {
            selectedUsageAccountIDs.remove(accountID)
        } else {
            selectedUsageAccountIDs.insert(accountID)
        }
        UserDefaults.standard.set(Array(selectedUsageAccountIDs).sorted(), forKey: DevBoostApp.selectedUsageAccountsKey)
        sender.state = selectedUsageAccountIDs.contains(accountID) ? .on : .off
        refreshUsage()
    }

    private func setStatusItemTitle(_ title: String) {
        statusItem?.isVisible = true
        guard let button = statusItem?.button else { return }
        button.isHidden = false
        button.isEnabled = true
        button.appearsDisabled = false
        button.alphaValue = 1
        button.title = ""
        button.alternateTitle = ""
        statusTitleField?.stringValue = title
        statusTitleField?.textColor = .labelColor
        statusTitleField?.alphaValue = 1
        if let width = statusTitleField?.intrinsicContentSize.width {
            statusItem?.length = ceil(width) + 12
        }
    }

    private func setUsageMenuStatus(_ title: String) {
        usageStatusMenuItem?.title = title
    }

    private func updateUsageMenuStatus() {
        guard usageStatusMenuItem != nil else { return }
        setUsageMenuStatus(lastUsageSyncAt.map { _ in usageSyncStatus() } ?? "Syncing usage…")
    }

    private func usageSyncStatus() -> String {
        guard let syncedAt = lastUsageSyncAt else { return "Syncing usage…" }
        let elapsed = max(0, Int(Date().timeIntervalSince(syncedAt)))
        if elapsed < 60 { return "Synced \(elapsed)s ago" }
        return "Synced \(elapsed / 60)m ago"
    }

    private func addIndentedItem(to menu: NSMenu, title: String, indentationLevel: Int = 1) {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.indentationLevel = indentationLevel
        // These entries are informational, but should use the normal menu
        // text color rather than the disabled gray used for section headings.
        item.isEnabled = true
        menu.addItem(item)
    }

    private func snapshotHasData(_ snapshot: [String: Any]?) -> Bool {
        guard let snapshot = snapshot else { return false }
        if (snapshot["credits_unlimited"] as? Bool) == true { return true }
        let quotas = snapshot["quotas"] as? [[String: Any]] ?? []
        if quotas.contains(where: { quota in
            ["used", "remaining", "limit"].contains { numberText(quota[$0]) != nil }
        }) { return true }
        let balances = snapshot["balances"] as? [[String: Any]] ?? []
        return balances.contains { numberText($0["remaining"]) != nil || numberText($0["spent"]) != nil }
    }

    private func menuBarUsageTitle(accounts: [[String: Any]], snapshots: [String: Any]) -> String {
        var summaries: [String] = []
        for account in accounts where (account["enabled"] as? Bool) ?? true {
            let aid = account["id"] as? String ?? ""
            guard selectedUsageAccountIDs.contains(aid) else { continue }
            guard let snapshot = snapshots[aid] as? [String: Any] else { continue }
            let label = menuBarProviderLabel(account["provider"] as? String ?? "Provider")
            let quotas = snapshot["quotas"] as? [[String: Any]] ?? []
            var value: String?
            for quota in quotas {
                if let percent = quotaRemainingPercent(quota) {
                    value = "L:\(String(format: "%.0f", min(100, max(0, percent))))%"
                } else if let remaining = (quota["remaining"] as? NSNumber)?.doubleValue {
                    value = "L:\(compactNumber(remaining)) \(quota["unit"] as? String ?? "units")"
                } else if let used = (quota["used"] as? NSNumber)?.doubleValue {
                    value = "U:\(compactNumber(used)) \(quota["unit"] as? String ?? "units")"
                }
                if value != nil { break }
            }
            if value == nil, snapshot["credits_unlimited"] as? Bool == true {
                value = "B:∞"
            }
            if value == nil {
                let balances = snapshot["balances"] as? [[String: Any]] ?? []
                if let balance = balances.first {
                    let currency = balance["currency"] as? String ?? "USD"
                    if let remaining = (balance["remaining"] as? NSNumber)?.doubleValue {
                        value = "B:\(compactNumber(remaining)) \(currency)"
                    } else if let spent = (balance["spent"] as? NSNumber)?.doubleValue {
                        value = "U:\(compactNumber(spent)) \(currency)"
                    }
                }
            }
            if let value = value { summaries.append("\(label): \(value)") }
        }
        return summaries.isEmpty ? "DevBoost" : summaries.joined(separator: " | ")
    }

    private func menuBarProviderLabel(_ provider: String) -> String {
        switch provider.lowercased() {
        case "codex": return "Cdx"
        case "agy": return "Agy"
        case "claude": return "Cl"
        case "opencode": return "OC"
        default: return provider.prefix(1).uppercased() + provider.dropFirst().prefix(2)
        }
    }

    private func numberText(_ value: Any?) -> String? {
        guard let number = value as? NSNumber else { return nil }
        let raw = number.doubleValue
        if raw.rounded() == raw {
            return String(Int(raw))
        }
        return String(format: "%.2f", raw)
    }

    private func quotaMenuDetail(_ quota: [String: Any], reset: String? = nil) -> String {
        let name = quota["name"] as? String ?? "Usage limit"
        return name + (reset.map { " · \($0)" } ?? "")
    }

    private func quotaMenuValue(_ quota: [String: Any]) -> String? {
        let unit = quota["unit"] as? String ?? "units"
        let remaining = (quota["remaining"] as? NSNumber)?.doubleValue
        let used = (quota["used"] as? NSNumber)?.doubleValue
        let limit = (quota["limit"] as? NSNumber)?.doubleValue
        let percent = quotaRemainingPercent(quota)
        if let percent = percent {
            let clamped = min(100, max(0, percent))
            return "\(String(format: "%.0f", clamped))% left"
        }
        if let remaining = remaining {
            return "\(compactNumber(remaining)) \(unit) left"
        }
        if let used = used, let limit = limit {
            return "\(compactNumber(max(0, limit - used))) \(unit) left"
        }
        if let used = used {
            return "\(compactNumber(used)) \(unit)"
        }
        return nil
    }

    private func quotaRemainingPercent(_ quota: [String: Any]) -> Double? {
        let unit = quota["unit"] as? String ?? "units"
        let remaining = (quota["remaining"] as? NSNumber)?.doubleValue
        let used = (quota["used"] as? NSNumber)?.doubleValue
        let limit = (quota["limit"] as? NSNumber)?.doubleValue
        if unit == "%", let remaining = remaining {
            return remaining
        }
        if let limit = limit, limit > 0, let remaining = remaining {
            return remaining / limit * 100
        }
        if let limit = limit, limit > 0, let used = used {
            return (limit - used) / limit * 100
        }
        return nil
    }

    private func compactNumber(_ value: Double) -> String {
        let magnitude = abs(value)
        let scale: (Double, String) = magnitude >= 1_000_000_000 ? (1_000_000_000, "B")
            : magnitude >= 1_000_000 ? (1_000_000, "M")
            : magnitude >= 1_000 ? (1_000, "K") : (1, "")
        let scaled = value / scale.0
        let precision = scale.0 == 1 || abs(scaled) >= 100 ? 0 : 1
        let text = String(format: "%.*f", precision, scaled)
        return text.hasSuffix(".0") ? String(text.dropLast(2)) + scale.1 : text + scale.1
    }

    private func formatResetTime(_ value: Any?) -> String? {
        let date: Date?
        if let number = value as? NSNumber {
            let seconds = number.doubleValue
            date = Date(timeIntervalSince1970: seconds < 1_000_000_000_000 ? seconds : seconds / 1000)
        } else if let string = value as? String {
            let parser = ISO8601DateFormatter()
            date = parser.date(from: string)
        } else {
            date = nil
        }
        guard let date = date else { return nil }
        let minutes = Int(ceil(date.timeIntervalSinceNow / 60))
        guard minutes > 0 else { return "Resets now" }
        if minutes < 60 { return "Resets in \(minutes)m" }
        let hours = minutes / 60
        let remainingMinutes = minutes % 60
        if hours < 24 {
            return "Resets in \(hours)h\(remainingMinutes > 0 ? "\(remainingMinutes)m" : "")"
        }
        let days = hours / 24
        let remainingHours = hours % 24
        if days < 7 {
            return "Resets in \(days)d\(remainingHours > 0 ? "\(remainingHours)h" : "")"
        }
        let weeks = days / 7
        let remainingDays = days % 7
        return "Resets in \(weeks)w\(remainingDays > 0 ? "\(remainingDays)d" : "")"
    }

    @objc private func openDashboard() {
        let port = dashboardPort()
        NSWorkspace.shared.open(URL(string: "http://127.0.0.1:\(port)")!)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func dashboardPort() -> String {
        let args = Array(CommandLine.arguments.dropFirst())
        for (index, arg) in args.enumerated() where (arg == "--port" || arg == "-p") && index + 1 < args.count {
            return args[index + 1]
        }
        return ProcessInfo.processInfo.environment["DEVBOOST_DASHBOARD_PORT"] ?? "3080"
    }

    private func runCommand(pythonArguments: [String]) {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = ["-u", backendScript()] + pythonArguments
        task.environment = runtimeEnvironment()
        task.standardOutput = FileHandle.standardOutput
        task.standardError = FileHandle.standardError
        task.terminationHandler = { _ in
            if pythonArguments.first == "serve" {
                DispatchQueue.main.async { NSApp.terminate(nil) }
            }
        }
        backend = task
        do {
            try task.run()
        } catch {
            fputs("DevBoost could not start its backend: \(error)\n", stderr)
            NSApp.terminate(nil)
        }
    }

}

private func backendScript() -> String {
    guard let script = Bundle.main.path(forResource: "devboost", ofType: "py") else {
        fatalError("DevBoost backend is missing from the app bundle")
    }
    return script
}

private func runtimeEnvironment() -> [String: String] {
    var environment = ProcessInfo.processInfo.environment
    environment["DEVBOOST_APP_DIR"] = appState
    environment["DEVBOOST_LOG_DIR"] = logState
    return environment
}

private func runWorker(executable: String, arguments: [String]) -> Int32 {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: executable)
    task.arguments = arguments
    task.environment = runtimeEnvironment()
    task.standardOutput = FileHandle.standardOutput
    task.standardError = FileHandle.standardError
    do {
        try task.run()
        task.waitUntilExit()
        return task.terminationStatus
    } catch {
        fputs("DevBoost worker could not start: \(error)\n", stderr)
        return 1
    }
}
