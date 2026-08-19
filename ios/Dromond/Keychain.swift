import Foundation
import Security

enum Keychain {
    private static let service = "com.batteryshark.dromond"
    /// Pre-rename service name. Read once, then migrated to `service`.
    private static let legacyService = "com.batteryshark.maestro"
    /// The account for the single server the app used to hold. It is still the
    /// account of the FIRST server after migration, so an upgrading phone keeps
    /// its key without being asked to type it again.
    static let legacyAccount = "shared-secret"

    /// One server's key. The account is the server's id, so several daemons
    /// can each keep their own secret on the same phone.
    static func load(for account: String) -> String {
        read(service: service, account: account) ?? ""
    }

    static func save(_ value: String, for account: String) throws {
        try write(value, service: service, account: account)
    }

    static func delete(account: String) {
        SecItemDelete(identity(service: service, account: account) as CFDictionary)
    }

    /// The pre-multi-server key, read once so migration can hand it to the
    /// first server. Also carries the pre-rename service migration.
    static func load() -> String {
        if let value = read(service: service, account: legacyAccount) { return value }
        guard let legacy = read(service: legacyService, account: legacyAccount)
        else { return "" }
        // Rewrite under the new service, then drop the old item so this runs once.
        // A failed save leaves the old item in place, so the next launch retries.
        if (try? save(legacy, for: legacyAccount)) != nil {
            SecItemDelete(identity(service: legacyService,
                                   account: legacyAccount) as CFDictionary)
        }
        return legacy
    }

    private static func read(service: String, account: String) -> String? {
        var query = identity(service: service, account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let value = String(data: data, encoding: .utf8), !value.isEmpty else { return nil }
        return value
    }

    private static func identity(service: String, account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    private static func write(_ value: String, service: String,
                              account: String) throws {
        let identity = identity(service: service, account: account)
        guard !value.isEmpty else {
            let status = SecItemDelete(identity as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else {
                throw KeychainError.saveFailed(status)
            }
            return
        }
        let attributes: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        var status = SecItemUpdate(identity as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var item = identity
            attributes.forEach { item[$0] = $1 }
            status = SecItemAdd(item as CFDictionary, nil)
        }
        guard status == errSecSuccess else {
            throw KeychainError.saveFailed(status)
        }
    }
}

enum KeychainError: LocalizedError {
    case saveFailed(OSStatus)

    var errorDescription: String? {
        switch self {
        case let .saveFailed(status): "Could not save the Dromond key (\(status))."
        }
    }
}
