import Foundation

struct APIValue<Value: Sendable>: Sendable {
    let instanceID: String
    let value: Value
}

struct OrchestraAPI: Sendable {
    let baseURL: URL
    let token: String
    var session: URLSession = .shared

    func snapshot() async throws -> APIValue<FleetSnapshot> {
        try await get("api/v2/snapshot")
    }

    func statistics(group: String? = nil, profile: String? = nil,
                    status: String? = nil) async throws -> APIValue<RunStatistics> {
        let filters = ["group": group, "profile": profile,
                       "status": status].compactMapValues { $0 }
        return try await get(queryPath("api/v2/statistics", query: filters))
    }

    func runs(cursor: String? = nil, q: String? = nil,
              group: String? = nil, profile: String? = nil,
              status: String? = nil) async throws -> APIValue<APIPage<Run>> {
        let filters = ["q": q, "group": group,
                       "profile": profile, "status": status]
            .compactMapValues { value -> String? in
                guard let value else { return nil }
                let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
                return trimmed.isEmpty ? nil : trimmed
            }
        return try await get(pagePath("api/v2/runs", cursor: cursor,
                                      limit: 200, extra: filters))
    }

    func inbox(cursor: String? = nil) async throws -> APIValue<APIPage<AttentionItem>> {
        try await get(pagePath("api/v2/inbox", cursor: cursor, limit: 200,
                               extra: ["state": "open"]))
    }

    func outbox(cursor: String? = nil, direction: String? = nil,
                status: String? = nil, kind: String? = nil,
                runID: Int? = nil) async throws -> APIValue<APIPage<RunMessage>> {
        let filters = ["direction": direction, "status": status, "kind": kind,
                       "run_id": runID.map(String.init)].compactMapValues { $0 }
        return try await get(pagePath("api/v2/outbox", cursor: cursor,
                                      limit: 200, extra: filters))
    }

    func groups() async throws -> APIValue<APIPage<RunGroup>> {
        try await get(pagePath("api/v2/groups", limit: 200))
    }

    func runtimes() async throws -> APIValue<APIPage<RuntimeConfig>> {
        try await get(pagePath("api/v2/runtimes", limit: 200))
    }

    func profiles() async throws -> APIValue<APIPage<Profile>> {
        try await get(pagePath("api/v2/profiles", limit: 200))
    }

    func runwaySources() async throws -> APIValue<APIPage<RunwaySource>> {
        try await get(pagePath("api/v2/runway-sources", limit: 200))
    }

    func devices() async throws -> APIValue<APIPage<Device>> {
        try await get(pagePath("api/v2/devices", limit: 200))
    }

    func serviceTokens() async throws -> APIValue<APIPage<ServiceTokenRecord>> {
        try await get(pagePath("api/v2/service-tokens", limit: 200))
    }

    func settings() async throws -> APIValue<APIPage<FleetSetting>> {
        try await get("api/v2/settings")
    }

    func profileDiscovery(local: Bool) async throws -> APIValue<ProfileDiscovery> {
        try await get("api/v2/profile-discovery?local=\(local)")
    }

    @discardableResult
    func updateSetting(_ setting: FleetSetting,
                       value: JSONValue) async throws -> APIValue<FleetSetting> {
        try await patch("api/v2/settings", body: [
            "request_id": .string(requestID("setting")),
            "key": .string(setting.key), "value": value,
            "expected_revision": .number(Double(setting.revision)),
        ], preferredKey: "setting")
    }

    func run(_ id: Int) async throws -> APIValue<Run> {
        try await get("api/v2/runs/\(id)")
    }

    func thread(_ id: Int, cursor: String? = nil,
                direction: String = "older") async throws -> APIValue<APIPage<RunMessage>> {
        try await get(pagePath("api/v2/runs/\(id)/thread", cursor: cursor,
                               limit: 300, extra: ["direction": direction]))
    }

    func events(_ id: Int, cursor: String? = nil,
                direction: String = "older") async throws -> APIValue<APIPage<RunEvent>> {
        try await get(pagePath("api/v2/runs/\(id)/events", cursor: cursor,
                               limit: 500, extra: ["direction": direction]))
    }

