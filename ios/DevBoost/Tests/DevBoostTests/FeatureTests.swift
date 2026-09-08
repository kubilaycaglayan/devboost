import Foundation
import XCTest
@testable import DevBoost

private enum FakeRemoteError: Error, Equatable {
    case unavailable
}

private enum FakeTransferError: Error, Equatable {
    case writeFailed
}

private actor SimulatedRemoteFile {
    struct Write: Equatable, Sendable {
        let offset: UInt64
        let size: Int
    }

    private var data = Data()
    private var writes: [Write] = []
    private let failingWrite: Int?

    init(failingWrite: Int? = nil) { self.failingWrite = failingWrite }

    func write(_ chunk: Data, at offset: UInt64) throws {
        let writeIndex = writes.count
        writes.append(Write(offset: offset, size: chunk.count))
        if writeIndex == failingWrite { throw FakeTransferError.writeFailed }
        let start = Int(offset)
        let end = start + chunk.count
        if data.count < end { data.append(Data(repeating: 0, count: end - data.count)) }
        data.replaceSubrange(start..<end, with: chunk)
    }

    func contents() -> Data { data }
    func recordedWrites() -> [Write] { writes }
}

private final class ProgressRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [Double] = []

    func record(_ value: Double) { lock.withLock { values.append(value) } }
    func snapshot() -> [Double] { lock.withLock { values } }
}

private actor RecordingRemoteCommand: RemoteCommanding {
    private let result: Result<String, FakeRemoteError>
    private(set) var commands: [String] = []

    init(result: Result<String, FakeRemoteError>) { self.result = result }

    func execute(_ command: String, on host: Host) async throws -> String {
        commands.append(command)
        return try result.get()
    }
}

final class FeatureTests: XCTestCase {
    func testUsageRefreshPolicySchedulesBackgroundRefreshEvery15Minutes() {
        let now = Date(timeIntervalSince1970: 1_000)
        XCTAssertEqual(UsageRefreshPolicy.foregroundInterval, 30)
        XCTAssertEqual(UsageRefreshPolicy.nextBackgroundRefresh(after: now),
                       Date(timeIntervalSince1970: 1_900))
    }

    func testUsageActivityUsesRemotePushEligibleActivityType() throws {
        let source = try String(contentsOf: URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Sources/DevBoost/Features/Usage/LiveUsageActivity.swift"), encoding: .utf8)
        XCTAssertTrue(source.contains("DEVBOOST_APNS_ENABLED"))
        XCTAssertTrue(source.contains(".token"))
        XCTAssertTrue(source.contains("pushType:"))
        XCTAssertTrue(source.contains("activity.pushToken"))
    }

    func testPhotoSelectionPreservesFilenameAndBytesAndIsolatesDuplicateNames() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        for name in ["IMG_1234.HEIC", "holiday photo.JPEG", "clip.MOV", "İstanbul.png"] {
            let source = directory.appendingPathComponent(name)
            let bytes = Data([0, 1, 255, 128, 42])
            try bytes.write(to: source)
            let first = try PhotoTransferStaging.copy(source)
            let second = try PhotoTransferStaging.copy(source)
            defer {
                try? FileManager.default.removeItem(at: first.deletingLastPathComponent())
                try? FileManager.default.removeItem(at: second.deletingLastPathComponent())
            }
            XCTAssertEqual(first.lastPathComponent, name)
            XCTAssertEqual(second.lastPathComponent, name)
            XCTAssertNotEqual(first, second)
            XCTAssertEqual(try Data(contentsOf: first), bytes)
            XCTAssertEqual(try Data(contentsOf: second), bytes)
            XCTAssertEqual(try Data(contentsOf: source), bytes)
        }
    }

