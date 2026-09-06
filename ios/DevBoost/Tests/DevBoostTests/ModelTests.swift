import XCTest
@testable import DevBoost

final class ModelTests: XCTestCase {
    func testHostRequiresAHostnameAndUsername() {
        XCTAssertFalse(Host().isConfigured)
        XCTAssertFalse(Host(hostname: "10.0.0.8", port: 22, username: "").isConfigured)
        XCTAssertTrue(Host(hostname: "10.0.0.8", port: 22, username: "ubuntu").isConfigured)
    }

    func testHostRejectsWhitespaceOnlyIdentityFields() {
        XCTAssertFalse(Host(hostname: "   ", port: 22, username: "ubuntu").isConfigured)
        XCTAssertFalse(Host(hostname: "server", port: 22, username: "\n\t").isConfigured)
    }

    func testHostRejectsInvalidPorts() {
        XCTAssertFalse(Host(hostname: "host", port: 0, username: "ubuntu").isConfigured)
        XCTAssertFalse(Host(hostname: "host", port: 65_536, username: "ubuntu").isConfigured)
    }

    func testPortForwardValidationAndLocalURL() {
        let hostID = UUID()
        let forward = PortForward(name: "Web", hostID: hostID, remoteHost: "127.0.0.1", remotePort: 3000, localPort: 13000)
        XCTAssertTrue(forward.isValid)
        XCTAssertEqual(forward.localURL?.absoluteString, "http://127.0.0.1:13000")
        XCTAssertFalse(PortForward(hostID: hostID, remotePort: 0).isValid)
        XCTAssertFalse(PortForward(hostID: hostID, localPort: 65536).isValid)
        XCTAssertFalse(PortForward(hostID: hostID, remoteHost: " ").isValid)
    }

    func testCodexSnapshotStartsEmpty() {
        XCTAssertNil(CodexUsageSnapshot.empty.primaryUsedPercent)
        XCTAssertNotNil(CodexUsageSnapshot.empty.message)
    }

    func testRemoteShellArgumentsAreQuoted() {
        XCTAssertEqual(shellQuote("folder name"), "'folder name'")
        XCTAssertEqual(shellQuote("it's safe"), "'it'\"'\"'s safe'")
    }

    func testCodexUsageParserReadsBothWindows() throws {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let payload: [String: Any] = ["rateLimitsByLimitId": ["codex": [
            "primary": ["usedPercent": 12.5, "resetsAt": 1_700_003_600],
            "secondary": ["usedPercent": 48, "resetsAt": 1_700_086_400]
        ]]]
        let snapshot = try CodexUsageParser.decode(payload, now: now)
        XCTAssertEqual(snapshot.primaryUsedPercent, 12.5)
        XCTAssertEqual(snapshot.secondaryUsedPercent, 48)
        XCTAssertEqual(snapshot.primaryResetAt, Date(timeIntervalSince1970: 1_700_003_600))
        XCTAssertEqual(snapshot.updatedAt, now)
    }
}
