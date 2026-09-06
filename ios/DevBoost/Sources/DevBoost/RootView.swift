import SwiftUI

struct RootView: View {
    var body: some View {
        TabView {
            NavigationStack { HomeView() }.tabItem { Label("Home", systemImage: "square.grid.2x2.fill") }
            NavigationStack { HostsView() }.tabItem { Label("Hosts", systemImage: "server.rack") }
            NavigationStack { DockerView() }.tabItem { Label("Docker", systemImage: "shippingbox.fill") }
            NavigationStack { TransferView() }.tabItem { Label("Transfer", systemImage: "arrow.up.doc.fill") }
            NavigationStack { UsageView() }.tabItem { Label("Usage", systemImage: "chart.bar.fill") }
        }
        .tint(AppTheme.accent)
        .preferredColorScheme(.dark)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // Keep the tab container attached to the full scene. Without this,
        // SwiftUI can leave the scene's safe-area margins to the window
        // background, which appears as black bands on device-sized screens.
        .background(AppTheme.background)
        .ignoresSafeArea(.all)
    }
}

struct HomeView: View {
    @EnvironmentObject private var store: AppStore
    private let columns = [GridItem(.flexible()), GridItem(.flexible())]
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .firstTextBaseline, spacing: 7) {
                        Text("DevBoost").font(.title2.weight(.bold))
                        if let host = store.hosts.first {
                            Text("•").foregroundStyle(AppTheme.muted)
                            Text(host.name).font(.headline.weight(.semibold)).foregroundStyle(AppTheme.accent).lineLimit(1)
                        }
                    }
                    HStack(spacing: 6) {
                        Circle().fill(AppTheme.accent).frame(width: 8, height: 8)
                        Text(store.hosts.isEmpty ? "Ready to connect · Add your first server" : "Connected · Host: \(store.hosts.first?.displayAddress ?? "")")
                            .font(.caption.weight(.medium)).foregroundStyle(AppTheme.muted).lineLimit(1)
                    }
                }
                .padding(.horizontal)

                if store.hosts.isEmpty {
                    SectionIntro(eyebrow: "Secure server control", title: "Your command center", detail: "Connect an Ubuntu server to manage ports, containers, files, and usage.")
                        .padding(.horizontal)
                }

                LazyVGrid(columns: columns, spacing: 14) {
                NavigationLink { HostsView() } label: { FeatureTile(title: "SSH Connections", detail: "\(store.hosts.count) saved host\(store.hosts.count == 1 ? "" : "s")", symbol: "server.rack", tint: .blue) }
                NavigationLink { TerminalPickerView() } label: { FeatureTile(title: "Terminal", detail: "Open a secure shell", symbol: "terminal.fill", tint: .indigo) }
                NavigationLink { DockerView() } label: { FeatureTile(title: "Docker", detail: "Containers, stats, logs", symbol: "shippingbox.fill", tint: .purple) }
                NavigationLink { TransferView() } label: { FeatureTile(title: "File Transfer", detail: "Photos and files to Ubuntu", symbol: "arrow.up.doc.fill", tint: .green) }
                NavigationLink { UsageView() } label: { FeatureTile(title: "AI Usage", detail: store.codexUsage.updatedAt == nil ? "Set up Codex usage" : "Updated just now", symbol: "chart.bar.fill", tint: .orange) }
                }
                if let used = store.codexUsage.primaryUsedPercent { CodexHomeCard(used: used, updatedAt: store.codexUsage.updatedAt) }
            }
            .padding(.vertical)
            .padding(.horizontal, 2)
        }
        .navigationBarTitleDisplayMode(.inline)
        .task {
            while !Task.isCancelled {
                if let host = store.hosts.first, host.keyInstalled {
                    do { store.codexUsage = try await CodexUsageService(remote: RemoteCommandService(keychain: store.keychain)).refresh(on: host) }
                    catch { store.codexUsage = CodexUsageSnapshot(message: error.localizedDescription) }
                }
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }
}

private struct FeatureTile: View {
    let title: String; let detail: String; let symbol: String; let tint: Color
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: symbol)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(tint)
                    .frame(width: 42, height: 42)
                    .background(tint.opacity(0.15), in: Circle())
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.white.opacity(0.7))
            }
            Spacer(minLength: 4)
            Text(title).font(.headline).foregroundStyle(.white)
            Text(detail).font(.caption).foregroundStyle(.white.opacity(0.7)).lineLimit(2)
        }
        .frame(maxWidth: .infinity, minHeight: 142, alignment: .leading)
        .padding(16)
        .background(LinearGradient(colors: [tint.opacity(0.34), tint.opacity(0.12)], startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay { RoundedRectangle(cornerRadius: 22, style: .continuous).stroke(.white.opacity(0.1), lineWidth: 1) }
    }
}

private struct CodexHomeCard: View {
    let used: Double; let updatedAt: Date?
    var body: some View {
        AppCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Label("Codex short window", systemImage: "bolt.fill").font(.headline)
                    Spacer()
                    Text("\(Int(max(0, min(100, 100 - used)).rounded()))% left")
                        .font(.title3.weight(.bold))
                        .foregroundStyle(used <= 50 ? .green : used <= 75 ? .yellow : used <= 90 ? .orange : .red)
                }
                ProgressView(value: min(max(100 - used, 0), 100), total: 100).tint(used <= 50 ? .green : used <= 75 ? .yellow : used <= 90 ? .orange : .red)
                Text(updatedAt.map { "Updated \($0.formatted(date: .omitted, time: .shortened))" } ?? "Not updated yet")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal)
    }
}
