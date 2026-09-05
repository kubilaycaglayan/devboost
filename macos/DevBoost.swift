import Cocoa
import Darwin

private let appState = NSString(string: "~/Library/Application Support/DevBoost").expandingTildeInPath
private let logState = (appState as NSString).appendingPathComponent("logs")

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

final class DevBoostApp: NSObject, NSApplicationDelegate {
    private var backend: Process?
    private var statusItem: NSStatusItem?
    private var usageMenu: NSMenu?
    private var usageTimer: Timer?
    private var interruptSource: DispatchSourceSignal?

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
    }

    func applicationWillTerminate(_ notification: Notification) {
        usageTimer?.invalidate()
        backend?.terminate()
    }

    private func setupMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "DevBoost"
        let menu = NSMenu()
        menu.addItem(withTitle: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "o")
        let usageItem = NSMenuItem(title: "AI Usage", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        submenu.addItem(withTitle: "Loading…", action: nil, keyEquivalent: "")
        usageItem.submenu = submenu
        menu.addItem(usageItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit DevBoost", action: #selector(quit), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
        usageMenu = submenu
    }

    private func refreshUsage() {
        let url = URL(string: "http://127.0.0.1:\(dashboardPort())/api/usage")!
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let data = data,
                  let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let accounts = root["accounts"] as? [[String: Any]],
                  let snapshots = root["snapshots"] as? [String: Any] else { return }
            DispatchQueue.main.async {
                guard let menu = self?.usageMenu else { return }
                menu.removeAllItems()
                let enabledAccounts = accounts.filter { ($0["enabled"] as? Bool) ?? true }
                if enabledAccounts.isEmpty {
                    menu.addItem(withTitle: "No accounts configured", action: nil, keyEquivalent: "")
                    return
                }
                for account in enabledAccounts {
                    let aid = account["id"] as? String ?? ""
                    let name = account["name"] as? String ?? (account["provider"] as? String ?? "Account")
                    let snapshot = snapshots[aid] as? [String: Any]
                    let title: String
                    if snapshot?["ok"] as? Bool == true {
                        let quotas = snapshot?["quotas"] as? [[String: Any]] ?? []
                        let remaining = quotas.first?["remaining"] as? NSNumber
                        let balances = snapshot?["balances"] as? [[String: Any]] ?? []
                        let balance = balances.first?["remaining"] as? NSNumber
                        if let remaining = remaining, let balance = balance {
                            title = "\(name): \(remaining) left · \(balance) \(balances.first?["currency"] as? String ?? "USD")"
                        } else if let remaining = remaining {
                            title = "\(name): \(remaining) left"
                        } else if let balance = balance {
                            title = "\(name): \(balance) \(balances.first?["currency"] as? String ?? "USD")"
                        } else if let spent = balances.first?["spent"] as? NSNumber {
                            title = "\(name): spent \(spent) \(balances.first?["currency"] as? String ?? "USD")"
                        } else {
                            title = "\(name): connected"
                        }
                    } else {
                        title = "\(name): unavailable"
                    }
                    menu.addItem(withTitle: title, action: nil, keyEquivalent: "")
                }
            }
        }.resume()
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
