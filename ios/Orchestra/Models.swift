import Foundation

struct Server: Codable, Identifiable, Hashable, Sendable {
    var id: UUID
    var label: String
    var url: String
    var keyAccount: String
    var instanceID: String?

    init(id: UUID = UUID(), label: String, url: String,
         keyAccount: String? = nil, instanceID: String? = nil) {
        self.id = id
        self.label = label.trimmingCharacters(in: .whitespacesAndNewlines)
        self.url = url
        self.keyAccount = keyAccount ?? id.uuidString
        self.instanceID = instanceID
    }

    var displayName: String {
        if !label.isEmpty { return label }
        return URL(string: url)?.host ?? "Orchestra"
    }
}

enum JSONValue: Codable, Hashable, Sendable, CustomStringConvertible {
    case string(String), number(Double), bool(Bool), object([String: JSONValue])
    case array([JSONValue]), null

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let decoded = try? value.decode(Bool.self) { self = .bool(decoded) }
        else if let decoded = try? value.decode(Double.self) { self = .number(decoded) }
        else if let decoded = try? value.decode(String.self) { self = .string(decoded) }
        else if let decoded = try? value.decode([String: JSONValue].self) { self = .object(decoded) }
        else { self = .array(try value.decode([JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case let .string(v): try value.encode(v)
        case let .number(v): try value.encode(v)
        case let .bool(v): try value.encode(v)
        case let .object(v): try value.encode(v)
        case let .array(v): try value.encode(v)
        case .null: try value.encodeNil()
        }
    }

    var description: String {
        switch self {
        case let .string(value): value
        case let .number(value): value.rounded() == value ? String(Int(value)) : String(value)
        case let .bool(value): String(value)
        case let .array(value): value.map(\.description).joined(separator: ", ")
        case let .object(value):
            value.sorted { $0.key < $1.key }
                .map { "\($0.key): \($0.value.description)" }.joined(separator: "\n")
        case .null: ""
        }
    }
}

struct APIPage<Element: Decodable & Sendable>: Decodable, Sendable {
    let items: [Element]
    let nextCursor: String?
    let resumeCursor: String?
    let hasMore: Bool

    enum CodingKeys: String, CodingKey {
        case items
        case nextCursor = "next_cursor"
        case resumeCursor = "resume_cursor"
        case hasMore = "has_more"
    }

    init(items: [Element] = [], nextCursor: String? = nil,
         resumeCursor: String? = nil, hasMore: Bool = false) {
        self.items = items
        self.nextCursor = nextCursor
        self.resumeCursor = resumeCursor
        self.hasMore = hasMore
    }

    init(from decoder: Decoder) throws {
        let value = try decoder.container(keyedBy: CodingKeys.self)
        items = (try? value.decode([Element].self, forKey: .items)) ?? []
        nextCursor = try? value.decode(String.self, forKey: .nextCursor)
        resumeCursor = try? value.decode(String.self, forKey: .resumeCursor)
        hasMore = (try? value.decode(Bool.self, forKey: .hasMore)) ?? (nextCursor != nil)
    }
}

struct RawLogTail: Sendable {
    let text: String
    let partial: Bool
    let byteCount: Int
}

struct FleetSnapshot: Decodable, Sendable {
    let generatedAt: String?
    let instance: FleetInstance
    let daemon: DaemonState
    let scheduler: SchedulerState
    let counts: FleetCounts
    let inbox: InboxCounts
    let messages: MessageCounts
    let observer: ObserverSettings?
    let storage: StorageStats?

    enum CodingKeys: String, CodingKey {
        case instance, daemon, scheduler, counts, inbox, messages, observer, storage
        case generatedAt = "generated_at"
    }

    init(from decoder: Decoder) throws {
        let value = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = try? value.decode(String.self, forKey: .generatedAt)
        instance = (try? value.decode(FleetInstance.self, forKey: .instance)) ?? .init()
        daemon = (try? value.decode(DaemonState.self, forKey: .daemon)) ?? .init()
        scheduler = (try? value.decode(SchedulerState.self, forKey: .scheduler)) ?? .init()
        counts = (try? value.decode(FleetCounts.self, forKey: .counts)) ?? .init()
        inbox = (try? value.decode(InboxCounts.self, forKey: .inbox)) ?? .init()
        messages = (try? value.decode(MessageCounts.self, forKey: .messages)) ?? .init()
        observer = try? value.decode(ObserverSettings.self, forKey: .observer)
        storage = try? value.decode(StorageStats.self, forKey: .storage)
    }
}

struct FleetInstance: Codable, Hashable, Sendable {
    var name = "Orchestra"
    var platform: String?
}

struct DaemonState: Codable, Hashable, Sendable {
    var status = "unknown"
    var healthy = false
    var lastTickAt: String?

    enum CodingKeys: String, CodingKey {
        case status, healthy
        case lastTickAt = "last_tick_at"
    }
}

struct SchedulerState: Codable, Hashable, Sendable {
    var paused = false
    var active = 0
    var queued = 0
    var maxActive = 0

    enum CodingKeys: String, CodingKey {
        case paused, active, queued
        case maxActive = "max_active"
    }
}

struct FleetCounts: Codable, Hashable, Sendable {
    var runsTotal = 0
    var runsActive = 0
    var runsQueued = 0

    enum CodingKeys: String, CodingKey {
        case runsTotal = "runs_total"
        case runsActive = "runs_active"
        case runsQueued = "runs_queued"
    }
}

struct InboxCounts: Codable, Hashable, Sendable {
    var open = 0
    var blocking = 0
}

struct MessageCounts: Codable, Hashable, Sendable {
    var total = 0
    var pending = 0
    var delivered = 0
    var undeliverable = 0
    var inbound = 0
    var outbound = 0
    var system = 0
}

struct RunGroup: Codable, Identifiable, Hashable, Sendable {
    let id: String
    var name: String
    var slug: String
    var archived: Bool
    var nextNumber: Int?
    var runsCount: Int?
    var cwdConfigured: Bool?

    enum CodingKeys: String, CodingKey {
        case id, name, slug, archived
        case nextNumber = "next_number"
        case runsCount = "runs_count"
        case cwdConfigured = "cwd_configured"
    }
}

struct RuntimeConfig: Codable, Identifiable, Hashable, Sendable {
    let id: String
    var name: String
    var kind: String
    var argv: [String]
    var configConfigured: Bool? = nil
    var enabled: Bool
    var supportsSteering: Bool?
    var supportsInterrupt: Bool?

    enum CodingKeys: String, CodingKey {
        case id, name, kind, argv, enabled
        case configConfigured = "config_configured"
        case supportsSteering = "supports_steering"
        case supportsInterrupt = "supports_interrupt"
    }
}

struct Profile: Codable, Identifiable, Hashable, Sendable {
    let id: String
    var name: String
    var runtimeID: String
    var model: String?
    var effort: String?
    var tier: Int
    var priority: Int
    var sandbox: String? = nil
    var timeoutSeconds: Int? = nil
    var activeCap: Int?
    var runwaySourceID: String?
    var envConfigured: Bool? = nil
    var configConfigured: Bool? = nil
    var observerCompatible: Bool? = nil
    var observerIncompatibility: String? = nil
    var note: String?
    var enabled: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, model, effort, tier, priority, sandbox, note, enabled
        case runtimeID = "runtime_id"
        case timeoutSeconds = "timeout_seconds"
        case activeCap = "active_cap"
        case runwaySourceID = "runway_source_id"
        case envConfigured = "env_configured"
        case configConfigured = "config_configured"
        case observerCompatible = "observer_compatible"
        case observerIncompatibility = "observer_incompatibility"
    }

    var observerReady: Bool { observerCompatible == true }

    var tierName: String {
        switch tier {
        case 1: "Workhorse"
        case 2: "Core"
        case 3: "Frontier"
        default: "Tier \(tier)"
        }
    }

    var observerIssue: String? {
        guard !observerReady else { return nil }
        return observerIncompatibility
            ?? "This fleet did not report a tool-free Observer posture for this profile."
    }
}

enum ReplacementObjectError: LocalizedError {
    case invalid(String)

