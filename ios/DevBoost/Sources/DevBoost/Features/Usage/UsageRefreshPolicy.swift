import Foundation

/// Cadences used by the foreground usage view and iOS Background App Refresh.
/// iOS may defer background work, but scheduling at this interval lets the
/// system refresh the Live Activity without requiring the user to reopen the app.
enum UsageRefreshPolicy {
    static let foregroundInterval: TimeInterval = 30
    static let backgroundInterval: TimeInterval = 15 * 60

    static func nextBackgroundRefresh(after date: Date) -> Date {
        date.addingTimeInterval(backgroundInterval)
    }
}
