import Foundation

struct DromondAPI: Sendable {
    let baseURL: URL
    let key: String
    var session: URLSession = .shared

    func snapshot() async throws -> Snapshot {
        try await get("api/snapshot")
    }

    func stop(runID: Int) async throws {
        let _: ActionResponse = try await post("api/runs/\(runID)/stop", body: [:])
    }

    func tell(runID: Int, text: String) async throws {
        let _: ActionResponse = try await post(
            "api/runs/\(runID)/tell",
            body: ["text": text]
        )
    }

    // --- reads ------------------------------------------------------------

    /// `?refresh=1` POLLS the providers before answering, which spends real
    /// outbound requests against the owner's own plans. Never automatic.
    func runway(refresh: Bool = false) async throws -> [Runway] {
        // This route answers with an OBJECT -- {"runway": [...],
        // "generated_at": ...} -- while the snapshot's own `runway` key is a
        // bare array. Decoding the array shape here threw AFTER the server had
        // already polled the providers, so the request was spent and the
        // reading thrown away.
        let page: RunwayPage = try await get("api/runway" + (refresh ? "?refresh=1" : ""))
        return page.runway
    }

    /// The control turns as a SERIES (I-0081), newest first. The snapshot pins
    /// only the latest one per project; this is the log behind it. A nil
    /// `projectID` reads every project, matching the app's own "all projects"
    /// scope, and `layer` narrows to one of router/merge/observer/conductor.
    ///
    /// The layer filter goes to the daemon rather than being applied here, so
    /// `limit` counts the turns asked for — filtering a page of 100 locally
    /// shows four observer turns and calls it the log.
    func turns(projectID: String? = nil, layer: String? = nil,
               limit: Int = 100) async throws -> [Run] {
        var items = [URLQueryItem(name: "limit", value: String(limit))]
        if let projectID, !projectID.isEmpty {
            items.append(URLQueryItem(name: "project", value: projectID))
        }
        if let layer, !layer.isEmpty {
            items.append(URLQueryItem(name: "layer", value: layer))
        }
        var components = URLComponents()
        components.queryItems = items
        let page: TurnsPage = try await get(
            "api/turns" + (components.query.map { "?\($0)" } ?? ""))
        return page.turns
    }

    func project(id: String) async throws -> ProjectDetail {
        let escaped = id.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? id
        return try await get("api/project?id=\(escaped)")
    }

    func brief(runID: Int) async throws -> BriefText {
        try await get("api/runs/\(runID)/brief")
    }

    func diff(runID: Int) async throws -> DiffText {
        try await get("api/runs/\(runID)/diff")
    }

    /// Keyed by harness name. Each harness answers for itself, because they
    /// genuinely differ: opencode takes no effort at all and the daemon
    /// rejects one, so a picker that offers effort everywhere writes an
    /// invalid profile.
    func profileOptions() async throws -> [String: HarnessOptions] {
        try await get("api/profiles/options")
    }

    func config() async throws -> ConfigText {
        try await get("api/config")
    }

    // --- writes -----------------------------------------------------------

    /// The observer's verdict, not just success — "working, log written 12s
    /// ago" and "silent for 900s, past the 600s stall cap" are the whole point
    /// of asking, and a caller that only learns it did not fail learns nothing.
    @discardableResult
    func check(runID: Int) async throws -> String? {
        let reply: CheckReply = try await post("api/runs/\(runID)/check", body: [:])
        return reply.verdict ?? reply.observation ?? reply.status
    }

    func sweep() async throws {
        let _: ActionResponse = try await post("api/sweep", body: [:])
    }

    func pauseDispatch(_ paused: Bool) async throws {
        let _: ActionResponse = try await post(
            "api/dispatch/" + (paused ? "pause" : "resume"), body: [:])
    }

    /// Restarts the daemon. The reply may never arrive — the process it is
    /// answering from is the one going away — so a transport failure here is
    /// success, not an error to show.
    func restart() async throws {
        do {
            let _: ActionResponse = try await post("api/restart", body: [:])
        } catch is URLError {
            return
        }
    }

    /// The daemon reads the change set from `body["profile"]`, so a flat body
    /// is an EMPTY change set: the write succeeds and does nothing.
    @discardableResult
    func saveProfile(name: String, edit: ProfileEdit) async throws -> ProfileWriteResult {
        try await postJSON("api/profiles/\(name)", body: ["profile": edit.payload])
    }

