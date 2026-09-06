import SwiftUI

enum AppTheme {
    static let accent = Color(red: 0.33, green: 0.92, blue: 0.52)
    static let background = Color(red: 0.035, green: 0.047, blue: 0.065)
    static let card = Color(red: 0.075, green: 0.09, blue: 0.12)
    static let muted = Color(red: 0.56, green: 0.60, blue: 0.67)
}

struct AppCard<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }

    var body: some View {
        content
            .padding(16)
            .background(AppTheme.card, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .stroke(.white.opacity(0.09), lineWidth: 1)
            }
    }
}

struct SectionIntro: View {
    let eyebrow: String
    let title: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(eyebrow.uppercased())
                .font(.caption.weight(.semibold))
                .tracking(1.1)
                .foregroundStyle(AppTheme.accent)
            Text(title)
                .font(.title2.weight(.bold))
            Text(detail)
                .font(.subheadline)
                .foregroundStyle(AppTheme.muted)
        }
    }
}
