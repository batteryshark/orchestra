import Foundation
import SwiftUI

struct ProjectDirectory: Decodable, Sendable {
    let defaultProjectId: String?
    let projects: [OrchestraProject]
}

struct OrchestraProject: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let root: String
    let available: Bool?

    var isAvailable: Bool { available ?? true }
}

struct DashboardState: Decodable, Sendable {
    let root: String
    let projectId: String
    let runs: [Run]
    let messages: [InboxMessage]
    let feed: [Finding]
    let teams: [OrchestraTeam]
    let ensemble: [EnsembleTeam]
}

struct Run: Decodable, Identifiable, Hashable, Sendable {
    let id: Int
    let agent: String
    let backend: String
    let model: String?
    let title: String?
    let workItem: String?
    let team: String?
    let requestedBy: String
    let branch: String?
    let parentRun: Int?
    let leadRun: Int?
    let childDepth: Int?
    let sessionRef: String?
    let status: String
    let exitCode: Int?
    let summary: String?
    let startedAt: String
    let finishedAt: String?
    let slug: String?

    var isTerminal: Bool {
        ["done", "failed", "timeout", "killed"].contains(status)
    }

    var isWaiting: Bool { status == "waiting_input" }

    var displayName: String {
        if let slug, !slug.isEmpty { return slug.replacingOccurrences(of: "_", with: " ") }
        return "Run #\(id)"
    }

    var modelDisplayName: String {
        guard let model, !model.isEmpty else { return backend }
        return model.split(separator: "/").last.map(String.init) ?? model
    }

    var statusColor: Color {
        switch status {
        case "done": .green
        case "failed", "timeout": .red
        case "killed": .secondary
        case "waiting_input", "spawning": .orange
        default: .blue
        }
    }
}

struct InboxMessage: Decodable, Identifiable, Sendable {
    let id: Int
    let sender: String
    let recipient: String
    let body: String
    let workItem: String?
    let runId: Int?
    let kind: String?
    let createdAt: String
    let readAt: String?

    var isUnread: Bool { readAt == nil }
}

struct Finding: Decodable, Identifiable, Sendable {
    let id: Int
    let author: String
    let body: String
    let tags: String?
    let workItem: String?
    let runId: Int?
    let createdAt: String
}

struct OrchestraTeam: Decodable, Hashable, Sendable {
    let name: String
    let members: [String]
}

struct EnsembleTeam: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let name: String
    let status: String
    let leadSession: String?
    let members: [EnsembleMember]
    let tasks: [EnsembleTask]
}

struct EnsembleMember: Decodable, Identifiable, Hashable, Sendable {
    let name: String
    let model: String?
    let status: String
    let executionStatus: String?
    let sessionId: String

    var id: String { sessionId }
}

struct EnsembleTask: Decodable, Hashable, Sendable {
    let content: String
    let status: String
    let priority: String?
    let assignee: String?
}

struct TranscriptResponse: Decodable, Sendable {
    let etag: String
    let unchanged: Bool?
    let run: Run?
    let items: [TranscriptItem]?
}

struct TeammateTranscriptResponse: Decodable, Sendable {
    let etag: String
    let unchanged: Bool?
    let items: [TranscriptItem]?
    let messages: [EnsembleMessage]?
}

struct TranscriptItem: Decodable, Sendable {
    let kind: String
    let body: String?
    let name: String?
    let status: String?
    let input: String?
    let output: String?
    let delivery: String?
    let sender: String?
    let recipient: String?
    let createdAt: String?
    let phase: String?
    let question: String?
    let recommendedDefault: String?
    let answer: String?
    let answeredBy: String?
    let deadlineAt: String?
}

struct EnsembleMessage: Decodable, Sendable {
    let fromName: String
    let toName: String
    let content: String
    let timeCreated: Int?
}

struct UsageSnapshot: Decodable, Sendable {
    let generatedAt: String
    let status: String
    let providers: [UsageProvider]
}

struct UsageProvider: Decodable, Identifiable, Sendable {
    let id: String
    let name: String
    let status: String
    let plan: String?
    let windows: [QuotaWindow]
    let message: String?
    let source: String?
    let rateLimitResets: RateLimitResetCredits?
    let fetchedAt: String
    let headroomPercent: Double?
}

struct QuotaWindow: Decodable, Identifiable, Sendable {
    let id: String
    let label: String
    let scope: String
    let usedPercent: Double
    let remainingPercent: Double
    let resetsAt: String?
    let burnRatePercentPerHour: Double?
}

struct RateLimitResetCredits: Decodable, Sendable {
    let availableCount: Int
    let title: String?
    let expiresAt: String?
}

struct RuntimeStats: Decodable, Sendable {
    let generatedAt: String
    let totalSeconds: Int
    let totalRuns: Int
    let timedRuns: Int
    let activeRuns: Int
    let ignoredRuns: Int
    let byAgent: [AgentRuntime]
    let byModel: [ModelRuntime]
}

struct AgentRuntime: Decodable, Identifiable, Sendable {
    let agent: String
    let seconds: Int
    let runs: Int
    let activeRuns: Int
    let models: [String]
    let backends: [String]
    let role: String

    var id: String { agent }
}

struct ModelRuntime: Decodable, Identifiable, Sendable {
    let backend: String
    let model: String
    let seconds: Int
    let runs: Int
    let activeRuns: Int
    let agents: [String]

    var id: String { "\(backend):\(model)" }
}

enum OrchestraFormatting {
    static let dateParser: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    static let fallbackDateParser = ISO8601DateFormatter()

    static func date(from value: String?) -> Date? {
        guard let value, !value.isEmpty else { return nil }
        return dateParser.date(from: value) ?? fallbackDateParser.date(from: value)
    }

    static func elapsed(from start: String, to finish: String?, now: Date = .now) -> String {
        guard let started = date(from: start) else { return "—" }
        let ended = date(from: finish) ?? now
        return duration(max(0, Int(ended.timeIntervalSince(started))))
    }

    static func duration(_ seconds: Int) -> String {
        let hours = seconds / 3_600
        let minutes = (seconds % 3_600) / 60
        if hours > 0 { return "\(hours)h \(minutes)m" }
        if minutes > 0 { return "\(minutes)m" }
        return "\(seconds)s"
    }

    static func timestamp(_ value: String?) -> String {
        guard let date = date(from: value) else { return value ?? "" }
        return date.formatted(date: .omitted, time: .shortened)
    }
}
