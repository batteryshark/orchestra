import Foundation

/// Everything `/api/snapshot` serves, in one decode.
///
/// The daemon builds this in one pass and every view reads from the same
/// object, which is why the app never disagrees with itself the way five
/// independent fetches would.
struct Snapshot: Decodable, Sendable {
    // A MINIMUM, not an equality. The snapshot grows fields as the daemon
    // grows features, and every one of those bumps the version; refusing
    // anything but one exact number meant a backend that had merely added a
    // key left the app showing "not in the correct format" with no way
    // forward. Codable ignores fields it was not asked for, so a newer
    // snapshot decodes fine. Raise this only when a field the app READS
    // changes shape or disappears.
    static let minimumVersion = 6

    let version: Int
    let generatedAt: String
    let home: String?
    let runs: [Run]
    let liveRuns: Int
    let projects: [Project]
    let profiles: [Profile]
    let runway: [Runway]
    let dispatch: Dispatch
    let daemon: Daemon
    let statistics: Statistics
    let findings: [Finding]
    let proposals: [Proposal]

    enum CodingKeys: String, CodingKey {
        case version, runs, home, projects, profiles, runway, dispatch, daemon
        case statistics, findings, proposals
        case generatedAt = "generated_at"
        case liveRuns = "live_runs"
    }

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        version = try v.decode(Int.self, forKey: .version)
        guard version >= Self.minimumVersion else {
            throw DecodingError.dataCorruptedError(
                forKey: .version, in: v,
                debugDescription: "Snapshot v\(version) is older than v\(Self.minimumVersion); update the daemon."
            )
        }
        generatedAt = try v.decode(String.self, forKey: .generatedAt)
        runs = try v.decode([Run].self, forKey: .runs)
        liveRuns = try v.decode(Int.self, forKey: .liveRuns)
        // Everything below arrived after v6. A field the app merely DISPLAYS
        // must never fail the decode, or one daemon change blanks the app.
        home = try v.decodeIfPresent(String.self, forKey: .home)
        projects = (try? v.decode([Project].self, forKey: .projects)) ?? []
        profiles = (try? v.decode([Profile].self, forKey: .profiles)) ?? []
        runway = (try? v.decode([Runway].self, forKey: .runway)) ?? []
        dispatch = (try? v.decode(Dispatch.self, forKey: .dispatch)) ?? Dispatch(paused: false, since: nil)
        daemon = (try? v.decode(Daemon.self, forKey: .daemon)) ?? Daemon()
        statistics = (try? v.decode(Statistics.self, forKey: .statistics)) ?? Statistics()
        findings = (try? v.decode([Finding].self, forKey: .findings)) ?? []
        proposals = (try? v.decode([Proposal].self, forKey: .proposals)) ?? []
    }
}

struct Run: Decodable, Identifiable, Hashable, Sendable {
    let id: Int
    let slug: String?
    let status: String
    let profile: String
    let backend: String
    let model: String?
    let title: String?
    let workItem: String?
    let project: String?
    let projectID: String?
    let requestedBy: String?
    let startedAt: String?
    let finishedAt: String?
    let elapsedSeconds: Double?
    let live: Bool
    let summary: String
    let branch: String?
    let workdir: String?
    /// Where the run started, and where it last anchored its work. The facts
    /// pane names both because a run that merged is judged on the range.
    let baseCommit: String?
    let checkpointCommit: String?
    let briefPath: String?
    let sessionRef: String?
    let parentRun: Int?
    let retryOf: Int?
    let exitCode: Int?
    /// Run ids this one waits on. A list, not a reason string.
    let blockedOn: [Int]
    let messages: [RunMessage]
    let tokensIn: Int?
    let tokensOut: Int?
    let tokensTotal: Int?
    let costUSD: Double?
    let usageSource: String?
    let billing: String?

    enum CodingKeys: String, CodingKey {
        case id, slug, status, profile, backend, model, title, project, live
        case summary, branch, workdir, messages, billing
        case workItem = "work_item"
        case baseCommit = "base_commit"
        case checkpointCommit = "checkpoint_commit"
        case briefPath = "brief_path"
        case projectID = "project_id"
        case requestedBy = "requested_by"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case elapsedSeconds = "elapsed_seconds"
        case sessionRef = "session_ref"
        case parentRun = "parent_run"
        case retryOf = "retry_of"
        case exitCode = "exit_code"
        case blockedOn = "blocked_on"
        case tokensIn = "tokens_in"
        case tokensOut = "tokens_out"
        case tokensTotal = "tokens_total"
        case costUSD = "cost_usd"
        case usageSource = "usage_source"
    }

