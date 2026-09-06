import Foundation
import Combine

private struct PersistedState: Codable {
    var hosts: [Host] = []
    var recentDestinations: [UUID: [RemoteDestination]] = [:]
    var transfers: [TransferRecord] = []
    var codexUsage = CodexUsageSnapshot.empty
}

@MainActor
final class AppStore: ObservableObject {
    @Published private(set) var hosts: [Host] = []
    @Published private(set) var recentDestinations: [UUID: [RemoteDestination]] = [:]
    @Published private(set) var transfers: [TransferRecord] = []
    @Published var codexUsage = CodexUsageSnapshot.empty { didSet { save() } }
    let keychain = KeychainStore()
    private let fileURL: URL

    init(fileURL: URL? = nil, reset: Bool = false) {
        if let fileURL {
            self.fileURL = fileURL
        } else {
            let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            self.fileURL = directory.appendingPathComponent("devboost-state.json")
        }
        if reset { try? FileManager.default.removeItem(at: self.fileURL) }
        guard let data = try? Data(contentsOf: self.fileURL), let state = try? JSONDecoder().decode(PersistedState.self, from: data) else { return }
        hosts = state.hosts
        recentDestinations = state.recentDestinations
        transfers = state.transfers
        codexUsage = state.codexUsage
    }

    func upsert(_ host: Host) { replace(&hosts, with: host); save() }
    func delete(_ host: Host) {
        if let keyID = host.keyID { KeyManager(keychain: keychain).remove(id: keyID) }
        hosts.removeAll { $0.id == host.id }
        recentDestinations[host.id] = nil
        save()
    }
    func remember(destination: String, for host: Host) {
        let clean = destination.trimmingCharacters(in: .whitespacesAndNewlines)
        guard clean.hasPrefix("/") else { return }
        var values = recentDestinations[host.id, default: []]
        values.removeAll { $0.path == clean }
        values.insert(RemoteDestination(path: clean), at: 0)
        recentDestinations[host.id] = Array(values.prefix(8))
        save()
    }
    func record(_ transfer: TransferRecord) { transfers.insert(transfer, at: 0); transfers = Array(transfers.prefix(100)); save() }
    func destinations(for host: Host) -> [RemoteDestination] { recentDestinations[host.id, default: []] }

    private func replace<T: Identifiable>(_ values: inout [T], with value: T) where T.ID: Equatable {
        if let index = values.firstIndex(where: { $0.id == value.id }) { values[index] = value } else { values.append(value) }
    }
    private func save() {
        let state = PersistedState(hosts: hosts, recentDestinations: recentDestinations, transfers: transfers, codexUsage: codexUsage)
        guard let data = try? JSONEncoder().encode(state) else { return }
        try? data.write(to: fileURL, options: [.atomic, .completeFileProtection])
    }
}
