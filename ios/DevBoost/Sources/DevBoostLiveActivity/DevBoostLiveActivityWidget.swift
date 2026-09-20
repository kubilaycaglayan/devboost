import ActivityKit
import Foundation
import SwiftUI
import WidgetKit

@main
struct DevBoostLiveActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: DevBoostUsageActivityAttributes.self) { context in
            LockScreenUsageView(context: context)
                .activityBackgroundTint(.black.opacity(0.9))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label("DevBoost", systemImage: "bolt.fill")
                        .font(.headline)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text("\(context.state.remainingPercent)%")
                        .font(.headline.monospacedDigit())
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack(spacing: 12) {
                        IslandProviderSummary(
                            name: "Codex",
                            remainingPercent: context.state.remainingPercent,
                            resetAt: context.state.resetAt
                        )
                        if let remaining = context.state.claudeRemainingPercent {
                            IslandProviderSummary(
                                name: "Claude",
                                remainingPercent: remaining,
                                resetAt: context.state.claudeResetAt
                            )
                        }
                    }
                }
            } compactLeading: {
                Image(systemName: "bolt.fill")
            } compactTrailing: {
                Text(compactPercentages(for: context.state))
                    .monospacedDigit()
            } minimal: {
                Image(systemName: "bolt.fill")
            }
        }
    }

}

private func resetDuration(_ resetAt: Date, now: Date = .now) -> String {
    let minutes = max(0, Int(ceil(resetAt.timeIntervalSince(now) / 60)))
    if minutes == 0 { return "<1m" }

    let hours = minutes / 60
    let remainingMinutes = minutes % 60
    if hours > 0 {
        return "\(hours)h\(remainingMinutes > 0 ? " \(remainingMinutes)m" : "")"
    }
    return "\(remainingMinutes)m"
}

private struct LockScreenUsageView: View {
    let context: ActivityViewContext<DevBoostUsageActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Label(activityTitle, systemImage: "bolt.fill")
                    .font(.headline)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            HStack(alignment: .top, spacing: 10) {
                ProviderUsageView(
                    name: "Codex",
                    remainingPercent: context.state.remainingPercent,
                    resetAt: context.state.resetAt
                )
                if let remaining = context.state.claudeRemainingPercent {
                    ProviderUsageView(
                        name: "Claude",
                        remainingPercent: remaining,
                        resetAt: context.state.claudeResetAt
                    )
                }
            }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private var activityTitle: String {
        let hostName = context.attributes.hostName.trimmingCharacters(in: .whitespacesAndNewlines)
        return hostName.isEmpty ? "DevBoost" : "DevBoost · \(hostName)"
    }
}

private struct ProviderUsageView: View {
    let name: String
    let remainingPercent: Int
    let resetAt: Date?

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(name)
                .font(.caption.weight(.bold))
                .foregroundStyle(.white)
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(.white.opacity(0.16), in: Capsule())
                .overlay { Capsule().stroke(.white.opacity(0.24), lineWidth: 0.5) }
                .lineLimit(1)

            Text("\(remainingPercent)%")
                .font(.system(size: 25, weight: .bold, design: .rounded).monospacedDigit())
                .contentTransition(.numericText())

            ProgressView(value: Double(remainingPercent), total: 100)
                .tint(remainingPercent > 25 ? .green : .orange)

            if let resetAt {
                Text("\(resetDuration(resetAt))")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct IslandProviderSummary: View {
    let name: String
    let remainingPercent: Int
    let resetAt: Date?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(name)
                .font(.caption.weight(.bold))
                .foregroundStyle(.white)
                .padding(.horizontal, 5)
                .padding(.vertical, 2)
                .background(.white.opacity(0.16), in: Capsule())
            Text("\(remainingPercent)% left")
                .font(.caption2.monospacedDigit())
            if let resetAt {
                Text(resetDuration(resetAt))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private func compactPercentages(for state: DevBoostUsageActivityAttributes.ContentState) -> String {
    if let claude = state.claudeRemainingPercent {
        return "C\(state.remainingPercent) A\(claude)"
    }
    return "\(state.remainingPercent)%"
}
