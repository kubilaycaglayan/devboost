import ActivityKit
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
                    resetCountdown(context.state.resetAt)
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

    @ViewBuilder
    private func resetCountdown(_ resetAt: Date) -> some View {
        Text("Resets in ") + Text(resetAt, style: .timer)
    }
}

private struct LockScreenUsageView: View {
    let context: ActivityViewContext<DevBoostUsageActivityAttributes>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("DevBoost", systemImage: "bolt.fill")
                    .font(.headline)
                    .lineLimit(1)
                Spacer()
                Text("\(context.state.remainingPercent)% left")
                    .font(.headline.monospacedDigit())
                    .layoutPriority(1)
            }
            ProgressView(value: Double(context.state.remainingPercent), total: 100)
                .tint(context.state.remainingPercent > 25 ? .green : .orange)
            Text("Resets in ") + Text(context.state.resetAt, style: .timer)
                .font(.subheadline.monospacedDigit())
        }
        .foregroundStyle(.white)
        .padding(.vertical, 4)
    }
}
