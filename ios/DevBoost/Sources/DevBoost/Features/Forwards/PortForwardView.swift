import SwiftUI
import WebKit

struct PortForwardView: View {
    @EnvironmentObject private var store: AppStore
    @State private var manager = PortForwardManager()
    @State private var editing: PortForward?
    @State private var webURL: URL?
    @State private var runningIDs = Set<UUID>()
    @State private var message: String?

    var body: some View {
        List {
            Section {
                Toggle("Start saved forwards when opened", isOn: Binding(
                    get: { store.forwardingSettings.autoStartSavedForwards },
                    set: { store.forwardingSettings.autoStartSavedForwards = $0 }
                ))
            } footer: {
                Text("Tunnels run while DevBoost is active. iOS may stop them when the app is suspended.")
            }

            if store.hosts.isEmpty {
                ContentUnavailableView("No SSH hosts", systemImage: "point.3.connected.trianglepath.dotted", description: Text("Add an SSH host before creating a port forward."))
            } else if store.forwards.isEmpty {
                ContentUnavailableView("No port forwards", systemImage: "arrow.left.arrow.right", description: Text("Forward a service listening on your remote server to this iPhone."))
            } else {
                ForEach(store.forwards) { forward in
                    PortForwardRow(
                        forward: forward,
                        host: store.hosts.first { $0.id == forward.hostID },
                        isRunning: runningIDs.contains(forward.id),
                        start: { start(forward) },
                        stop: { stop(forward) },
                        edit: { editing = forward },
                        remove: { remove(forward) },
                        open: { if let url = forward.localURL { editing = nil; openForward(forward, url: url) } }
                    )
                }
            }
        }
        .navigationTitle("Port Forwards")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    editing = PortForward(hostID: store.hosts.first?.id ?? UUID(), remoteHost: store.forwardingSettings.defaultRemoteHost)
                } label: { Image(systemName: "plus") }
                    .disabled(store.hosts.isEmpty)
                    .accessibilityLabel("Add port forward")
            }
        }
        .sheet(item: $editing) { forward in
            NavigationStack {
                PortForwardEditor(forward: forward, hosts: store.hosts) { value in
                    store.upsert(value)
                    editing = nil
                }
            }
        }
        .sheet(isPresented: Binding(get: { webURL != nil }, set: { if !$0 { webURL = nil } })) {
            if let webURL {
                NavigationStack {
                    PortForwardWebView(url: webURL)
                        .navigationTitle("Forwarded Service")
                        .navigationBarTitleDisplayMode(.inline)
                }
            }
        }
        .alert("Port Forward", isPresented: Binding(get: { message != nil }, set: { if !$0 { message = nil } })) {
            Button("OK", role: .cancel) { message = nil }
        } message: { Text(message ?? "") }
        .task {
            guard store.forwardingSettings.autoStartSavedForwards else { return }
            for forward in store.forwards where forward.autoStart { start(forward) }
        }
        .onDisappear { Task { await manager.stopAll() }; runningIDs.removeAll() }
    }

    private func start(_ forward: PortForward) {
        guard let host = store.hosts.first(where: { $0.id == forward.hostID }) else { message = "Choose an SSH host for this forward."; return }
        Task {
            do {
                try await manager.start(forward, on: host, keychain: store.keychain)
                runningIDs.insert(forward.id)
            } catch { message = error.localizedDescription }
        }
    }

    private func stop(_ forward: PortForward) {
        Task { await manager.stop(forward); runningIDs.remove(forward.id) }
    }

    private func remove(_ forward: PortForward) {
        stop(forward)
        store.delete(forward)
    }

    private func openForward(_ forward: PortForward, url: URL) {
        guard runningIDs.contains(forward.id) else { message = "Start the forward before opening it."; return }
        // WKWebView keeps the remote service inside DevBoost and avoids relying
        // on another iOS app being able to reach the app's loopback listener.
        webURL = url
    }
}

private struct PortForwardRow: View {
    let forward: PortForward
    let host: Host?
    let isRunning: Bool
    let start: () -> Void
    let stop: () -> Void
    let edit: () -> Void
    let remove: () -> Void
    let open: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(forward.displayName).font(.headline)
                Spacer()
                Circle().fill(isRunning ? .green : .gray).frame(width: 9, height: 9)
            }
            Text("127.0.0.1:\(forward.localPort) → \(forward.remoteHost):\(forward.remotePort)").font(.caption.monospaced()).foregroundStyle(AppTheme.muted)
            Text(host?.name ?? "Missing SSH host").font(.caption).foregroundStyle(AppTheme.muted)
            HStack {
                Button(isRunning ? "Stop" : "Start", systemImage: isRunning ? "stop.fill" : "play.fill", action: isRunning ? stop : start)
                if isRunning { Button("Open", systemImage: "safari", action: open) }
                Spacer()
                Menu {
                    Button("Edit", systemImage: "pencil", action: edit)
                    Button("Delete", systemImage: "trash", role: .destructive, action: remove)
                } label: { Image(systemName: "ellipsis.circle") }
            }
            .buttonStyle(.bordered)
        }
        .padding(.vertical, 5)
    }
}

private struct PortForwardEditor: View {
    @Environment(\.dismiss) private var dismiss
    @State private var forward: PortForward
    let hosts: [Host]
    let save: (PortForward) -> Void

    init(forward: PortForward, hosts: [Host], save: @escaping (PortForward) -> Void) {
        _forward = State(initialValue: forward); self.hosts = hosts; self.save = save
    }

    var body: some View {
        Form {
            Section("Service") {
                TextField("Name", text: $forward.name)
                Picker("SSH host", selection: $forward.hostID) {
                    ForEach(hosts) { Text($0.name).tag($0.id) }
                }
                TextField("Remote host", text: $forward.remoteHost).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("Remote port", value: $forward.remotePort, format: .number).keyboardType(.numberPad)
            }
            Section("This iPhone") {
                TextField("Local port", value: $forward.localPort, format: .number).keyboardType(.numberPad)
                Toggle("Start automatically", isOn: $forward.autoStart)
            }
            Section {
                Text("The remote host is resolved from the SSH server. For most development servers, use 127.0.0.1 and the app's listening port.").font(.footnote).foregroundStyle(AppTheme.muted)
            }
        }
        .navigationTitle("Port Forward")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) { Button("Save") { save(forward) }.disabled(!forward.isValid || hosts.isEmpty) }
        }
    }
}

struct PortForwardWebView: View {
    let url: URL
    var body: some View { ForwardWebView(url: url).ignoresSafeArea(.all) }
}

private struct ForwardWebView: UIViewRepresentable {
    let url: URL
    func makeUIView(context: Context) -> WKWebView { WKWebView(frame: .zero) }
    func updateUIView(_ webView: WKWebView, context: Context) {
        guard webView.url != url else { return }
        webView.load(URLRequest(url: url))
    }
}