    /// Hand-written for the same reason Snapshot's is: the synthesized
    /// initializer throws on a TYPE surprise, not just a missing key, so one
    /// field changing shape server-side would blank every run in the app.
    /// Only id/status carry the run's identity; everything else degrades.
    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        id = try v.decode(Int.self, forKey: .id)
        status = (try? v.decode(String.self, forKey: .status)) ?? "unknown"
        slug = try? v.decode(String.self, forKey: .slug)
        profile = (try? v.decode(String.self, forKey: .profile)) ?? ""
        backend = (try? v.decode(String.self, forKey: .backend)) ?? ""
        model = try? v.decode(String.self, forKey: .model)
        title = try? v.decode(String.self, forKey: .title)
        workItem = try? v.decode(String.self, forKey: .workItem)
        project = try? v.decode(String.self, forKey: .project)
        projectID = try? v.decode(String.self, forKey: .projectID)
        requestedBy = try? v.decode(String.self, forKey: .requestedBy)
        startedAt = try? v.decode(String.self, forKey: .startedAt)
        finishedAt = try? v.decode(String.self, forKey: .finishedAt)
        elapsedSeconds = try? v.decode(Double.self, forKey: .elapsedSeconds)
        live = (try? v.decode(Bool.self, forKey: .live)) ?? false
        summary = (try? v.decode(String.self, forKey: .summary)) ?? ""
        branch = try? v.decode(String.self, forKey: .branch)
        workdir = try? v.decode(String.self, forKey: .workdir)
        baseCommit = try? v.decode(String.self, forKey: .baseCommit)
        checkpointCommit = try? v.decode(String.self, forKey: .checkpointCommit)
        briefPath = try? v.decode(String.self, forKey: .briefPath)
        sessionRef = try? v.decode(String.self, forKey: .sessionRef)
        parentRun = try? v.decode(Int.self, forKey: .parentRun)
        retryOf = try? v.decode(Int.self, forKey: .retryOf)
        exitCode = try? v.decode(Int.self, forKey: .exitCode)
        blockedOn = (try? v.decode([Int].self, forKey: .blockedOn)) ?? []
        messages = (try? v.decode([RunMessage].self, forKey: .messages)) ?? []
        tokensIn = try? v.decode(Int.self, forKey: .tokensIn)
        tokensOut = try? v.decode(Int.self, forKey: .tokensOut)
        tokensTotal = try? v.decode(Int.self, forKey: .tokensTotal)
        costUSD = try? v.decode(Double.self, forKey: .costUSD)
        usageSource = try? v.decode(String.self, forKey: .usageSource)
        billing = try? v.decode(String.self, forKey: .billing)
    }

    /// Terminal runs are history; the rest are still the machine's business.
    var isTerminal: Bool {
        ["done", "failed", "killed", "timeout"].contains(status)
    }

    var displayTitle: String {
        if let title, !title.isEmpty { return title }
        if let workItem, !workItem.isEmpty { return workItem }
        return slug ?? "run \(id)"
    }
}

/// One entry in a run's thread. The merge report is a message of kind
/// `merge`, so the inbox, the outbox and the merge result are one read.
struct RunMessage: Decodable, Identifiable, Hashable, Sendable {
    let id: Int
    let sender: String?
    let body: String
    let kind: String?
    let direction: String?
    let state: String?
    let createdAt: String?
    let deliveredAt: String?
    let undeliverableReason: String?
    let pendingBoundary: Bool?

    enum CodingKeys: String, CodingKey {
        case id, sender, body, kind, direction, state
        case createdAt = "created_at"
        case deliveredAt = "delivered_at"
        case undeliverableReason = "undeliverable_reason"
        case pendingBoundary = "pending_boundary"
    }

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? v.decode(Int.self, forKey: .id)) ?? 0
        sender = try? v.decode(String.self, forKey: .sender)
        body = (try? v.decode(String.self, forKey: .body)) ?? ""
        kind = try? v.decode(String.self, forKey: .kind)
        direction = try? v.decode(String.self, forKey: .direction)
        state = try? v.decode(String.self, forKey: .state)
        createdAt = try? v.decode(String.self, forKey: .createdAt)
        deliveredAt = try? v.decode(String.self, forKey: .deliveredAt)
        undeliverableReason = try? v.decode(String.self, forKey: .undeliverableReason)
        pendingBoundary = try? v.decode(Bool.self, forKey: .pendingBoundary)
    }
}

