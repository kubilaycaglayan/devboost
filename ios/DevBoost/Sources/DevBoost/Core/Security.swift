import Foundation
import Security
import Crypto
import Citadel

protocol SecretStore: Sendable {
    func set(_ value: Data, for key: String) throws
    func data(for key: String) throws -> Data?
    func remove(_ key: String) throws
}

final class KeychainStore: SecretStore, @unchecked Sendable {
    private let service = "com.personal.devboost"

    func set(_ value: Data, for key: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: key]
        let status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: value] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item[kSecValueData as String] = value
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else { throw KeychainError.failure }
        } else if status != errSecSuccess { throw KeychainError.failure }
    }
    func data(for key: String) throws -> Data? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: key, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainError.failure }
        return result as? Data
    }
    func remove(_ key: String) throws { SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: key] as CFDictionary) }
}

enum KeychainError: LocalizedError { case failure; var errorDescription: String? { "Secure storage is unavailable." } }

struct StoredKey: Identifiable { let id: String; let publicKey: String }

final class KeyManager: @unchecked Sendable {
    private let keychain: any SecretStore
    init(keychain: any SecretStore) { self.keychain = keychain }

    func generate(comment: String) throws -> StoredKey {
        let key = Curve25519.Signing.PrivateKey()
        let id = UUID().uuidString
        let publicKey = Self.publicKeyText(for: key.publicKey, comment: comment)
        do {
            try keychain.set(key.rawRepresentation, for: "private-key-\(id)")
            try keychain.set(Data(publicKey.utf8), for: "public-key-\(id)")
            return StoredKey(id: id, publicKey: publicKey)
        } catch {
            remove(id: id)
            throw error
        }
    }
    func remove(id: String) { try? keychain.remove("private-key-\(id)"); try? keychain.remove("public-key-\(id)") }
    func publicKey(id: String) throws -> String {
        guard let value = try keychain.data(for: "public-key-\(id)"), let key = String(data: value, encoding: .utf8) else { throw AppError.noKey }
        return key
    }
    func privateKey(id: String) throws -> Curve25519.Signing.PrivateKey {
        guard let value = try keychain.data(for: "private-key-\(id)"), let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: value) else { throw AppError.noKey }
        return key
    }
    private static func publicKeyText(for key: Curve25519.Signing.PublicKey, comment: String) -> String {
        var blob = Data()
        append(Data("ssh-ed25519".utf8), to: &blob)
        append(key.rawRepresentation, to: &blob)
        let suffix = comment.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
        return "ssh-ed25519 \(blob.base64EncodedString())" + (suffix.isEmpty ? "" : " \(suffix)")
    }
    private static func append(_ value: Data, to output: inout Data) {
        var length = UInt32(value.count).bigEndian
        withUnsafeBytes(of: &length) { output.append(contentsOf: $0) }
        output.append(value)
    }
}

enum AuthorizedKeys {
    static func installCommand(for key: String) -> String {
        let encoded = Data(key.utf8).base64EncodedString()
        return "set -eu; umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; touch ~/.ssh/authorized_keys; key=$(printf '%s' '\(encoded)' | base64 -d); grep -Fqx -- \"$key\" ~/.ssh/authorized_keys || printf '%s\\n' \"$key\" >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
    }
}
