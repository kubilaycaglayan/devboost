import BackgroundTasks
import SwiftUI

@main
struct DevBoostApp: App {
    private static let usageRefreshIdentifier = "com.personal.devboost.usage-refresh"
    @StateObject private var store: AppStore
    @Environment(\.scenePhase) private var scenePhase

    init() {
        _store = StateObject(wrappedValue: AppStore(reset: ProcessInfo.processInfo.arguments.contains("--ui-test-reset-state")))
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
                .onAppear { scheduleUsageRefresh() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .background {
                scheduleUsageRefresh()
            }
        }
        .backgroundTask(.appRefresh(Self.usageRefreshIdentifier)) {
            await refreshUsageInBackground()
        }
    }

    private func scheduleUsageRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: Self.usageRefreshIdentifier)
        request.earliestBeginDate = UsageRefreshPolicy.nextBackgroundRefresh(after: .now)
        try? BGTaskScheduler.shared.submit(request)
    }

    @MainActor
    private func refreshUsageInBackground() async {
        defer { scheduleUsageRefresh() }
        guard let host = store.hosts.first(where: { $0.keyInstalled }) else { return }
        do {
            let usage = try await CodexUsageService(
                remote: RemoteCommandService(keychain: store.keychain)
            ).refresh(on: host)
            store.codexUsage = usage
            await LiveUsageActivity.sync(with: usage, hostName: host.name)
        } catch {
            // Keep the last known quota visible in the Live Activity. A
            // transient background network failure should not erase it.
        }
    }
}