struct Project: Decodable, Identifiable, Hashable, Sendable {
    let projectID: String
    let name: String
    let runs: Int
    let live: Int

    var id: String { projectID }

    enum CodingKeys: String, CodingKey {
        case name, runs, live
        case projectID = "project_id"
    }
}

struct Profile: Decodable, Identifiable, Hashable, Sendable {
    let name: String
    let backend: String
    let model: String
    let effort: String?
    let role: String?
    let variant: String?
    let tier: Int?
    let tierName: String?
    let priority: Int?
    let spawnProfiles: [String]
    let note: String?
    let noteAge: String?

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, backend, model, effort, role, variant, tier, priority, note
        case tierName = "tier_name"
        case spawnProfiles = "spawn_profiles"
        case noteAge = "note_age"
    }

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        name = try v.decode(String.self, forKey: .name)
        backend = try v.decode(String.self, forKey: .backend)
        model = (try? v.decode(String.self, forKey: .model)) ?? ""
        effort = try? v.decode(String.self, forKey: .effort)
        role = try? v.decode(String.self, forKey: .role)
        variant = try? v.decode(String.self, forKey: .variant)
        tier = try? v.decode(Int.self, forKey: .tier)
        tierName = try? v.decode(String.self, forKey: .tierName)
        priority = try? v.decode(Int.self, forKey: .priority)
        spawnProfiles = (try? v.decode([String].self, forKey: .spawnProfiles)) ?? []
        note = try? v.decode(String.self, forKey: .note)
        noteAge = try? v.decode(String.self, forKey: .noteAge)
    }
}

/// One provider's remaining headroom. `known == false` means the provider was
/// asked and did not say — which is not the same as zero, and must never be
/// drawn as an empty bar.
struct Runway: Decodable, Identifiable, Hashable, Sendable {
    let provider: String
    let kind: String
    let remaining: Double?
    let unit: String?
    let resetsIn: String?
    let windows: [RunwayWindow]
    /// How old the underlying reading is. Claude's comes from a cache file
    /// Claude Code writes, so it can be days behind with nothing broken.
    let ageHours: Double?

    /// Worth saying only once it is old enough to change what the number means.
    var readingAge: String? {
        guard let ageHours, ageHours >= 2 else { return nil }
        if ageHours < 48 { return "read \(Int(ageHours.rounded()))h ago" }
        return "read \(Int((ageHours / 24).rounded()))d ago"
    }
    let credits: String?
    let reason: String?
    let known: Bool

    var id: String { "\(provider)/\(kind)" }

    enum CodingKeys: String, CodingKey {
        case provider, kind, remaining, unit, windows, credits, reason, known
        case ageHours = "age_hours"
        case resetsIn = "resets_in"
    }

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        provider = try v.decode(String.self, forKey: .provider)
        kind = (try? v.decode(String.self, forKey: .kind)) ?? ""
        remaining = try? v.decode(Double.self, forKey: .remaining)
        unit = try? v.decode(String.self, forKey: .unit)
        resetsIn = try? v.decode(String.self, forKey: .resetsIn)
        windows = (try? v.decode([RunwayWindow].self, forKey: .windows)) ?? []
        ageHours = try? v.decode(Double.self, forKey: .ageHours)
        credits = try? v.decode(String.self, forKey: .credits)
        reason = try? v.decode(String.self, forKey: .reason)
        known = (try? v.decode(Bool.self, forKey: .known)) ?? false
    }

    /// Banked reset credits, already worded by the daemon — but nil when there
    /// are none. "0 banked resets" is not news, and the owner said so; the
    /// suppression lives here so every view agrees and none has to sniff the
    /// string itself.
    var creditsLabel: String? {
        guard let credits, !credits.isEmpty, !credits.hasPrefix("0 ") else { return nil }
        return credits
    }
}

struct RunwayWindow: Decodable, Identifiable, Hashable, Sendable {
    let label: String?
    let remaining: Double?
    let unit: String?
    let resetsIn: String?
    /// Why a window has no current value — set when its reset has already
    /// passed, so the reading describes a window that no longer exists.
    let staleReason: String?

    var id: String { label ?? UUID().uuidString }

    enum CodingKeys: String, CodingKey {
        case label, remaining, unit
        case resetsIn = "resets_in"
        case staleReason = "stale_reason"
    }
}

struct Dispatch: Decodable, Hashable, Sendable {
    let paused: Bool
    let since: String?
}

