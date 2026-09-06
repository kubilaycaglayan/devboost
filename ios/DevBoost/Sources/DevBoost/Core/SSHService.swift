import Citadel
import Crypto
import Foundation
import NIO
import NIOSSH

final class KnownHostValidator: NIOSSHClientServerAuthenticationDelegate, @unchecked Sendable {
    let expected: String?
    init(expected: String?) { self.expected = expected }

    func validateHostKey(hostKey: NIOSSHPublicKey, validationCompletePromise: EventLoopPromise<Void>) {
        var buffer = ByteBufferAllocator().buffer(capacity: 256)
        hostKey.write(to: &buffer)
        let bytes = buffer.readData(length: buffer.readableBytes) ?? Data()
        let fingerprint = "SHA256:" + Data(SHA256.hash(data: bytes)).base64EncodedString().replacingOccurrences(of: "=", with: "")
        guard let expected else { validationCompletePromise.fail(AppError.firstHostKey(fingerprint)); return }
        expected == fingerprint ? validationCompletePromise.succeed(()) : validationCompletePromise.fail(AppError.hostKeyChanged(fingerprint))
    }
}

protocol RemoteCommanding: Sendable {
    func execute(_ command: String, on host: Host) async throws -> String
}

struct RemoteCommandService: RemoteCommanding {
    let keychain: KeychainStore

    func execute(_ command: String, on host: Host) async throws -> String {
        var lastError: Error?
        for attempt in 0..<2 {
            do {
                let client = try await SSHConnectionFactory(keychain: keychain).connect(to: host)
                do {
                    // Keep stderr out of structured command output. Commands
                    // that need stderr explicitly redirect it themselves.
                    let output = String(buffer: try await client.executeCommand(command, mergeStreams: false))
                    try? await client.close()
                    return output
                } catch {
                    try? await client.close()
                    throw error
                }
            } catch {
                lastError = error
                if attempt == 0 && isTransient(error) {
                    try? await Task.sleep(for: .milliseconds(400))
                    continue
                }
                throw presented(error)
            }
        }
        throw presented(lastError ?? AppError.connectionFailed("SSH command failed."))
    }

    private func presented(_ error: Error) -> Error {
        if error is AppError { return error }
        if error is AuthenticationFailed { return AppError.connectionFailed("Authentication failed. Check the installed SSH key.") }
        if let error = error as? SSHClientError {
            switch error {
            case .allAuthenticationOptionsFailed, .unsupportedPrivateKeyAuthentication:
                return AppError.connectionFailed("Authentication failed. Check that this phone's SSH key is installed on the server.")
            case .channelCreationFailed:
                return AppError.connectionFailed("SSH connected, but the server refused a command channel. Try again.")
            case .unsupportedPasswordAuthentication, .unsupportedHostBasedAuthentication:
                return AppError.connectionFailed("The server does not support the configured SSH authentication method.")
            }
        }
        if let error = error as? CitadelError {
            switch error {
            case .channelCreationFailed, .channelFailure:
                return AppError.connectionFailed("The SSH command channel was interrupted. Try again.")
            case .unauthorized:
                return AppError.connectionFailed("The server rejected the SSH request.")
            default:
                break
            }
        }
        if let error = error as? SSHClient.CommandFailed {
            return AppError.connectionFailed("Remote command failed with exit code \(error.exitCode).")
        }
        return AppError.connectionFailed("SSH command failed: \(error.localizedDescription)")
    }

    private func isTransient(_ error: Error) -> Bool {
        if let error = error as? SSHClientError {
            switch error { case .channelCreationFailed: return true; default: return false }
        }
        if let error = error as? CitadelError {
            switch error { case .channelCreationFailed, .channelFailure: return true; default: return false }
        }
        return false
    }
}

