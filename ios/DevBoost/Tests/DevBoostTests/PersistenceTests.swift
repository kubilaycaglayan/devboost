import XCTest
@testable import DevBoost

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
}
