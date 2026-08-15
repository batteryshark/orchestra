import Foundation
import Security

enum Keychain {
    private static let service = "com.batteryshark.dromond"
    /// Pre-rename service name. Read once, then migrated to `service`.
    private static let legacyService = "com.batteryshark.maestro"
    private static let account = "shared-secret"

    static func load() -> String {
        if let value = read(service: service) { return value }
        guard let legacy = read(service: legacyService) else { return "" }
        // Rewrite under the new service, then drop the old item so this runs once.
        // A failed save leaves the old item in place, so the next launch retries.
        if (try? save(legacy)) != nil {
            SecItemDelete(identity(service: legacyService) as CFDictionary)
        }
        return legacy
    }

    private static func read(service: String) -> String? {
        var query = identity(service: service)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data,
              let value = String(data: data, encoding: .utf8), !value.isEmpty else { return nil }
        return value
    }

    private static func identity(service: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    static func save(_ value: String) throws {
        let identity = identity(service: service)
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