    var errorDescription: String? {
        switch self {
        case let .invalid(label): "\(label) must be a JSON object."
        }
    }
}

func replacementObject(_ text: String, label: String) throws -> [String: JSONValue]? {
    let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !value.isEmpty else { return nil }
    guard let data = value.data(using: .utf8),
          let object = try? JSONDecoder().decode([String: JSONValue].self, from: data) else {
        throw ReplacementObjectError.invalid(label)
    }
    return object
}

func replacementStringMap(_ text: String, label: String) throws -> [String: String]? {
    let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !value.isEmpty else { return nil }
    guard let data = value.data(using: .utf8),
          let object = try? JSONDecoder().decode([String: String].self, from: data) else {
        throw ReplacementObjectError.invalid(label)
    }
    return object
}

struct RunHold: Codable, Hashable, Sendable {
    let kind: String
    let detail: String?
}

struct Usage: Codable, Hashable, Sendable {
    var inputTokens: Int?
    var outputTokens: Int?
    var totalTokens: Int?
    var costUSD: Double?

    enum CodingKeys: String, CodingKey {
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case totalTokens = "total_tokens"
        case costUSD = "cost_usd"
    }
}

struct RunStatistics: Codable, Hashable, Sendable {
    let runs: Int
    let agentSeconds: Int?
    let byStatus: [String: Int]
    let workerUsage: Usage?
    let observerUsage: Usage?
    let combinedUsage: Usage?