    /// `names: nil` sends JSON null, which is the config's way of saying every
    /// profile is enabled. Writing today's full list instead would look
    /// identical and then quietly exclude the next profile added.
    @discardableResult
    func setEnabledProfiles(projectID: String, names: [String]?) async throws -> ProjectWriteResult {
        try await postJSON("api/project", body: [
            "project_id": projectID,
            "enabled_profiles": names as Any,
        ])
    }

    /// POST with a heterogeneous body — a change set carries lists and
    /// numbers, not only strings.
    private func postJSON<Value: Decodable>(_ path: String, body: [String: Any]) async throws -> Value {
        var request = try request(path)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(
            withJSONObject: body.mapValues { $0 is NSNull ? NSNull() : $0 })
        let (data, response) = try await session.data(for: request)
        try Self.validate(response, data: data)
        return try JSONDecoder().decode(Value.self, from: data)
    }

    func trace(runID: Int, afterID: Int? = nil) -> AsyncThrowingStream<TraceEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try request("api/runs/\(runID)/stream")
                    if let afterID {
                        request.setValue(String(afterID), forHTTPHeaderField: "Last-Event-ID")
                    }
                    let (bytes, response) = try await session.bytes(for: request)
                    try Self.validate(response, data: nil)
                    var decoder = SSEDecoder()
                    // NOT `bytes.lines`: that sequence DROPS empty lines, and
                    // an empty line is precisely the SSE frame delimiter. The
                    // decoder was therefore never flushed and the stream
                    // yielded nothing at all, forever, while the daemon sent
                    // perfectly well-formed frames.
                    for try await line in bytes.sseLines {
                        guard let message = decoder.feed(line: line) else { continue }
                        if message.event == "end" { break }
                        guard message.event == "trace" else { continue }
                        continuation.yield(try JSONDecoder().decode(
                            TraceEvent.self,
                            from: Data(message.data.utf8)
                        ))
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func get<Value: Decodable>(_ path: String) async throws -> Value {
        let (data, response) = try await session.data(for: request(path))
        try Self.validate(response, data: data)
        return try JSONDecoder().decode(Value.self, from: data)
    }

    private func post<Value: Decodable>(
        _ path: String,
        body: [String: String]
    ) async throws -> Value {
        var request = try request(path)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        let (data, response) = try await session.data(for: request)
        try Self.validate(response, data: data)
        return try JSONDecoder().decode(Value.self, from: data)
    }

    private func request(_ path: String) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw APIError.invalidURL
        }
        var request = URLRequest(url: url)
        request.setValue(key, forHTTPHeaderField: "X-Dromond-Key")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 30
        return request
    }

    private static func validate(_ response: URLResponse, data: Data?) throws {
        guard let http = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            let reason = data.flatMap { String(data: $0, encoding: .utf8) }?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw APIError.http(http.statusCode, reason ?? HTTPURLResponse.localizedString(forStatusCode: http.statusCode))
        }
    }
}

struct CheckReply: Decodable, Sendable {
    let verdict: String?
    let observation: String?
    let status: String?
    let error: String?
}

struct RunwayPage: Decodable, Sendable {
    let runway: [Runway]
    let generatedAt: String?

    enum CodingKeys: String, CodingKey {
        case runway
        case generatedAt = "generated_at"
    }
}

/// `GET /api/turns` answers with an OBJECT, like `/api/runway` — the turns are
/// under a key, and the rows are ordinary run payloads because a control turn
/// is a runs row.
struct TurnsPage: Decodable, Sendable {
    let turns: [Run]
    let generatedAt: String?

    enum CodingKeys: String, CodingKey {
        case turns
        case generatedAt = "generated_at"
    }
}

/// Small one-key replies, named so a view never decodes a bare dictionary.
struct BriefText: Decodable, Sendable {
    let text: String?
    let path: String?
}

/// The committed diff between a run's base and its head.
///
/// Every field here is named off the wire, not guessed. The previous
/// declaration -- diff/files/error -- matched nothing the route sends, and
/// because all three were optional the decode SUCCEEDED and returned an object
/// that was entirely nil. A run with 250 KB of diff read as "no committed
/// changes".
struct DiffText: Decodable, Sendable {
    let run: Int?
    let base: String?
    let head: String?
    let text: String?
    /// The daemon caps the diff; when true, `text` is not the whole story.
    let truncated: Bool?
    /// Set when there is nothing to show, and it says why.
    let message: String?
}

struct ConfigText: Decodable, Sendable {
    let path: String?
    let text: String?
}

/// What one harness will accept. `supportsEffort == false` means the harness
/// has no effort flag at all and the daemon rejects one — opencode takes a
/// `variant` instead, and `effortNote` says so in the harness's own words.
struct HarnessOptions: Decodable, Sendable {
    let supportsEffort: Bool
    let effortNote: String?
    let freeModel: Bool
    let models: [HarnessModel]
    let error: String?