struct SSHConnectionFactory: Sendable {
    let keychain: KeychainStore
    func connect(to host: Host) async throws -> SSHClient {
        guard host.isConfigured else { throw AppError.invalidHost }
        guard let keyID = host.keyID else { throw AppError.noKey }
        let privateKey = try KeyManager(keychain: keychain).privateKey(id: keyID)
        return try await SSHClient.connect(
            host: host.hostname, port: host.port,
            authenticationMethod: .ed25519(username: host.username, privateKey: privateKey),
            hostKeyValidator: .custom(KnownHostValidator(expected: host.knownFingerprint)),
            reconnect: .once, connectTimeout: .seconds(15)
        )
    }
}

final class SSHManager: @unchecked Sendable {
    private let keys: KeyManager
    private let stateLock = NSLock()
    private var client: SSHClient?
    private var writer: TTYStdinWriter?
    private var terminalTask: Task<Void, Never>?

    init(keys: KeyManager) { self.keys = keys }

    func connect(_ host: Host, terminalSize: (cols: Int, rows: Int)?, onData: @escaping @Sendable (Data) -> Void, onState: @escaping @Sendable (ConnectionState) -> Void) async throws {
        guard host.isConfigured else { throw AppError.invalidHost }
        guard let keyID = host.keyID else { throw AppError.noKey }
        disconnect()
        onState(.connecting)
        let privateKey = try keys.privateKey(id: keyID)
        onState(.authenticating)
        let connected: SSHClient
        do {
            connected = try await SSHClient.connect(host: host.hostname, port: host.port, authenticationMethod: .ed25519(username: host.username, privateKey: privateKey), hostKeyValidator: .custom(KnownHostValidator(expected: host.knownFingerprint)), reconnect: .never, connectTimeout: .seconds(15))
        } catch { throw error }
        stateLock.withLock { client = connected }
        onState(.connected)
        let task = Task { [weak self] in
            let columns = max(2, terminalSize?.cols ?? 80)
            let rows = max(1, terminalSize?.rows ?? 24)
            do {
                try await connected.withPTY(.init(wantReply: true, term: "xterm-256color", terminalCharacterWidth: columns, terminalRowHeight: rows, terminalPixelWidth: 0, terminalPixelHeight: 0, terminalModes: .init([.ECHO: 1]))) { inbound, outbound in
                    self?.stateLock.withLock { self?.writer = outbound }
                    for try await output in inbound {
                        switch output {
                        case .stdout(let buffer), .stderr(let buffer):
                            var copy = buffer
                            if let bytes = copy.readBytes(length: copy.readableBytes) { onData(Data(bytes)) }
                        }
                    }
                }
            } catch { onState(.failed(error.localizedDescription)) }
            try? await connected.close()
            self?.stateLock.withLock { self?.writer = nil; self?.client = nil }
        }
        stateLock.withLock { terminalTask = task }
    }

    func installKey(host: Host, password: String, publicKey: String) async throws {
        guard host.isConfigured else { throw AppError.invalidHost }
        let client = try await SSHClient.connect(host: host.hostname, port: host.port, authenticationMethod: .passwordBased(username: host.username, password: password), hostKeyValidator: .custom(KnownHostValidator(expected: host.knownFingerprint)), reconnect: .never, connectTimeout: .seconds(15))
        do {
            _ = try await client.executeCommand(AuthorizedKeys.installCommand(for: publicKey), mergeStreams: true)
            try? await client.close()
        } catch {
            try? await client.close()
            throw error
        }
    }

    func send(_ data: Data) { guard let writer = stateLock.withLock({ writer }) else { return }; Task { try? await writer.write(ByteBuffer(data: data)) } }
    func resize(cols: Int, rows: Int) { guard let writer = stateLock.withLock({ writer }) else { return }; Task { try? await writer.changeSize(cols: max(2, cols), rows: max(1, rows), pixelWidth: 0, pixelHeight: 0) } }
    func disconnect() {
        let values = stateLock.withLock { () -> (Task<Void, Never>?, SSHClient?) in let values = (terminalTask, client); terminalTask = nil; client = nil; writer = nil; return values }
        values.0?.cancel()
        if let client = values.1 { Task { try? await client.close() } }
    }
}