    enum CodingKeys: String, CodingKey {
        case runs
        case agentSeconds = "agent_seconds"
        case byStatus = "by_status"
        case workerUsage = "worker_usage"
        case observerUsage = "observer_usage"
        case combinedUsage = "combined_usage"
    }
}

struct Run: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let display: String?
    let groupID: String
    let groupNumber: Int
    let profileID: String
    let requestID: String?
    let title: String?
    let context: String?
    let status: String
    let hold: RunHold?
    let waitingKind: String?
    let parentRunID: Int?
    let retryOf: Int?
    let continuationOf: Int?
    let cwdSource: String?
    let queuedAt: String?
    let startedAt: String?
    let finishedAt: String?
    let summary: String?
    let result: JSONValue?
    let failure: JSONValue?
    let usage: Usage?
    let observerUsage: Usage?
    let combinedUsage: Usage?
    let profileSnapshot: JSONValue?
    let runtimeSnapshot: JSONValue?
    let revision: Int?

    enum CodingKeys: String, CodingKey {
        case id, display, title, context, status, hold
        case summary, result, failure, usage, revision
        case groupID = "group_id"
        case groupNumber = "group_number"
        case profileID = "profile_id"
        case requestID = "request_id"
        case waitingKind = "waiting_kind"
        case parentRunID = "parent_run_id"
        case retryOf = "retry_of"
        case continuationOf = "continuation_of"
        case queuedAt = "queued_at"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case observerUsage = "observer_usage"
        case combinedUsage = "combined_usage"
        case profileSnapshot = "profile_snapshot"
        case runtimeSnapshot = "runtime_snapshot"
        case cwdSource = "cwd_source"
    }

    static let terminalStates: Set<String> = [
        "completed", "failed", "timed_out", "stopped", "skipped",
    ]
    var isTerminal: Bool { Self.terminalStates.contains(status) }
    var isLive: Bool { !isTerminal }
    var resultText: String { summary ?? result?.description ?? failure?.description ?? "" }
}

