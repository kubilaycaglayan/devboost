import ActivityKit
import Foundation

/// Keeps one Lock Screen activity in sync with the latest successful Codex refresh.
/// Live Activities are enabled by the user in iOS Settings; Apple provides no
/// in-app permission prompt for them, so a disabled setting is simply respected.
enum LiveUsageActivity {
    private static let pushTokenKey = "devboost.live-activity-push-token"

    static var areActivitiesEnabled: Bool {
        guard #available(iOS 16.2, *) else { return false }
        return ActivityAuthorizationInfo().areActivitiesEnabled
    }

    static func sync(with usage: CodexUsageSnapshot, hostName: String = "Codex") async {
        guard #available(iOS 16.2, *),
              areActivitiesEnabled,
              let resetAt = usage.primaryResetAt,
              let used = usage.primaryUsedPercent else { return }

        let state = DevBoostUsageActivityAttributes.ContentState(
            remainingPercent: Int(max(0, min(100, 100 - used)).rounded()),
            resetAt: resetAt
        )
        let content = ActivityContent(state: state, staleDate: resetAt)

        if let activity = Activity<DevBoostUsageActivityAttributes>.activities.first {
            retainPushToken(for: activity)
            await activity.update(content)
            return
        }

        do {
            let activity = try Activity.request(
                attributes: DevBoostUsageActivityAttributes(hostName: hostName),
                content: content,
                // Personal Teams cannot enable Push Notifications. Keep the
                // local activity usable there; paid-team builds can enable
                // DEVBOOST_APNS_ENABLED for remote updates.
                pushType: {
#if DEVBOOST_APNS_ENABLED
                    .token
#else
                    nil
#endif
                }()
            )
            retainPushToken(for: activity)
        } catch {
            // The in-app usage screen remains available if iOS declines an activity.
        }
    }

    /// The token is intentionally only retained locally. A future APNs relay
    /// can read it through the app's authenticated pairing flow and send
    /// signed Live Activity updates while DevBoost is suspended.
    private static func retainPushToken(
        for activity: Activity<DevBoostUsageActivityAttributes>
    ) {
        if let token = activity.pushToken {
            UserDefaults.standard.set(token.base64EncodedString(), forKey: pushTokenKey)
        }
    }
}
