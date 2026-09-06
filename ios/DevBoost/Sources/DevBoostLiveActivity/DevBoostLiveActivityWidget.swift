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
                    Text("Resets in \(resetDuration(context.state.resetAt))")
                        .font(.subheadline.monospacedDigit())
                }
            } compactLeading: {
                Image(systemName: "bolt.fill")
            } compactTrailing: {
                Text("\(context.state.remainingPercent)%")
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
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Label(activityTitle, systemImage: "bolt.fill")
                    .font(.headline)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            Text("\(context.state.remainingPercent)%")
                .font(.system(size: 34, weight: .bold, design: .rounded).monospacedDigit())
                .frame(maxWidth: .infinity, alignment: .center)
                .contentTransition(.numericText())

            ProgressView(value: Double(context.state.remainingPercent), total: 100)
                .tint(context.state.remainingPercent > 25 ? .green : .orange)
                .frame(maxWidth: .infinity)

            Text("Resets in \(resetDuration(context.state.resetAt))")
                .frame(maxWidth: .infinity, alignment: .center)
                .font(.subheadline.monospacedDigit())
                .foregroundStyle(.secondary)
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