struct RunAdmission: Decodable, Sendable {
    let created: Bool
    let run: Run
}

struct AttentionChoice: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let label: String

    init(from decoder: Decoder) throws {
        let single = try decoder.singleValueContainer()
        if let text = try? single.decode(String.self) {
            id = text
            label = text
            return
        }
        let object = try single.decode([String: String].self)
        id = object["id"] ?? object["value"] ?? object["label"] ?? "choice"
        label = object["label"] ?? object["value"] ?? id
    }
}

struct AttentionItem: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let correlationID: String?
    let runID: Int?
    let kind: String
    let state: String
    let prompt: String?
    let message: String?
    let detail: String?
    let blocking: Bool
    let choices: [AttentionChoice]
    let deadline: String?
    let fallback: JSONValue?
    let openedAt: String?
    let resolution: JSONValue?

    enum CodingKeys: String, CodingKey {
        case id, kind, state, prompt, message, detail
        case blocking, choices, deadline, fallback, resolution
        case correlationID = "correlation_id"
        case runID = "run_id"
        case openedAt = "opened_at"
    }
}

struct RunMessage: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let runID: Int
    let display: String?
    let sender: String?
    let direction: String?
    let kind: String?
    let status: String
    let body: String
    let correlationID: String?
    let replyTo: Int?
    let createdAt: String?
    let deliveredAt: String?
    let undeliverableAt: String?
    let deliveryError: String?

    enum CodingKeys: String, CodingKey {
        case id, display, sender, direction, kind, status, body
        case runID = "run_id"
        case correlationID = "correlation_id"
        case replyTo = "reply_to"
        case createdAt = "created_at"
        case deliveredAt = "delivered_at"
        case undeliverableAt = "undeliverable_at"
        case deliveryError = "delivery_error"
    }
}

struct RunEvent: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let kind: String
    let name: String?
    let text: String?
    let payload: JSONValue?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, name, text, payload
        case createdAt = "created_at"
    }
}

struct Artifact: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let runID: Int
    let name: String
    let mediaType: String
    let byteSize: Int
    let sha256: String
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, name, sha256
        case runID = "run_id"
        case mediaType = "media_type"
        case byteSize = "byte_size"
        case createdAt = "created_at"
    }
}

struct GitCheckpoint: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let commit: String?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, commit
        case createdAt = "created_at"
    }

}

struct RunChanges: Codable, Hashable, Sendable {
    let branch: String?
    let base: String?
    let head: String?
    let patch: String?
    let diff: String?
    let truncated: Bool?
    let checkpoints: [GitCheckpoint]

}

struct RunLineage: Codable, Sendable {
    let rootRunID: Int?
    let items: [Run]

    enum CodingKeys: String, CodingKey {
        case items
        case rootRunID = "root_run_id"
    }
}

struct ObserverCheck: Codable, Identifiable, Hashable, Sendable {
    let id: Int
    let judgment: String?
    let action: String
    let rationale: String?
    let evidenceFrom: Int?
    let evidenceTo: Int?
    let usage: Usage?
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, judgment, action, rationale, usage
        case evidenceFrom = "evidence_from"
        case evidenceTo = "evidence_to"
        case createdAt = "created_at"
    }
}

struct ObserverRunDetail: Codable, Sendable {
    let checks: [ObserverCheck]
    let usage: Usage?
}

struct RunwayWindow: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let remainingPercent: Double?
    let resetsAt: String?

    enum CodingKeys: String, CodingKey {
        case id, name
        case remainingPercent = "remaining_percent"
        case resetsAt = "resets_at"
    }
}

