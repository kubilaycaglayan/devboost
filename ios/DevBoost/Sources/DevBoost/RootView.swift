import SwiftUI

struct RootView: View {
    var body: some View {
        TabView {
            NavigationStack { HomeView() }.tabItem { Label("Home", systemImage: "square.grid.2x2.fill") }
            NavigationStack { HostsView() }.tabItem { Label("Hosts", systemImage: "server.rack") }
            NavigationStack { TerminalPickerView() }.tabItem { Label("Terminal", systemImage: "terminal.fill") }
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
                    .accessibilityIdentifier("home-feature-hosts")
                NavigationLink { TerminalPickerView() } label: { FeatureTile(title: "Terminal", detail: "Open a secure shell", symbol: "terminal.fill", tint: .indigo) }
                    .accessibilityIdentifier("home-feature-terminal")
                NavigationLink { DockerView() } label: { FeatureTile(title: "Docker", detail: "Containers, stats, logs", symbol: "shippingbox.fill", tint: .purple) }
                    .accessibilityIdentifier("home-feature-docker")
                NavigationLink { PortForwardView() } label: { FeatureTile(title: "Port Forwards", detail: "Open remote apps on iPhone", symbol: "arrow.left.arrow.right", tint: .cyan) }
                    .accessibilityIdentifier("home-feature-forwards")
                NavigationLink { TransferView() } label: { FeatureTile(title: "File Transfer", detail: "Photos and files to Ubuntu", symbol: "arrow.up.doc.fill", tint: .green) }
                    .accessibilityIdentifier("home-feature-transfer")
                NavigationLink { UsageView() } label: { FeatureTile(title: "AI Usage", detail: store.codexUsage.updatedAt == nil ? "Set up Codex usage" : "Updated just now", symbol: "chart.bar.fill", tint: .orange) }
                    .accessibilityIdentifier("home-feature-usage")
                }
                .buttonStyle(FeatureTileButtonStyle())
            }
            .padding(.vertical)
            .padding(.horizontal, 16)
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            NavigationLink { DataRetentionView() } label: {
                Image(systemName: "gearshape")
            }
            .accessibilityLabel("Data retention settings")
        }
        .task {
            while !Task.isCancelled {
                if let host = store.hosts.first, host.keyInstalled {
                    do {
                        store.codexUsage = try await CodexUsageService(remote: RemoteCommandService(keychain: store.keychain)).refresh(on: host)
                        await LiveUsageActivity.sync(with: store.codexUsage, hostName: host.name)
                    }
                    catch { store.codexUsage = CodexUsageSnapshot(message: error.localizedDescription) }
                }
                try? await Task.sleep(for: .seconds(UsageRefreshPolicy.foregroundInterval))
            }
        }
    }
}

private struct DataRetentionView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        Form {
            Section {
                Toggle("Keep data after app deletion", isOn: Binding(
                    get: { store.retainsDataAfterDeletion },
                    set: { store.setRetainsDataAfterDeletion($0) }
                ))
            } header: {
                Text("Data retention")
            } footer: {
                Text("When enabled, DevBoost keeps an encrypted, device-only copy of your saved app data in the Keychain. Reinstalling the app restores it. Turn this off to remove that retained copy.")
            }
        }
        .navigationTitle("Settings")
    }
}

// The tiles draw their own surfaces; avoid automatic navigation-link chrome.
private struct FeatureTileButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .contentShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
            .opacity(configuration.isPressed ? 0.75 : 1)
    }
}

private struct FeatureTile: View {
    let title: String; let detail: String; let symbol: String; let tint: Color
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Circle()
                    .fill(tint.opacity(0.15))
                    .frame(width: 42, height: 42)
                    .overlay {
                        Image(systemName: symbol)
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(tint)
                    }
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
