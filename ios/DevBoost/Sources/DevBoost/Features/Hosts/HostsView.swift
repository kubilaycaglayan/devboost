import SwiftUI

struct HostsView: View {
    @EnvironmentObject private var store: AppStore
    @State private var adding = false
    var body: some View {
        Group {
            if store.hosts.isEmpty {
                ContentUnavailableView {
                    Label("No SSH hosts", systemImage: "server.rack")
                } description: {
                    Text("Add your Ubuntu server to use DevBoost.")
                } actions: {
                    Button("Add SSH host", systemImage: "plus") { adding = true }
                        .buttonStyle(.borderedProminent)
                        .accessibilityLabel("Add your first SSH host")
                }
            } else {
                List {
                    ForEach(store.hosts) { host in
                        NavigationLink(value: host) {
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    Text(host.name).font(.headline)
                                    Spacer()
                                    Circle().fill(host.keyInstalled ? .green : .orange).frame(width: 8, height: 8)
                                }
                                Text(host.displayAddress).font(.subheadline).foregroundStyle(.secondary)
                                Text(host.keyInstalled ? "Ready to connect" : "Key setup needed")
                                    .font(.caption).foregroundStyle(host.keyInstalled ? .green : .orange)
                            }
                            .padding(.vertical, 5)
                        }
                    }.onDelete { indexSet in indexSet.map { store.hosts[$0] }.forEach(store.delete) }
                }
            }
        }
        .navigationTitle("SSH Connections")
        .navigationDestination(for: Host.self) { HostEditor(host: $0) }
        .toolbar { Button { adding = true } label: { Image(systemName: "plus") }.accessibilityLabel("Add SSH host") }
        .sheet(isPresented: $adding) { NavigationStack { HostEditor(host: Host()) } }
    }
}

struct HostEditor: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var host: Host
    @State private var password = ""
    @State private var publicKey = ""
    @State private var message: String?
    @State private var trustFingerprint: String?
    @State private var isWorking = false

    init(host: Host) { _host = State(initialValue: host) }
    var body: some View {
        Form {
            Section("Connection") {
                TextField("Name", text: $host.name)
                TextField("Hostname or IP", text: $host.hostname).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("Username", text: $host.username).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("SSH port", value: $host.port, format: .number).keyboardType(.numberPad)
            }
            Section {
                Button("Generate Ed25519 key", systemImage: "key.fill") { generateKey() }
                if !publicKey.isEmpty {
                    Text(publicKey).font(.caption2).textSelection(.enabled)
                    SecureField("Ubuntu password (used once)", text: $password)
                    Button("Install key on server", systemImage: "arrow.up.circle.fill") { installKey() }.disabled(isWorking)
                }
            } header: { Text("Device key") } footer: { Text("Private keys stay in this iPhone's Keychain. You will be asked to trust a new server fingerprint before connecting.") }
            if host.keyInstalled { Section { NavigationLink("Open terminal") { TerminalLaunchView(host: host) } } }
        }
        .navigationTitle("Host")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Save") { store.upsert(host); dismiss() }.disabled(!host.isConfigured) } }
        .onAppear { if let id = host.keyID { publicKey = (try? KeyManager(keychain: store.keychain).publicKey(id: id)) ?? "" } }
        .alert("DevBoost", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) { Button("OK", role: .cancel) {} } message: { Text(message ?? "") }
        .alert("Trust this server?", isPresented: Binding(get: { trustFingerprint != nil }, set: { if !$0 { trustFingerprint = nil } })) {
            Button("Trust") { host.knownFingerprint = trustFingerprint; trustFingerprint = nil; installKey() }
            Button("Cancel", role: .cancel) { trustFingerprint = nil }
        } message: { Text("Fingerprint:\n\(trustFingerprint ?? "")\n\nVerify this against the server before trusting it.") }
    }
    private func generateKey() {
        do {
            let manager = KeyManager(keychain: store.keychain)
            let previousKeyID = host.keyID
            let key = try manager.generate(comment: host.name)
            host.keyID = key.id; publicKey = key.publicKey; host.keyInstalled = false; host.knownFingerprint = nil
            if let previousKeyID { manager.remove(id: previousKeyID) }
        } catch { message = error.localizedDescription }
    }
    private func installKey() {
        guard !password.isEmpty, !publicKey.isEmpty else { message = "Generate a key and enter the server password first."; return }
        isWorking = true
        Task {
            do {
                try await SSHManager(keys: KeyManager(keychain: store.keychain)).installKey(host: host, password: password, publicKey: publicKey)
                host.keyInstalled = true; password = ""; message = "Key installed. Save this host."
            } catch let error as AppError {
                if case .firstHostKey(let fingerprint) = error { trustFingerprint = fingerprint } else { message = error.localizedDescription }
            } catch { message = error.localizedDescription }
            isWorking = false
        }
    }
}