struct RunwaySource: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let provider: String
    let account: String
    let lane: String?
    let adapter: String
    let enabled: Bool
    let archived: Bool
    let argvConfigured: Bool
    let configConfigured: Bool
    let status: String
    let fresh: Bool
    let observedAt: String?
    let burnRate: Double?
    let windows: [RunwayWindow]
    let linkedProfileIDs: [String]
    let history: [RunwayObservation]?

    enum CodingKeys: String, CodingKey {
        case id, name, provider, account, lane, adapter, enabled, archived
        case status, fresh, windows, history
        case argvConfigured = "argv_configured"
        case configConfigured = "config_configured"
        case observedAt = "observed_at"
        case burnRate = "burn_rate"
        case linkedProfileIDs = "linked_profile_ids"
    }
}

struct RunwaySourceDraft: Sendable {
    var name: String
    var provider: String
    var account: String
    var lane: String
    var adapter: String
    var enabled: Bool
    var argv: [String]?
    var config: [String: JSONValue]?
}

struct RunwayObservation: Codable, Hashable, Sendable {
    let observedAt: String?
    let burnRate: Double?
    let windows: [RunwayWindow]

    enum CodingKeys: String, CodingKey {
        case windows
        case observedAt = "observed_at"
        case burnRate = "burn_rate"
    }
}

struct ObserverSettings: Codable, Hashable, Sendable {
    var profileID: String?
    var concurrency: Int
    var firstCheckSeconds: Int
    var minimumEvents: Int?
    var subsequentCheckSeconds: Int?
    var authority: String? = nil

    enum CodingKeys: String, CodingKey {
        case concurrency, authority
        case profileID = "profile_id"
        case firstCheckSeconds = "first_check_seconds"
        case minimumEvents = "minimum_events"
        case subsequentCheckSeconds = "subsequent_check_seconds"
    }
}

struct StorageStats: Codable, Hashable, Sendable {
    let databaseBytes: Int?
    let logBytes: Int?
    let artifactBytes: Int?
    let checkpointBytes: Int?

    enum CodingKeys: String, CodingKey {
        case databaseBytes = "database_bytes"
        case logBytes = "log_bytes"
        case artifactBytes = "artifact_bytes"
        case checkpointBytes = "checkpoint_bytes"
    }
}

struct FleetSetting: Codable, Identifiable, Hashable, Sendable {
    let key: String
    let value: JSONValue
    let revision: Int
    let updatedBy: String?
    let updatedAt: String?

    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, value, revision
        case updatedBy = "updated_by"
        case updatedAt = "updated_at"
    }
}

struct Device: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let label: String
    let createdAt: String?
    let lastUsedAt: String?
    let revokedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, label
        case createdAt = "created_at"
        case lastUsedAt = "last_used_at"
        case revokedAt = "revoked_at"
    }
}

struct ServiceTokenRecord: Codable, Identifiable, Hashable, Sendable {
    let id: String
    let label: String
    let authorities: [String]
    let createdAt: String?
    let revokedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, label, authorities
        case createdAt = "created_at"
        case revokedAt = "revoked_at"
    }
}

struct ServiceTokenCreation: Decodable, Sendable {
    let token: String
    let serviceToken: ServiceTokenRecord?

    enum CodingKeys: String, CodingKey {
        case token
        case serviceToken = "service_token"
    }
}

struct PairingCode: Codable, Sendable {
    let pairingID: String?
    let code: String
    let pairingURI: String?
    let expiresAt: String

    enum CodingKeys: String, CodingKey {
        case code
        case pairingID = "pairing_id"
        case pairingURI = "pairing_uri"
        case expiresAt = "expires_at"
    }
}

struct PairingRedemption: Codable, Sendable {
    let token: String
    let device: Device?
}

struct DiscoveryResult<Value: Codable & Sendable>: Codable, Sendable {
    let data: Value?
    let error: String?
}

struct CodexDiscoveredModel: Codable, Identifiable, Sendable {
    let model: String
    let efforts: [String]
    let defaultEffort: String?

    var id: String { model }

    enum CodingKeys: String, CodingKey {
        case model, efforts
        case defaultEffort = "default_effort"
    }
}

