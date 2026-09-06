import ActivityKit
import Foundation

/// Keeps one Lock Screen activity in sync with the latest successful Codex refresh.
/// Live Activities are enabled by the user in iOS Settings; Apple provides no
/// in-app permission prompt for them, so a disabled setting is simply respected.
enum LiveUsageActivity {
    static func sync(with usage: CodexUsageSnapshot, hostName: String = "Codex") async {
        guard #available(iOS 16.2, *),
              ActivityAuthorizationInfo().areActivitiesEnabled,
              let resetAt = usage.primaryResetAt,
              let used = usage.primaryUsedPercent else { return }

        let state = DevBoostUsageActivityAttributes.ContentState(
            remainingPercent: Int(max(0, min(100, 100 - used)).rounded()),
            resetAt: resetAt
        )
        let content = ActivityContent(state: state, staleDate: resetAt)

        if let activity = Activity<DevBoostUsageActivityAttributes>.activities.first {
            await activity.update(content)
            return
        }

        do {
            _ = try Activity.request(
                attributes: DevBoostUsageActivityAttributes(hostName: hostName),
                content: content,
                pushType: nil
            )
        } catch {
            // The in-app usage screen remains available if iOS declines an activity.
        }
    }
}
