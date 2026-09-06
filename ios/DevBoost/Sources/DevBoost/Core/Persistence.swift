import Foundation
import Combine

private struct PersistedState: Codable {
    var hosts: [Host] = []
    var forwards: [PortForward] = []
    var forwardingSettings = PortForwardingSettings()
    var recentDestinations: [UUID: [RemoteDestination]] = [:]
    var transfers: [TransferRecord] = []
    var codexUsage = CodexUsageSnapshot.empty

    init(hosts: [Host] = [], forwards: [PortForward] = [], forwardingSettings: PortForwardingSettings = .init(), recentDestinations: [UUID: [RemoteDestination]] = [:], transfers: [TransferRecord] = [], codexUsage: CodexUsageSnapshot = .empty) {
        self.hosts = hosts
        self.forwards = forwards
        self.forwardingSettings = forwardingSettings
        self.recentDestinations = recentDestinations
        self.transfers = transfers
        self.codexUsage = codexUsage
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        hosts = try values.decodeIfPresent([Host].self, forKey: .hosts) ?? []
        forwards = try values.decodeIfPresent([PortForward].self, forKey: .forwards) ?? []
        forwardingSettings = try values.decodeIfPresent(PortForwardingSettings.self, forKey: .forwardingSettings) ?? .init()
        recentDestinations = try values.decodeIfPresent([UUID: [RemoteDestination]].self, forKey: .recentDestinations) ?? [:]
        transfers = try values.decodeIfPresent([TransferRecord].self, forKey: .transfers) ?? []
        codexUsage = try values.decodeIfPresent(CodexUsageSnapshot.self, forKey: .codexUsage) ?? .empty
    }
}

@MainActor
final class AppStore: ObservableObject {
    @Published private(set) var hosts: [Host] = []
    @Published private(set) var forwards: [PortForward] = []
    @Published var forwardingSettings = PortForwardingSettings() { didSet { save() } }
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
        forwards = state.forwards
        forwardingSettings = state.forwardingSettings
        recentDestinations = state.recentDestinations
        transfers = state.transfers
        codexUsage = state.codexUsage
    }

    func upsert(_ host: Host) { replace(&hosts, with: host); save() }
    func delete(_ host: Host) {
        if let keyID = host.keyID { KeyManager(keychain: keychain).remove(id: keyID) }
        hosts.removeAll { $0.id == host.id }
        recentDestinations[host.id] = nil
        forwards.removeAll { $0.hostID == host.id }
        save()
    }
    func upsert(_ forward: PortForward) { replace(&forwards, with: forward); save() }
    func delete(_ forward: PortForward) { forwards.removeAll { $0.id == forward.id }; save() }
    func forwards(for host: Host) -> [PortForward] { forwards.filter { $0.hostID == host.id } }
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
        let state = PersistedState(hosts: hosts, forwards: forwards, forwardingSettings: forwardingSettings, recentDestinations: recentDestinations, transfers: transfers, codexUsage: codexUsage)
        guard let data = try? JSONEncoder().encode(state) else { return }
        try? data.write(to: fileURL, options: [.atomic, .completeFileProtection])
        if retainsDataAfterDeletion { try? retentionStore.set(data, for: Self.retainedStateKey) }
    }
}
