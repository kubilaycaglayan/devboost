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
    private var interruptSource: DispatchSourceSignal?

    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGINT, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        source.setEventHandler { NSApp.terminate(nil) }
        source.resume()
        interruptSource = source
        setupMenu()
        runCommand(pythonArguments: ["serve"] + Array(CommandLine.arguments.dropFirst()))
    }

    func applicationWillTerminate(_ notification: Notification) {
        backend?.terminate()
    }

    private func setupMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "DevBoost"
        let menu = NSMenu()
        menu.addItem(withTitle: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "o")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit DevBoost", action: #selector(quit), keyEquivalent: "q")
        item.menu = menu
        statusItem = item
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
