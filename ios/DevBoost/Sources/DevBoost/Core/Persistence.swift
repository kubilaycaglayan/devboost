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
    @Published private(set) var retainsDataAfterDeletion = false
    let keychain: KeychainStore
    private let fileURL: URL
    private let retentionStore: any SecretStore
    private static let retainedStateKey = "retained-state-v1"
    private static let retentionPreferenceKey = "retain-data-after-deletion-v1"

    init(fileURL: URL? = nil, reset: Bool = false, retentionStore: (any SecretStore)? = nil) {
        keychain = KeychainStore()
        self.retentionStore = retentionStore ?? keychain
        if let fileURL {
            self.fileURL = fileURL
        } else {
            let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            self.fileURL = directory.appendingPathComponent("devboost-state.json")
        }
        if reset {
            try? FileManager.default.removeItem(at: self.fileURL)
            return
        }
        retainsDataAfterDeletion = (try? self.retentionStore.data(for: Self.retentionPreferenceKey)) == Data([1])
        let data = (try? Data(contentsOf: self.fileURL)) ?? (retainsDataAfterDeletion ? try? self.retentionStore.data(for: Self.retainedStateKey) : nil)
        guard let data, let state = try? JSONDecoder().decode(PersistedState.self, from: data) else { return }
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
    func setRetainsDataAfterDeletion(_ enabled: Bool) {
        retainsDataAfterDeletion = enabled
        if enabled {
            try? retentionStore.set(Data([1]), for: Self.retentionPreferenceKey)
            save()
        } else {
            try? retentionStore.remove(Self.retentionPreferenceKey)
            try? retentionStore.remove(Self.retainedStateKey)
        }
    }

    private func replace<T: Identifiable>(_ values: inout [T], with value: T) where T.ID: Equatable {
        if let index = values.firstIndex(where: { $0.id == value.id }) { values[index] = value } else { values.append(value) }
    }
    private func save() {
        let state = PersistedState(hosts: hosts, recentDestinations: recentDestinations, transfers: transfers, codexUsage: codexUsage)
        guard let data = try? JSONEncoder().encode(state) else { return }
        try? data.write(to: fileURL, options: [.atomic, .completeFileProtection])
        if retainsDataAfterDeletion { try? retentionStore.set(data, for: Self.retainedStateKey) }
    }
}
