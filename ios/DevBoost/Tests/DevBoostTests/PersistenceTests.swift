import XCTest
@testable import DevBoost

private final class InMemoryRetentionStore: SecretStore, @unchecked Sendable {
    var values: [String: Data] = [:]
    func set(_ value: Data, for key: String) throws { values[key] = value }
    func data(for key: String) throws -> Data? { values[key] }
    func remove(_ key: String) throws { values[key] = nil }
}

@MainActor
final class PersistenceTests: XCTestCase {
    private var directory: URL!
    private var stateURL: URL { directory.appendingPathComponent("state.json") }

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    func testFirstRunStartsEmptyWithoutCreatingSharedApplicationState() {
        let store = AppStore(fileURL: stateURL)
        XCTAssertTrue(store.hosts.isEmpty)
        XCTAssertTrue(store.transfers.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: stateURL.path))
    }

    func testHostsDestinationsAndTransfersRoundTrip() {
        let host = Host(name: "Lab", hostname: "example.test", port: 2222, username: "ubuntu")
        let transfer = TransferRecord(id: UUID(), hostID: host.id, fileName: "app.tar", remotePath: "/srv/app.tar", createdAt: Date(timeIntervalSince1970: 42), succeeded: true, message: "Uploaded")
        let first = AppStore(fileURL: stateURL)
        first.upsert(host)
        first.remember(destination: "  /srv/apps  ", for: host)
        first.record(transfer)

        let second = AppStore(fileURL: stateURL)
        XCTAssertEqual(second.hosts, [host])
        XCTAssertEqual(second.destinations(for: host).map(\.path), ["/srv/apps"])
        XCTAssertEqual(second.transfers, [transfer])
    }

    func testPortForwardsAndSettingsRoundTrip() {
        let host = Host(name: "Lab", hostname: "example.test", username: "ubuntu")
        let forward = PortForward(name: "Web", hostID: host.id, remoteHost: "127.0.0.1", remotePort: 8080, localPort: 18080, autoStart: true)
        let first = AppStore(fileURL: stateURL)
        first.upsert(host)
        first.upsert(forward)
        first.forwardingSettings.autoStartSavedForwards = true

        let second = AppStore(fileURL: stateURL)
        XCTAssertEqual(second.forwards, [forward])
        XCTAssertEqual(second.forwards(for: host), [forward])
        XCTAssertTrue(second.forwardingSettings.autoStartSavedForwards)
    }

    func testDeletingHostAlsoDeletesItsPortForwards() {
        let host = Host(hostname: "example.test", username: "ubuntu")
        let store = AppStore(fileURL: stateURL)
        store.upsert(host)
        store.upsert(PortForward(hostID: host.id))
        store.delete(host)
        XCTAssertTrue(store.forwards.isEmpty)
    }

    func testDestinationsRejectRelativePathsDeduplicateAndKeepNewestEight() {
        let host = Host(hostname: "host", username: "user")
        let store = AppStore(fileURL: stateURL)
        store.remember(destination: "relative", for: host)
        XCTAssertTrue(store.destinations(for: host).isEmpty)
        for index in 0..<10 { store.remember(destination: "/folder\(index)", for: host) }
        store.remember(destination: "/folder5", for: host)
        XCTAssertEqual(store.destinations(for: host).count, 8)
        XCTAssertEqual(store.destinations(for: host).first?.path, "/folder5")
        XCTAssertFalse(store.destinations(for: host).contains { $0.path == "/folder0" })
    }

    func testTransfersKeepTheMostRecentHundred() {
        let store = AppStore(fileURL: stateURL)
        let hostID = UUID()
        for index in 0..<101 {
            store.record(TransferRecord(id: UUID(), hostID: hostID, fileName: "file\(index)", remotePath: "/file\(index)", createdAt: .now, succeeded: true, message: "Uploaded"))
        }
        XCTAssertEqual(store.transfers.count, 100)
        XCTAssertEqual(store.transfers.first?.fileName, "file100")
        XCTAssertEqual(store.transfers.last?.fileName, "file1")
    }

    func testMalformedStateFallsBackToFirstRun() throws {
        try Data("not json".utf8).write(to: stateURL)
        let store = AppStore(fileURL: stateURL)
        XCTAssertTrue(store.hosts.isEmpty)
        XCTAssertTrue(store.transfers.isEmpty)
    }

    func testRetainedStateRestoresAfterAppContainerIsRemoved() throws {
        let retained = InMemoryRetentionStore()
        let host = Host(name: "Lab", hostname: "example.test", username: "ubuntu")
        let first = AppStore(fileURL: stateURL, retentionStore: retained)
        first.setRetainsDataAfterDeletion(true)
        first.upsert(host)
        first.remember(destination: "/srv/apps", for: host)
        let forward = PortForward(name: "Web", hostID: host.id, remotePort: 8080, localPort: 18080, autoStart: true)
        first.upsert(forward)
        first.forwardingSettings.autoStartSavedForwards = true

        try FileManager.default.removeItem(at: stateURL)
        let restored = AppStore(fileURL: stateURL, retentionStore: retained)
        XCTAssertTrue(restored.retainsDataAfterDeletion)
        XCTAssertEqual(restored.hosts, [host])
        XCTAssertEqual(restored.destinations(for: host).map(\.path), ["/srv/apps"])
        XCTAssertEqual(restored.forwards, [forward])
        XCTAssertTrue(restored.forwardingSettings.autoStartSavedForwards)
    }

    func testTurningOffRetentionRemovesTheRestoreCopy() throws {
        let retained = InMemoryRetentionStore()
        let host = Host(hostname: "example.test", username: "ubuntu")
        let first = AppStore(fileURL: stateURL, retentionStore: retained)
        first.setRetainsDataAfterDeletion(true)
        first.upsert(host)
        first.setRetainsDataAfterDeletion(false)

        try FileManager.default.removeItem(at: stateURL)
        let restored = AppStore(fileURL: stateURL, retentionStore: retained)
        XCTAssertFalse(restored.retainsDataAfterDeletion)
        XCTAssertTrue(restored.hosts.isEmpty)
    }

    func testResetDoesNotRestoreRetainedDataForUITests() {
        let retained = InMemoryRetentionStore()
        let first = AppStore(fileURL: stateURL, retentionStore: retained)
        first.setRetainsDataAfterDeletion(true)
        first.upsert(Host(hostname: "example.test", username: "ubuntu"))

        let reset = AppStore(fileURL: stateURL, reset: true, retentionStore: retained)
        XCTAssertTrue(reset.hosts.isEmpty)
    }
}