struct Daemon: Decodable, Hashable, Sendable {
    var pid: Int?
    var lastSweepAt: String?
    var outcome: String?
    var actions: Int?
    var released: Int?
    var reaped: Int?
    var error: String?
    var lastError: String?
    var lastErrorAt: String?
    var startedAt: String?
    var observer: Observer?

    enum CodingKeys: String, CodingKey {
        case pid, outcome, actions, released, reaped, error, observer
        case lastSweepAt = "last_sweep_at"
        case lastError = "last_error"
        case lastErrorAt = "last_error_at"
        case startedAt = "started_at"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        pid = try? v.decode(Int.self, forKey: .pid)
        lastSweepAt = try? v.decode(String.self, forKey: .lastSweepAt)
        outcome = try? v.decode(String.self, forKey: .outcome)
        actions = try? v.decode(Int.self, forKey: .actions)
        released = try? v.decode(Int.self, forKey: .released)
        reaped = try? v.decode(Int.self, forKey: .reaped)
        error = try? v.decode(String.self, forKey: .error)
        lastError = try? v.decode(String.self, forKey: .lastError)
        lastErrorAt = try? v.decode(String.self, forKey: .lastErrorAt)
        startedAt = try? v.decode(String.self, forKey: .startedAt)
        observer = try? v.decode(Observer.self, forKey: .observer)
    }
}

struct Observer: Decodable, Hashable, Sendable {
    let enabled: Bool
    let profile: String?
    let problem: String?
    let firstLook: Int?
    let interval: Int?

    enum CodingKeys: String, CodingKey {
        case enabled, profile, problem
        case firstLook = "first_look"
        case interval
    }
}

struct Statistics: Decodable, Hashable, Sendable {
    var runsTotal: Int = 0
    var runsActive: Int = 0
    var planRuns: Int = 0
    var byStatus: [String: Int] = [:]
    var workerSeconds: Double = 0
    var tokensTotal: Int = 0
    var costUSD: Double = 0
    var byProfile: [ProfileStat] = []

    enum CodingKeys: String, CodingKey {
        case runsTotal = "runs_total"
        case runsActive = "runs_active"
        case planRuns = "plan_runs"
        case byStatus = "by_status"
        case workerSeconds = "worker_seconds"
        case tokensTotal = "tokens_total"
        case costUSD = "cost_usd"
        case byProfile = "by_profile"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        runsTotal = (try? v.decode(Int.self, forKey: .runsTotal)) ?? 0
        runsActive = (try? v.decode(Int.self, forKey: .runsActive)) ?? 0
        planRuns = (try? v.decode(Int.self, forKey: .planRuns)) ?? 0
        byStatus = (try? v.decode([String: Int].self, forKey: .byStatus)) ?? [:]
        workerSeconds = (try? v.decode(Double.self, forKey: .workerSeconds)) ?? 0
        tokensTotal = (try? v.decode(Int.self, forKey: .tokensTotal)) ?? 0
        costUSD = (try? v.decode(Double.self, forKey: .costUSD)) ?? 0
        byProfile = (try? v.decode([ProfileStat].self, forKey: .byProfile)) ?? []
    }
}

struct ProfileStat: Decodable, Identifiable, Hashable, Sendable {
    let profile: String?
    let runs: Int?
    let active: Int?
    let tokens: Int?
    let cost: Double?

    var id: String { profile ?? UUID().uuidString }
}

struct Finding: Decodable, Identifiable, Hashable, Sendable {
    let id: Int?
    let run: Int?
    let claim: String?
    let with: String?
    let confidence: String?
    let whyNotFixed: String?
    let filedAs: String?
    let at: String?

    enum CodingKeys: String, CodingKey {
        case id, run, claim, confidence, at
        case with = "where"
        case whyNotFixed = "why_not_fixed"
        case filedAs = "filed_as"
    }
}

struct Proposal: Decodable, Identifiable, Hashable, Sendable {
    let id: Int?
    let run: Int?
    let title: String?
    let why: String?
    let verdict: String?
    let action: String?
    let at: String?
}

struct TraceEvent: Decodable, Identifiable, Sendable {
    let id: Int
    let kind: String
    let name: String?
    let payload: String
    let payloadLength: Int
    let truncated: Bool
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, name, payload, truncated
        case payloadLength = "payload_len"
        case createdAt = "created_at"
    }
}

struct ActionResponse: Decodable, Sendable {
    let error: String?
}
