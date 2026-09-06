import Crypto
import Foundation
import XCTest
@testable import DevBoost

private final class InMemorySecretStore: SecretStore, @unchecked Sendable {
    var values: [String: Data] = [:]
    var failWrites = false

    func set(_ value: Data, for key: String) throws {
        if failWrites { throw KeychainError.failure }
        values[key] = value
    }

    func data(for key: String) throws -> Data? { values[key] }
    func remove(_ key: String) throws { values[key] = nil }
}

final class SecurityTests: XCTestCase {
    func testGeneratedKeyCanBeReadBackAndPublicKeyIsOpenSSHFormatted() throws {
        let store = InMemorySecretStore()
        let key = try KeyManager(keychain: store).generate(comment: "My server  ")
        XCTAssertTrue(key.publicKey.hasPrefix("ssh-ed25519 "))
        XCTAssertTrue(key.publicKey.hasSuffix(" My server"))
        XCTAssertEqual(try KeyManager(keychain: store).publicKey(id: key.id), key.publicKey)
        XCTAssertEqual(try KeyManager(keychain: store).privateKey(id: key.id).rawRepresentation.count, 32)
    }

    func testKeyCommentCollapsesWhitespaceAndOmitsEmptyComment() throws {
        let store = InMemorySecretStore()
        let manager = KeyManager(keychain: store)
        let spaced = try manager.generate(comment: "  release\nserver   key ")
        let empty = try manager.generate(comment: " \n\t ")
        XCTAssertTrue(spaced.publicKey.hasSuffix(" release server key"))
        XCTAssertFalse(empty.publicKey.hasSuffix(" "))
    }

    func testFailedKeyWriteRollsBackBothSecrets() {
        let store = InMemorySecretStore()
        store.failWrites = true
        XCTAssertThrowsError(try KeyManager(keychain: store).generate(comment: "server"))
        XCTAssertTrue(store.values.isEmpty)
    }

    func testMissingOrCorruptKeysMapToNoKey() throws {
        let store = InMemorySecretStore()
        let manager = KeyManager(keychain: store)
        XCTAssertThrowsError(try manager.publicKey(id: "missing")) { error in
            guard case .noKey? = error as? AppError else { return XCTFail("Missing public key should report noKey") }
        }
        store.values["private-key-bad"] = Data([1, 2, 3])
        XCTAssertThrowsError(try manager.privateKey(id: "bad")) { error in
            guard case .noKey? = error as? AppError else { return XCTFail("Corrupt private key should report noKey") }
        }
    }

    func testAuthorizedKeysCommandIsIdempotentAndDoesNotInterpolateRawKey() {
        let key = "ssh-ed25519 abc+/= comment; touch /tmp/should-not-run"
        let command = AuthorizedKeys.installCommand(for: key)
        XCTAssertTrue(command.contains("grep -Fqx -- \"$key\""))
        XCTAssertTrue(command.contains("base64 -d"))
        XCTAssertFalse(command.contains("printf '%s' '\(key)'"))
        XCTAssertTrue(command.contains(Data(key.utf8).base64EncodedString()))
    }
}