    func testStagedFileStreamsByteForByteAcrossSupportedFileTypes() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let signatures: [(String, [UInt8])] = [
            ("IMG_7418.HEIC", [0, 0, 0, 24, 0x66, 0x74, 0x79, 0x70, 0x68, 0x65, 0x69, 0x63]),
            ("portrait.jpeg", [0xff, 0xd8, 0xff, 0xe1]),
            ("transparent image.PNG", [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
            ("Live Photo.MOV", [0, 0, 0, 20, 0x66, 0x74, 0x79, 0x70, 0x71, 0x74]),
            ("document.pdf", Array("%PDF-1.7".utf8)),
            ("archive.zip", [0x50, 0x4b, 0x03, 0x04]),
            ("İstanbul notes.txt", Array("hello\n".utf8))
        ]

        for (name, signature) in signatures {
            var expected = Data(signature)
            expected.append(contentsOf: (expected.count..<(FileUploadStream.chunkSize + 37)).map { UInt8($0 % 251) })
            let source = directory.appendingPathComponent(name)
            try expected.write(to: source)
            let staged = try PhotoTransferStaging.copy(source)
            defer { PhotoTransferStaging.discard(staged) }
            let remote = SimulatedRemoteFile()

            let sent = try await FileUploadStream.write(fileURL: staged, progress: { _ in }) { chunk, offset in
                try await remote.write(chunk, at: offset)
            }
            let uploaded = await remote.contents()
            let writes = await remote.recordedWrites()

            XCTAssertEqual(staged.lastPathComponent, name)
            XCTAssertEqual(sent, UInt64(expected.count))
            XCTAssertEqual(uploaded, expected, "Bytes changed while transferring \(name)")
            XCTAssertEqual(writes, [
                .init(offset: 0, size: FileUploadStream.chunkSize),
                .init(offset: UInt64(FileUploadStream.chunkSize), size: 37)
            ])
        }
    }

    func testUploadChunkOffsetsCoverEmptyExactBoundaryAndMultiChunkFiles() async throws {
        let sizes = [0, 1, FileUploadStream.chunkSize - 1, FileUploadStream.chunkSize,
                     FileUploadStream.chunkSize + 1, FileUploadStream.chunkSize * 2,
                     FileUploadStream.chunkSize * 2 + 19]
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }

        for size in sizes {
            let expected = Data((0..<size).map { UInt8($0 % 253) })
            let source = directory.appendingPathComponent("boundary-\(size).bin")
            try expected.write(to: source)
            let remote = SimulatedRemoteFile()
            let progress = ProgressRecorder()

            let sent = try await FileUploadStream.write(fileURL: source, progress: progress.record) { chunk, offset in
                try await remote.write(chunk, at: offset)
            }
            let uploaded = await remote.contents()
            let writes = await remote.recordedWrites()
            let expectedOffsets = stride(from: 0, to: size, by: FileUploadStream.chunkSize).map(UInt64.init)
            let progressValues = progress.snapshot()

            XCTAssertEqual(sent, UInt64(size))
            XCTAssertEqual(uploaded, expected)
            XCTAssertEqual(writes.map(\.offset), expectedOffsets)
            XCTAssertTrue(zip(progressValues, progressValues.dropFirst()).allSatisfy { $0.0 <= $0.1 })
            if size == 0 { XCTAssertTrue(progressValues.isEmpty) }
            else { XCTAssertEqual(progressValues.last ?? -1, 1, accuracy: 0.000_001) }
        }
    }

    func testUploadStopsAtWriteFailureWithoutReportingLaterChunks() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appendingPathComponent("failure.heic")
        try Data(repeating: 0xab, count: FileUploadStream.chunkSize * 2 + 1).write(to: source)
        let remote = SimulatedRemoteFile(failingWrite: 1)