struct ReasonixDiscoveredProvider: Codable, Identifiable, Sendable {
    let provider: String
    let models: [String]
    let efforts: [String]
    let defaultEffort: String?

    var id: String { provider }

    enum CodingKeys: String, CodingKey {
        case provider, models, efforts
        case defaultEffort = "default_effort"
    }
}

struct LocalDiscoveredModel: Codable, Identifiable, Sendable {
    let id: String
    let source: String
}

struct ProfileDiscoveryRuntimes: Codable, Sendable {
    let opencode: DiscoveryResult<[String: [String]]>
    let codex: DiscoveryResult<[CodexDiscoveredModel]>
    let reasonix: DiscoveryResult<[ReasonixDiscoveredProvider]>
    let claude: DiscoveryResult<JSONValue>
}

struct ProfileDiscovery: Codable, Sendable {
    let runtimes: ProfileDiscoveryRuntimes
    let localRequested: Bool
    let localModels: [LocalDiscoveredModel]

    enum CodingKeys: String, CodingKey {
        case runtimes
        case localRequested = "local_requested"
        case localModels = "local_models"
    }
}

struct PairingClaim: Equatable, Sendable {
    let endpoint: String?
    let pairingID: String?
    let code: String

    init(endpoint: String?, pairingID: String? = nil, code: String) {
        self.endpoint = endpoint
        self.pairingID = pairingID
        self.code = code
    }

    static func parse(_ input: String) throws -> PairingClaim {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { throw APIError.invalidPairingCode }
        guard text.contains("://") else { return .init(endpoint: nil, code: text) }
        guard let components = URLComponents(string: text),
              let code = components.queryItems?.first(where: { $0.name == "code" })?.value,
              !code.isEmpty else { throw APIError.invalidPairingCode }
        let endpoint = components.queryItems?.first(where: { $0.name == "endpoint" })?.value
        let pairingID = components.queryItems?.first(where: { $0.name == "pairing_id" })?.value
        return .init(endpoint: endpoint, pairingID: pairingID, code: code)
    }
}

struct SSEMessage: Equatable, Sendable {
    var id: String?
    var event = "message"
    var data = ""
}

struct SSEDecoder: Sendable {
    private var message = SSEMessage()

    mutating func feed(line: String) -> SSEMessage? {
        if line.isEmpty {
            guard !message.data.isEmpty else { return nil }
            if message.data.last == "\n" { message.data.removeLast() }
            defer { message = SSEMessage() }
            return message
        }
        guard !line.hasPrefix(":") else { return nil }
        let split = line.firstIndex(of: ":")
        let field = split.map { String(line[..<$0]) } ?? line
        var value = split.map { String(line[line.index(after: $0)...]) } ?? ""
        if value.first == " " { value.removeFirst() }
        switch field {
        case "id": message.id = value
        case "event": message.event = value
        case "data": message.data += value + "\n"
        default: break
        }
        return nil
    }
}

enum APIError: LocalizedError, Equatable {
    case invalidURL
    case invalidResponse
    case invalidPairingCode
    case apiVersion(Int?)
    case instanceChanged(expected: String, received: String)
    case http(Int, String)
    case malformedEnvelope

    var errorDescription: String? {
        switch self {
        case .invalidURL: "Enter a full http:// or https:// Orchestra address."
        case .invalidResponse: "Orchestra returned an invalid HTTP response."
        case .invalidPairingCode: "Enter a valid pairing code or Orchestra pairing URI."
        case let .apiVersion(version): "This app requires Orchestra API v2, not v\(version.map(String.init) ?? "unknown")."
        case let .instanceChanged(expected, received):
            "This address now identifies a different Orchestra instance (\(expected.prefix(8)) → \(received.prefix(8))). Pair it again."
        case let .http(_, message): message
        case .malformedEnvelope: "Orchestra returned a malformed v2 response."
        }
    }
}