    enum CodingKeys: String, CodingKey {
        case models, error
        case supportsEffort = "supports_effort"
        case effortNote = "effort_note"
        case freeModel = "free_model"
    }

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        supportsEffort = (try? v.decode(Bool.self, forKey: .supportsEffort)) ?? false
        effortNote = try? v.decode(String.self, forKey: .effortNote)
        freeModel = (try? v.decode(Bool.self, forKey: .freeModel)) ?? false
        models = (try? v.decode([HarnessModel].self, forKey: .models)) ?? []
        error = try? v.decode(String.self, forKey: .error)
    }
}

struct HarnessModel: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let efforts: [String]
    let defaultEffort: String?

    enum CodingKeys: String, CodingKey {
        case id, efforts
        case defaultEffort = "default_effort"
    }

    init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        id = (try? v.decode(String.self, forKey: .id)) ?? ""
        efforts = (try? v.decode([String].self, forKey: .efforts)) ?? []
        defaultEffort = try? v.decode(String.self, forKey: .defaultEffort)
    }
}

/// A change set. Only the fields set are sent, so a save never rewrites a
/// field the editor did not touch.
struct ProfileEdit: Sendable {
    var backend: String?
    var model: String?
    var effort: String?
    var variant: String?
    var tier: Int?
    var priority: Int?
    var spawnProfiles: [String]?
    var note: String?

    var payload: [String: Any] {
        var out: [String: Any] = [:]
        if let backend { out["backend"] = backend }
        if let model { out["model"] = model }
        if let effort { out["effort"] = effort }
        if let variant { out["variant"] = variant }
        if let tier { out["tier"] = tier }
        if let priority { out["priority"] = priority }
        if let spawnProfiles { out["spawn_profiles"] = spawnProfiles }
        if let note { out["note"] = note }
        return out
    }
}

struct ProfileWriteResult: Decodable, Sendable {
    let applied: Bool?
    let changed: [String]?
    let unchanged: Bool?
    let error: String?
}

struct ProjectWriteResult: Decodable, Sendable {
    let applied: Bool?
    let enabledProfiles: [String]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case applied, error
        case enabledProfiles = "enabled_profiles"
    }
}

/// One project's view of the two things a project actually changes: which
/// profiles it may staff runs with, and its own statistics. There is no `name`
/// on this route -- the project's name comes from the snapshot's project list.
struct ProjectDetail: Decodable, Sendable {
    let projectID: String?
    /// Null means the project has not chosen, which is every profile — not none.
    let enabledProfiles: [String]?
    let statistics: Statistics?
    let generatedAt: String?

    enum CodingKeys: String, CodingKey {
        case statistics
        case projectID = "project_id"
        case enabledProfiles = "enabled_profiles"
        case generatedAt = "generated_at"
    }
}

enum APIError: LocalizedError {
    case invalidURL
    case invalidResponse
    case http(Int, String)

    var errorDescription: String? {
        switch self {
        case .invalidURL: "The Dromond URL is invalid."
        case .invalidResponse: "Dromond returned an invalid response."
        case let .http(code, reason): "Dromond returned \(code): \(reason)"
        }
    }
}

extension URLSession.AsyncBytes {
    /// Every line, empty ones included. Splits on LF and tolerates CRLF.
    var sseLines: AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var buffer = [UInt8]()
                do {
                    for try await byte in self {
                        if byte == 0x0A {
                            continuation.yield(String(decoding: buffer, as: UTF8.self))
                            buffer.removeAll(keepingCapacity: true)
                        } else if byte != 0x0D {
                            buffer.append(byte)
                        }
                    }
                    if !buffer.isEmpty {
                        continuation.yield(String(decoding: buffer, as: UTF8.self))
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

struct SSEMessage: Equatable {
    let event: String
    let data: String
}

struct SSEDecoder {
    private var event = "message"
    private var data: [String] = []

    mutating func feed(line: String) -> SSEMessage? {
        if line.isEmpty {
            defer {
                event = "message"
                data.removeAll(keepingCapacity: true)
            }
            return data.isEmpty ? nil : SSEMessage(event: event, data: data.joined(separator: "\n"))
        }
        if line.hasPrefix(":") { return nil }
        let fieldAndValue = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        let field = fieldAndValue[0]
        let value = fieldAndValue.count == 2
            ? fieldAndValue[1].drop(while: { $0 == " " })
            : Substring()
        if field == "event" { event = String(value) }
        if field == "data" { data.append(String(value)) }
        return nil
    }
}
