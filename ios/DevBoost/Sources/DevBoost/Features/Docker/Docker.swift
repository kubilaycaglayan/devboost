import Foundation
import SwiftUI

struct DockerContainer: Identifiable, Hashable {
    let id: String
    let name: String
    let image: String
    let status: String
    let cpu: String
    let memory: String
    let network: String
}

struct DockerService: Sendable {
    let remote: any RemoteCommanding
    func containers(on host: Host) async throws -> [DockerContainer] {
        let marker = "__DEVBOOST_STATS__"
        let command = DockerCommands.snapshot(marker: marker)
        let output = try await remote.execute(command, on: host)
        return DockerOutputParser.containers(from: output, marker: marker)
    }
    func logs(container: DockerContainer, on host: Host) async throws -> String {
        try await remote.execute(DockerCommands.logs(container: container.name), on: host)
    }
}

enum DockerCommands {
    static func snapshot(marker: String = "__DEVBOOST_STATS__") -> String {
        "command -v docker >/dev/null || { echo Docker CLI is not installed; exit 127; }; docker ps --format '{{json .}}'; printf '\\n\(marker)\\n'; docker stats --no-stream --format '{{json .}}'"
    }

    static func logs(container: String) -> String {
        "docker logs --timestamps --tail 300 -- \(shellQuote(container)) 2>&1"
    }
}

enum DockerOutputParser {
    static func containers(from output: String, marker: String = "__DEVBOOST_STATS__") -> [DockerContainer] {
        let parts = output.components(separatedBy: marker)
        let ps = jsonLines(parts.first ?? "")
        let stats = jsonLines(parts.count > 1 ? parts[1] : "")
        var byName: [String: [String: Any]] = [:]
        for row in stats {
            if let name = row["Name"] as? String { byName[name] = row }
        }
        return ps.compactMap { row in
            let name = (row["Names"] as? String) ?? ""
            guard !name.isEmpty else { return nil }
            let value = byName[name] ?? [:]
            return DockerContainer(id: (row["ID"] as? String) ?? name, name: name, image: (row["Image"] as? String) ?? "", status: (row["Status"] as? String) ?? "", cpu: (value["CPUPerc"] as? String) ?? "—", memory: (value["MemUsage"] as? String) ?? "—", network: (value["NetIO"] as? String) ?? "—")
        }
    }

    private static func jsonLines(_ source: String) -> [[String: Any]] {
        source.split(whereSeparator: \.isNewline).compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
    }
}

struct DockerView: View {
    @EnvironmentObject private var store: AppStore
    @State private var hostID: UUID?
    @State private var containers: [DockerContainer] = []
    @State private var message = "Choose a host to inspect Docker."
    @State private var loading = false
    private var host: Host? { store.hosts.first { $0.id == (hostID ?? store.hosts.first?.id) } }

    var body: some View {
        List {
            Section {
                Picker("SSH host", selection: $hostID) { ForEach(store.hosts) { Text($0.name).tag(Optional($0.id)) } }
                Button("Refresh", systemImage: "arrow.clockwise") { refresh() }.disabled(host == nil || loading)
            }
            Section { Text(message).font(.footnote).foregroundStyle(.secondary) }
            Section("Containers") {
                ForEach(containers) { container in
                    NavigationLink { DockerLogView(container: container, host: host) } label: {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(container.name).font(.headline)
                            Text(container.image).font(.caption).foregroundStyle(.secondary)
                            Text("\(container.status) · CPU \(container.cpu) · \(container.memory)").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .overlay { if store.hosts.isEmpty { ContentUnavailableView("No SSH hosts", systemImage: "shippingbox", description: Text("Add a host before monitoring Docker.")) } }
        .navigationTitle("Docker")
        .onAppear { if hostID == nil { hostID = store.hosts.first?.id }; refresh() }
    }
    private func refresh() {
        guard let host else { return }
        loading = true; message = "Refreshing Docker…"
        Task { do { containers = try await DockerService(remote: RemoteCommandService(keychain: store.keychain)).containers(on: host); message = containers.isEmpty ? "Docker is available; no containers are running." : "\(containers.count) running container\(containers.count == 1 ? "" : "s")." } catch { containers = []; message = error.localizedDescription }; loading = false }
    }
}

private struct DockerLogView: View {
    @EnvironmentObject private var store: AppStore
    let container: DockerContainer
    let host: Host?
    @State private var logs = "Loading logs…"
    var body: some View {
        ScrollView { Text(logs).font(.system(.caption, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).padding() }
            .navigationTitle(container.name)
            .toolbar { Button("Refresh") { load() } }
            .task { load() }
    }
    private func load() { guard let host else { logs = "Select an SSH host first."; return }; Task { do { logs = try await DockerService(remote: RemoteCommandService(keychain: store.keychain)).logs(container: container, on: host) } catch { logs = error.localizedDescription } } }
}
