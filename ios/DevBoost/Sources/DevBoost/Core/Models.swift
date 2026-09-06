import Foundation

struct Host: Codable, Identifiable, Equatable, Hashable {
    var id = UUID()
    var name = "Ubuntu server"
    var hostname = ""
    var port = 22
    var username = ""
    var keyID: String?
    var knownFingerprint: String?
    var keyInstalled = false

    var displayAddress: String { "\(username)@\(hostname):\(port)" }
    var isConfigured: Bool {
        !hostname.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
        !username.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
        (1...65535).contains(port)
    }
}

struct RemoteDestination: Codable, Identifiable, Hashable {
    let path: String
    var id: String { path }
}

struct TransferRecord: Codable, Identifiable, Hashable {
    let id: UUID
    let hostID: UUID
    let fileName: String
    let remotePath: String
    let createdAt: Date
    let succeeded: Bool
    let message: String
}

struct CodexUsageSnapshot: Codable, Equatable {
    var primaryUsedPercent: Double?
    var primaryResetAt: Date?
    var secondaryUsedPercent: Double?
    var secondaryResetAt: Date?
    var availableResets: Int?
    var creditsUnlimited: Bool?
    var creditBalance: String?
    var planType: String?
    var source: String?
    var updatedAt: Date?
    var message: String?

    static let empty = CodexUsageSnapshot(message: "Connect a Codex account to see live usage.")
}

enum ConnectionState: Equatable {
    case disconnected, connecting, authenticating, connected, reconnecting, failed(String)

    var title: String {
        switch self {
        case .disconnected: "Disconnected"
        case .connecting: "Connecting"
        case .authenticating: "Authenticating"
        case .connected: "Connected"
        case .reconnecting: "Reconnecting"
        case .failed(let message): message
        }
    }
}

enum AppError: LocalizedError {
    case invalidHost, noKey, firstHostKey(String), hostKeyChanged(String), connectionFailed(String)
    case invalidRemotePath

    var errorDescription: String? {
        switch self {
        case .invalidHost: "Enter a hostname, username, and valid SSH port."
        case .noKey: "Generate an SSH key before connecting."
        case .firstHostKey(let value): "This server's fingerprint is \(value). Trust it to continue."
        case .hostKeyChanged(let value): "The server host key changed: \(value)"
        case .connectionFailed(let message): message
        case .invalidRemotePath: "Choose an absolute destination folder."
        }
    }
}
