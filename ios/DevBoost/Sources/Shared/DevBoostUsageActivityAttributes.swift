import ActivityKit
import Foundation

struct DevBoostUsageActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        let remainingPercent: Int
        let resetAt: Date
    }

    let hostName: String
}