        do {
            _ = try await FileUploadStream.write(fileURL: source, progress: { _ in }) { chunk, offset in
                try await remote.write(chunk, at: offset)
            }
            XCTFail("A failed remote write must fail the upload")
        } catch let error as FakeTransferError {
            XCTAssertEqual(error, .writeFailed)
        }
        let writes = await remote.recordedWrites()
        let uploaded = await remote.contents()
        XCTAssertEqual(writes.map(\.offset), [0, UInt64(FileUploadStream.chunkSize)])
        XCTAssertEqual(uploaded.count, FileUploadStream.chunkSize)
    }

    func testUploadSizeVerificationRejectsMissingAndTruncatedRemoteFiles() throws {
        XCTAssertNoThrow(try FileUploadStream.verify(expectedSize: 262_145, uploadedSize: 262_145))
        for uploadedSize: UInt64? in [nil, 0, 131_072, 262_144, 262_146] {
            XCTAssertThrowsError(try FileUploadStream.verify(expectedSize: 262_145, uploadedSize: uploadedSize)) { error in
                XCTAssertTrue((error as? AppError)?.errorDescription?.contains("Upload verification failed") == true)
            }
        }
    }

    func testDockerServiceRunsSnapshotAndLogFlowsThroughRemoteBoundary() async throws {
        let output = #"""
{"ID":"abc","Names":"web","Image":"nginx","Status":"Up"}
__DEVBOOST_STATS__
{"Name":"web","CPUPerc":"1%","MemUsage":"2MiB / 4MiB","NetIO":"3kB / 4kB"}
"""#
        let remote = RecordingRemoteCommand(result: .success(output))
        let service = DockerService(remote: remote)
        let host = Host(hostname: "server.example", username: "ubuntu")

        let containers = try await service.containers(on: host)
        let logs = try await service.logs(container: containers[0], on: host)
        let commands = await remote.commands

        XCTAssertEqual(containers.first?.name, "web")
        XCTAssertEqual(containers.first?.cpu, "1%")
        XCTAssertEqual(logs, output)
        XCTAssertEqual(commands.count, 2)
        XCTAssertTrue(commands[0].contains("docker ps"))
        XCTAssertTrue(commands[1].contains("docker logs --timestamps --tail 300 -- 'web'"))
    }

    func testDockerServicePreservesRemoteFailure() async {
        let remote = RecordingRemoteCommand(result: .failure(.unavailable))
        do {
            _ = try await DockerService(remote: remote).containers(on: Host(hostname: "server.example", username: "ubuntu"))
            XCTFail("Docker failures must reach the feature boundary")
        } catch let error as FakeRemoteError {
            XCTAssertEqual(error, .unavailable)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testCodexServiceExecutesRemoteQuotaScriptAndNormalizesResponse() async throws {
        let response = #"{"rateLimitsByLimitId":{"codex":{"primary":{"usedPercent":37.5,"resetsAt":1700000000}}}}"#
        let remote = RecordingRemoteCommand(result: .success(response))
        let snapshot = try await CodexUsageService(remote: remote).refresh(on: Host(hostname: "server.example", username: "ubuntu"))
        let commands = await remote.commands
        let command = try XCTUnwrap(commands.first)

        XCTAssertEqual(snapshot.primaryUsedPercent, 37.5)
        XCTAssertTrue(command.contains("python3"))
        XCTAssertFalse(command.contains("account/rateLimits/read"),
                       "The command should encode the complete script, not interpolate protocol text")
        XCTAssertTrue(command.contains("base64 -d"))
    }

    func testCodexServiceRejectsMalformedRemoteResponse() async {
        let remote = RecordingRemoteCommand(result: .success("not json"))
        do {
            _ = try await CodexUsageService(remote: remote).refresh(on: Host(hostname: "server.example", username: "ubuntu"))
            XCTFail("Malformed quota output must fail")
        } catch let error as AppError {
            XCTAssertEqual(error.errorDescription, "Codex returned invalid quota data.")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testSSHConnectionRejectsInvalidHostAndMissingKeyBeforeNetworkAccess() async {
        do {
            _ = try await SSHConnectionFactory(keychain: KeychainStore()).connect(to: Host())
            XCTFail("Invalid host should be rejected locally")
        } catch let error as AppError {
            guard case .invalidHost = error else { return XCTFail("Unexpected error: \(error)") }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }

        do {
            _ = try await SSHConnectionFactory(keychain: KeychainStore()).connect(to: Host(hostname: "example.test", username: "ubuntu"))
            XCTFail("Missing key should be rejected locally")
        } catch let error as AppError {
            guard case .noKey = error else { return XCTFail("Unexpected error: \(error)") }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testPortForwardManagerRejectsInvalidConfigurationBeforeOpeningSSH() async {
        let forward = PortForward(hostID: UUID(), remotePort: 0)
        do {
            try await PortForwardManager().start(forward, on: Host(), keychain: KeychainStore())
            XCTFail("Expected invalid forwarding configuration")
        } catch let error as PortForwardError {
            XCTAssertEqual(error, .invalidConfiguration)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testDockerParserMergesStatsByNameAndUsesSafeDefaults() {
        let output = #"""
{"ID":"abc","Names":"web","Image":"nginx","Status":"Up"}
{"Names":""}
{"ID":"no-stats","Names":"worker"}
__MARK__
{"Name":"web","CPUPerc":"2.5%","MemUsage":"4MiB / 8MiB","NetIO":"1kB / 2kB"}
not-json
"""#
        let values = DockerOutputParser.containers(from: output, marker: "__MARK__")
        XCTAssertEqual(values.count, 2)
        XCTAssertEqual(values[0].id, "abc")
        XCTAssertEqual(values[0].cpu, "2.5%")
        XCTAssertEqual(values[1].id, "no-stats")
        XCTAssertEqual(values[1].cpu, "—")
        XCTAssertEqual(values[1].network, "—")
    }

    func testDockerCommandsQuoteLogContainerAndKeepRemoteBoundaries() {
        let snapshot = DockerCommands.snapshot(marker: "marker with spaces")
        XCTAssertTrue(snapshot.contains("printf '\\nmarker with spaces\\n'"))
        XCTAssertTrue(DockerCommands.logs(container: "web; rm -rf /").contains("'web; rm -rf /'"))
    }

    func testRemotePathNormalizesWhitespaceAndSanitizesFileNames() {
        XCTAssertEqual(RemotePath.normalized("  srv/app  "), "/srv/app")
        XCTAssertEqual(RemotePath.normalized(" /srv/app "), "/srv/app")
        XCTAssertEqual(RemotePath.safeFileName("folder/file\0name"), "folder_file_name")
        XCTAssertEqual(RemotePath.safeFileName(""), "")
    }

    func testTransferCommandsAndHomeOutputHandleShellBoundaries() throws {
        XCTAssertEqual(RemoteTransferCommands.folders(path: "/srv/a folder"), "find '/srv/a folder' -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null | LC_ALL=C sort")
        XCTAssertEqual(RemoteTransferCommands.createDirectory(path: "/srv/a folder"), "mkdir -p -- '/srv/a folder'")
        XCTAssertEqual(try RemoteTransferOutput.homeDirectory(from: "/home/ubuntu\nextra\n"), "/home/ubuntu")
        XCTAssertThrowsError(try RemoteTransferOutput.homeDirectory(from: "ubuntu\n"))
        XCTAssertThrowsError(try RemoteTransferOutput.homeDirectory(from: ""))
    }

    func testTmuxSessionsListsNamesAndBuildsSafelyQuotedTerminalCommands() async throws {
        let remote = RecordingRemoteCommand(result: .success("work\nmy session\nwork\n"))
        let host = Host(name: "Development", hostname: "server.example", username: "ubuntu")

        let sessions = try await TmuxSessions.list(on: host, remote: remote)
        let commands = await remote.commands

        XCTAssertEqual(sessions, ["my session", "work"])
        XCTAssertEqual(commands.count, 1)
        XCTAssertTrue(commands[0].contains("command -v tmux"))
        XCTAssertTrue(commands[0].contains("tmux list-sessions -F '#{session_name}'"))
        XCTAssertEqual(TmuxSessions.createCommand(named: "dev'; touch /tmp/nope"), "exec tmux new-session -s 'dev'\"'\"'; touch /tmp/nope'")
        XCTAssertEqual(TmuxSessions.attachCommand(named: "my session"), "exec tmux attach-session -t 'my session'")
    }

    func testTmuxSessionsReportsWhenTmuxIsUnavailable() async {
        let remote = RecordingRemoteCommand(result: .success("__DEVBOOST_TMUX_UNAVAILABLE__\n"))
        do {
            _ = try await TmuxSessions.list(on: Host(name: "Development", hostname: "server.example", username: "ubuntu"), remote: remote)
            XCTFail("A host without tmux must show an actionable error")
        } catch let error as AppError {
            XCTAssertEqual(error.errorDescription, "tmux is not installed on Development.")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testCodexParserAcceptsLegacyAndMillisecondResetDates() throws {
        let payload: [String: Any] = ["rateLimits": [
            "primary": ["usedPercent": NSNumber(value: 101), "resetsAt": NSNumber(value: 1_700_000_000_000)],
            "secondary": ["usedPercent": NSNumber(value: 0)]
        ]]
        let snapshot = try CodexUsageParser.decode(payload, now: Date(timeIntervalSince1970: 1_700_000_000))
        XCTAssertEqual(snapshot.primaryUsedPercent, 101)
        XCTAssertEqual(snapshot.primaryResetAt, Date(timeIntervalSince1970: 1_700_000_000))
        XCTAssertEqual(snapshot.secondaryUsedPercent, 0)
        XCTAssertNil(snapshot.secondaryResetAt)
    }

    func testCodexParserRejectsMissingOrMalformedQuotaData() {
        let payloads: [[String: Any]] = [[:], ["rateLimitsByLimitId": ["codex": [:]]], ["rateLimits": ["codex": ["primary": ["usedPercent": "50"]]]]]
        for payload in payloads {
            XCTAssertThrowsError(try CodexUsageParser.decode(payload), "Payload should not be treated as valid quota data") { error in
                XCTAssertEqual((error as? AppError)?.errorDescription, "Codex returned no subscription quota data.")
            }
        }
    }

    func testConnectionAndErrorStatesHaveUserFacingMessages() {
        XCTAssertEqual(ConnectionState.disconnected.title, "Disconnected")
        XCTAssertEqual(ConnectionState.reconnecting.title, "Reconnecting")
        XCTAssertEqual(ConnectionState.failed("network down").title, "network down")
        XCTAssertEqual(AppError.invalidHost.errorDescription, "Enter a hostname, username, and valid SSH port.")
        XCTAssertEqual(AppError.noKey.errorDescription, "Generate an SSH key before connecting.")
    }
}