    func artifacts(_ id: Int) async throws -> APIValue<APIPage<Artifact>> {
        try await get(pagePath("api/v2/runs/\(id)/artifacts", limit: 200))
    }

    func changes(_ id: Int) async throws -> APIValue<RunChanges> {
        try await get("api/v2/runs/\(id)/changes")
    }

    func lineage(_ id: Int) async throws -> APIValue<RunLineage> {
        try await get("api/v2/runs/\(id)/lineage")
    }

    func observer(_ id: Int) async throws -> APIValue<ObserverRunDetail> {
        try await get("api/v2/runs/\(id)/observer")
    }

    func rawLog(_ id: Int) async throws -> RawLogTail {
        var request = try makeRequest("api/v2/runs/\(id)/log")
        request.setValue("text/plain", forHTTPHeaderField: "Accept")
        request.setValue("bytes=-262144", forHTTPHeaderField: "Range")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data, accepted: [200, 206])
        guard let value = String(data: data, encoding: .utf8) else {
            throw APIError.invalidResponse
        }
        return RawLogTail(text: value,
                          partial: (response as? HTTPURLResponse)?.statusCode == 206,
                          byteCount: data.count)
    }

    func fullRawLog(_ id: Int) async throws -> Data {
        var request = try makeRequest("api/v2/runs/\(id)/log")
        request.setValue("text/plain", forHTTPHeaderField: "Accept")
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return data
    }

    func artifactContent(_ id: String) async throws -> Data {
        let (data, response) = try await session.data(
            for: makeRequest("api/v2/artifacts/\(escapedPath(id))/content"))
        try validate(response, data: data, accepted: [200, 206])
        return data
    }

    @discardableResult
    func dispatch(group: String, profile: String, title: String?,
                  request: String, cwd: String?) async throws -> APIValue<RunAdmission> {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("run")),
            "group": .string(group), "profile": .string(profile),
            "context": .string(request),
            "title": title.map(JSONValue.string) ?? .null,
            "requested_by": .string("apple-client"),
        ]
        if let cwd { body["cwd"] = .string(cwd) }
        return try await post("api/v2/runs", body: body)
    }

    @discardableResult
    func control(runID: Int, action: String, text: String? = nil) async throws -> APIValue<JSONValue> {
        var body: [String: JSONValue] = ["request_id": .string(requestID(action))]
        if let text {
            body[action == "continue" || action == "retry" ? "context" : "text"] = .string(text)
        }
        return try await post("api/v2/runs/\(runID)/\(action)", body: body)
    }

    @discardableResult
    func answer(attentionID: String, answer: String,
                choice: String? = nil) async throws -> APIValue<JSONValue> {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("answer")), "answer": .string(answer),
        ]
        body["choice"] = choice.map(JSONValue.string) ?? .null
        return try await post("api/v2/attention/\(escapedPath(attentionID))/answer", body: body)
    }

    @discardableResult
    func decideProposal(_ attentionID: String, approve: Bool) async throws -> APIValue<JSONValue> {
        try await post("api/v2/attention/\(escapedPath(attentionID))/\(approve ? "approve" : "reject")",
                       body: [
                        "request_id": .string(requestID("proposal")),
                        "answer": .string(approve ? "Approved" : "Rejected"),
                       ])
    }

    @discardableResult
    func acknowledge(_ attentionID: String) async throws -> APIValue<JSONValue> {
        try await post("api/v2/attention/\(escapedPath(attentionID))/acknowledge",
                       body: [
                        "request_id": .string(requestID("acknowledge")),
                        "answer": .string("Acknowledged"),
                       ])
    }

    @discardableResult
    func scheduler(paused: Bool) async throws -> APIValue<JSONValue> {
        try await post("api/v2/scheduler/\(paused ? "pause" : "resume")",
                       body: ["request_id": .string(requestID("scheduler"))])
    }

    @discardableResult
    func createGroup(name: String, cwd: String? = nil) async throws -> APIValue<RunGroup> {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("group")), "name": .string(name),
        ]
        if let cwd { body["cwd"] = .string(cwd) }
        return try await post("api/v2/groups", body: body, preferredKey: "group")
    }

    @discardableResult
    func updateGroup(_ id: String, name: String? = nil,
                     archived: Bool? = nil) async throws -> APIValue<RunGroup> {
        var body: [String: JSONValue] = ["request_id": .string(requestID("group"))]
        if let name { body["name"] = .string(name) }
        if let archived { body["archived"] = .bool(archived) }
        return try await patch("api/v2/groups/\(escapedPath(id))", body: body,
                               preferredKey: "group")
    }

    @discardableResult
    func updateGroupCWD(_ id: String, cwd: String?) async throws -> APIValue<RunGroup> {
        try await patch("api/v2/groups/\(escapedPath(id))", body: [
            "request_id": .string(requestID("group-cwd")),
            "cwd": cwd.map(JSONValue.string) ?? .null,
        ], preferredKey: "group")
    }

    @discardableResult
    func updateProfile(_ profile: Profile, env: [String: String]? = nil,
                       config: [String: JSONValue]? = nil) async throws -> APIValue<Profile> {
        try await patch("api/v2/profiles/\(escapedPath(profile.id))",
                        body: profileBody(profile, env: env, config: config),
                        preferredKey: "profile")
    }

    @discardableResult
    func createProfile(_ profile: Profile, env: [String: String]? = nil,
                       config: [String: JSONValue]? = nil) async throws -> APIValue<Profile> {
        try await post("api/v2/profiles",
                       body: profileBody(profile, env: env, config: config),
                       preferredKey: "profile")
    }

    @discardableResult
    func updateRuntime(_ runtime: RuntimeConfig, updateArgv: Bool,
                       config: [String: JSONValue]? = nil) async throws -> APIValue<RuntimeConfig> {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("runtime")),
            "name": .string(runtime.name), "kind": .string(runtime.kind),
            "enabled": .bool(runtime.enabled),
        ]
        if updateArgv { body["argv"] = .array(runtime.argv.map(JSONValue.string)) }
        if let config { body["config"] = .object(config) }
        return try await patch("api/v2/runtimes/\(escapedPath(runtime.id))",
                               body: body, preferredKey: "runtime")
    }

    @discardableResult
    func createRuntime(_ runtime: RuntimeConfig,
                       config: [String: JSONValue]? = nil) async throws -> APIValue<RuntimeConfig> {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("runtime")),
            "name": .string(runtime.name), "kind": .string(runtime.kind),
            "argv": .array(runtime.argv.map(JSONValue.string)),
            "enabled": .bool(runtime.enabled),
        ]
        if let config { body["config"] = .object(config) }
        return try await post("api/v2/runtimes", body: body, preferredKey: "runtime")
    }

    @discardableResult
    func refreshRunway(_ id: String) async throws -> APIValue<JSONValue> {
        try await post("api/v2/runway-sources/\(escapedPath(id))/refresh",
                       body: ["request_id": .string(requestID("runway"))])
    }

    @discardableResult
    func createRunwaySource(_ source: RunwaySourceDraft) async throws -> APIValue<RunwaySource> {
        try await post("api/v2/runway-sources", body: runwayBody(source),
                       preferredKey: "runway_source")
    }

    @discardableResult
    func updateRunwaySource(_ id: String,
                            source: RunwaySourceDraft) async throws -> APIValue<RunwaySource> {
        try await patch("api/v2/runway-sources/\(escapedPath(id))",
                        body: runwayBody(source), preferredKey: "runway_source")
    }

    @discardableResult
    func archiveRunwaySource(_ id: String) async throws -> APIValue<RunwaySource> {
        try await patch("api/v2/runway-sources/\(escapedPath(id))", body: [
            "request_id": .string(requestID("runway-archive")),
            "archived": .bool(true),
        ], preferredKey: "runway_source")
    }

    @discardableResult
    func updateObserver(_ settings: ObserverSettings) async throws -> APIValue<JSONValue> {
        try await patch("api/v2/observer", body: [
            "request_id": .string(requestID("observer")),
            "profile_id": settings.profileID.map(JSONValue.string) ?? .null,
            "concurrency": .number(Double(settings.concurrency)),
            "first_check_seconds": .number(Double(settings.firstCheckSeconds)),
            "minimum_events": settings.minimumEvents.map { .number(Double($0)) } ?? .null,
            "subsequent_check_seconds": settings.subsequentCheckSeconds.map { .number(Double($0)) } ?? .null,
            "authority": .string(settings.authority ?? "correct_then_stop"),
        ])
    }

    func createPairing(label: String) async throws -> APIValue<PairingCode> {
        try await post("api/v2/devices/pairing", body: [
            "request_id": .string(requestID("pair")), "label": .string(label),
        ])
    }

    func redeemPairing(pairingID: String?, code: String,
                       label: String) async throws -> APIValue<PairingRedemption> {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("redeem")),
            "code": .string(code), "name": .string(label),
        ]
        if let pairingID { body["pairing_id"] = .string(pairingID) }
        return try await post("api/v2/pairing/redeem", body: body)
    }

    @discardableResult
    func revokeDevice(_ id: String) async throws -> APIValue<JSONValue> {
        try await patch("api/v2/devices/\(escapedPath(id))", body: [
            "request_id": .string(requestID("device")),
            "revoked": .bool(true),
        ])
    }

    func createServiceToken(label: String, authorities: [String]) async throws -> APIValue<ServiceTokenCreation> {
        try await post("api/v2/service-tokens", body: [
            "request_id": .string(requestID("service-token")),
            "label": .string(label),
            "authorities": .array(authorities.map(JSONValue.string)),
        ])
    }

    @discardableResult
    func revokeServiceToken(_ id: String) async throws -> APIValue<JSONValue> {
        try await patch("api/v2/service-tokens/\(escapedPath(id))", body: [
            "request_id": .string(requestID("service-token")),
            "revoked": .bool(true),
        ])
    }

    func prunePlan() async throws -> APIValue<JSONValue> {
        try await post("api/v2/storage/prune-plan", body: [
            "request_id": .string(requestID("prune-plan")),
        ])
    }

    func invalidations(lastEventID: String? = nil) -> AsyncThrowingStream<SSEMessage, Error> {
        stream(path: "api/v2/stream", lastEventID: lastEventID) { $0 }
    }

    func runEvents(_ id: Int, lastEventID: String? = nil) -> AsyncThrowingStream<RunEvent, Error> {
        stream(path: "api/v2/runs/\(id)/stream", lastEventID: lastEventID) { message in
            try JSONDecoder().decode(RunEvent.self, from: Data(message.data.utf8))
        }
    }

    private func stream<Value: Sendable>(
        path: String, lastEventID: String?, transform: @escaping @Sendable (SSEMessage) throws -> Value
    ) -> AsyncThrowingStream<Value, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try makeRequest(path)
                    request.timeoutInterval = 60 * 60
                    if let lastEventID {
                        request.setValue(lastEventID, forHTTPHeaderField: "Last-Event-ID")
                    }
                    let (bytes, response) = try await session.bytes(for: request)
                    try validate(response, data: nil)
                    var decoder = SSEDecoder()
                    for try await line in bytes.sseLines {
                        guard let message = decoder.feed(line: line) else { continue }
                        continuation.yield(try transform(message))
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

    private func get<Value: Decodable & Sendable>(
        _ path: String, preferredKey: String? = nil
    ) async throws -> APIValue<Value> {
        try await send(path, method: "GET", body: nil, preferredKey: preferredKey)
    }

    private func post<Value: Decodable & Sendable>(
        _ path: String, body: [String: JSONValue], preferredKey: String? = nil
    ) async throws -> APIValue<Value> {
        try await send(path, method: "POST", body: body, preferredKey: preferredKey)
    }

    private func patch<Value: Decodable & Sendable>(
        _ path: String, body: [String: JSONValue], preferredKey: String? = nil
    ) async throws -> APIValue<Value> {
        try await send(path, method: "PATCH", body: body, preferredKey: preferredKey)
    }

    private func send<Value: Decodable & Sendable>(
        _ path: String, method: String, body: [String: JSONValue]?, preferredKey: String?
    ) async throws -> APIValue<Value> {
        var request = try makeRequest(path)
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }
        let (data, response) = try await session.data(for: request)
        try validate(response, data: data)
        return try Self.decodeEnvelope(data, preferredKey: preferredKey)
    }

    static func decodeEnvelope<Value: Decodable & Sendable>(
        _ data: Data, preferredKey: String? = nil
    ) throws -> APIValue<Value> {
        guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let version = root["api_version"] as? NSNumber else {
            throw APIError.malformedEnvelope
        }
        guard version.intValue == 2 else { throw APIError.apiVersion(version.intValue) }
        guard let instanceID = root["instance_id"] as? String,
              let rawData = root["data"] else { throw APIError.malformedEnvelope }
        var payload = rawData
        if let preferredKey, let object = rawData as? [String: Any],
           let nested = object[preferredKey] { payload = nested }
        let encoded = try JSONSerialization.data(withJSONObject: payload,
                                                  options: .fragmentsAllowed)
        return try APIValue(instanceID: instanceID,
                            value: JSONDecoder().decode(Value.self, from: encoded))
    }

    private func makeRequest(_ path: String) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL else {
            throw APIError.invalidURL
        }
        var request = URLRequest(url: url)
        if !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 30
        return request
    }

    private func validate(_ response: URLResponse, data: Data?,
                          accepted: Set<Int> = Set(200..<300)) throws {
        guard let response = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        guard accepted.contains(response.statusCode) else {
            let message = data.flatMap(Self.errorMessage)
                ?? HTTPURLResponse.localizedString(forStatusCode: response.statusCode)
            throw APIError.http(response.statusCode, message)
        }
    }

    private static func errorMessage(_ data: Data) -> String? {
        guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let error = root["error"] as? [String: Any] else { return nil }
        return error["message"] as? String
    }

    private func pagePath(_ path: String, cursor: String? = nil, limit: Int,
                          extra: [String: String] = [:]) -> String {
        var query = extra
        query["limit"] = String(limit)
        if let cursor { query["cursor"] = cursor }
        return queryPath(path, query: query)
    }

    private func queryPath(_ path: String, query: [String: String]) -> String {
        var components = URLComponents()
        components.queryItems = query.sorted { $0.key < $1.key }
            .map(URLQueryItem.init(name:value:))
        return path + (components.percentEncodedQuery.map { "?" + $0 } ?? "")
    }

    private func escapedPath(_ value: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    private func requestID(_ prefix: String) -> String {
        "apple:\(prefix):\(UUID().uuidString.lowercased())"
    }

    private func profileBody(_ profile: Profile, env: [String: String]?,
                             config: [String: JSONValue]?) -> [String: JSONValue] {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("profile")),
            "name": .string(profile.name), "runtime_id": .string(profile.runtimeID),
            "model": profile.model.map(JSONValue.string) ?? .null,
            "effort": profile.effort.map(JSONValue.string) ?? .null,
            "tier": .number(Double(profile.tier)),
            "priority": .number(Double(profile.priority)),
            "sandbox": profile.sandbox.map(JSONValue.string) ?? .null,
            "timeout_seconds": profile.timeoutSeconds.map { .number(Double($0)) } ?? .null,
            "active_cap": profile.activeCap.map { .number(Double($0)) } ?? .null,
            "runway_source_id": profile.runwaySourceID.map(JSONValue.string) ?? .null,
            "note": profile.note.map(JSONValue.string) ?? .null,
            "enabled": .bool(profile.enabled),
        ]
        if let env { body["env"] = .object(env.mapValues(JSONValue.string)) }
        if let config { body["config"] = .object(config) }
        return body
    }

    private func runwayBody(_ source: RunwaySourceDraft) -> [String: JSONValue] {
        var body: [String: JSONValue] = [
            "request_id": .string(requestID("runway-source")),
            "name": .string(source.name), "provider": .string(source.provider),
            "account": .string(source.account), "lane": .string(source.lane),
            "adapter": .string(source.adapter), "enabled": .bool(source.enabled),
        ]
        if let argv = source.argv {
            body["argv"] = .array(argv.map(JSONValue.string))
        }
        if let config = source.config { body["config"] = .object(config) }
        return body
    }
}

extension URLSession.AsyncBytes {
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
