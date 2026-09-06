import SwiftUI

@main
struct DevBoostApp: App {
    @StateObject private var store: AppStore

    init() {
        _store = StateObject(wrappedValue: AppStore(reset: ProcessInfo.processInfo.arguments.contains("--ui-test-reset-state")))
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
        }
    }
}
