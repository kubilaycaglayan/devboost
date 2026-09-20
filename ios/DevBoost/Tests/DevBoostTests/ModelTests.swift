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

    func testCodexUsageParserReadsChatGPTUpstreamWindows() throws {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let payload: [String: Any] = ["devboostSource": "https", "plan_type": "plus",
            "rate_limit": ["primary_window": ["used_percent": 63, "reset_at": 1_700_003_600],
                            "secondary_window": ["used_percent": 10, "reset_at": 1_700_086_400]],
            "credits": ["balance": "0", "unlimited": false],
            "rate_limit_reset_credits": ["available_count": 1]]
        let snapshot = try CodexUsageParser.decode(payload, now: now)
        XCTAssertEqual(snapshot.primaryUsedPercent, 63)
        XCTAssertEqual(snapshot.secondaryUsedPercent, 10)
        XCTAssertEqual(snapshot.primaryResetAt, Date(timeIntervalSince1970: 1_700_003_600))
        XCTAssertEqual(snapshot.source, "[HTTPS] OpenAI upstream")
        XCTAssertEqual(snapshot.availableResets, 1)
    }

    func testClaudeUsageParserReadsAvailableWindowsAndISO8601Reset() throws {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let payload: [String: Any] = [
            "subscription_type": "pro",
            "five_hour": ["utilization": 12.5, "resets_at": "2030-01-01T00:00:00Z"],
            "seven_day": ["utilization": 50],
            "seven_day_opus": NSNull()
        ]
        let snapshot = try ClaudeUsageParser.decode(payload, now: now)
        XCTAssertEqual(snapshot.quotas.map(\.id), ["five_hour", "seven_day"])
        XCTAssertEqual(snapshot.quotas[0].usedPercent, 12.5)
        XCTAssertEqual(snapshot.quotas[0].resetAt, ISO8601DateFormatter().date(from: "2030-01-01T00:00:00Z"))
        XCTAssertEqual(snapshot.planType, "pro")
        XCTAssertEqual(snapshot.updatedAt, now)
    }

    func testClaudeUsageParserReadsLocalTokenFallback() throws {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let payload: [String: Any] = [
            "devboostSource": "local",
            "devboostObservedTokens": 1_234,
            "devboostLiveError": "HTTP Error 429: Too Many Requests"
        ]

        let snapshot = try ClaudeUsageParser.decode(payload, now: now)

        XCTAssertTrue(snapshot.quotas.isEmpty)
        XCTAssertEqual(snapshot.observedTokens, 1_234)
        XCTAssertEqual(snapshot.source, "[Local] Claude remote records")
        XCTAssertEqual(snapshot.updatedAt, now)
        XCTAssertEqual(snapshot.message, "HTTP Error 429: Too Many Requests Showing observed token usage.")
    }

    func testClaudeSnapshotRetainsQuotaDataWhenRefreshFails() {
        let snapshot = ClaudeUsageSnapshot(
            quotas: [ClaudeUsageQuota(id: "five_hour", name: "Current session", usedPercent: 25, resetAt: nil)],
            planType: "pro",
            source: "[HTTPS] Claude Code subscription",
            updatedAt: Date(timeIntervalSince1970: 1_700_000_000),
            message: nil
        )

        let stale = snapshot.retainingData(with: "Claude is temporarily rate-limited.")

        XCTAssertEqual(stale.quotas, snapshot.quotas)
        XCTAssertEqual(stale.planType, snapshot.planType)
        XCTAssertEqual(stale.source, snapshot.source)
        XCTAssertEqual(stale.updatedAt, snapshot.updatedAt)
        XCTAssertEqual(stale.message, "Claude is temporarily rate-limited.")
    }
}
