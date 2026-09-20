import ActivityKit
import Foundation

struct DevBoostUsageActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        let remainingPercent: Int
        let resetAt: Date
        let claudeRemainingPercent: Int?
        let claudeResetAt: Date?
    }

    let hostName: String
}
