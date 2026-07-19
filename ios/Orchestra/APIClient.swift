import Foundation

enum OrchestraAPIError: LocalizedError, Equatable {
    case invalidServerURL
    case unsupportedScheme
    case nonHTTPResponse
    case server(status: Int, message: String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .invalidServerURL:
            "Enter the full URL of the Orchestra server, including http:// or https://."
        case .unsupportedScheme:
            "The server URL must use HTTP or HTTPS."
        case .nonHTTPResponse:
            "The Orchestra server returned an invalid response."
        case let .server(status, message):
            message.isEmpty ? "Orchestra returned HTTP \(status)." : message
        case let .decoding(message):
            "Orchestra returned data this app could not read: \(message)"
        }
    }
}

struct OrchestraAPIClient: Sendable {
    let baseURL: URL
    let session: URLSession

    init(baseURL: URL, session: URLSession = .shared) throws {
        guard let scheme = baseURL.scheme?.lowercased(), baseURL.host != nil else {
            throw OrchestraAPIError.invalidServerURL
        }
        guard scheme == "http" || scheme == "https" else {
            throw OrchestraAPIError.unsupportedScheme
        }
        self.baseURL = baseURL
        self.session = session
    }

    static func validatedURL(from value: String) throws -> URL {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), !trimmed.isEmpty else {
            throw OrchestraAPIError.invalidServerURL
        }
        guard let scheme = url.scheme?.lowercased(), url.host != nil else {
            throw OrchestraAPIError.invalidServerURL
        }
        guard scheme == "http" || scheme == "https" else {
            throw OrchestraAPIError.unsupportedScheme
        }
        return url
    }

    func projects() async throws -> ProjectDirectory {
        try await get("api/projects")
    }

    func state(projectID: String) async throws -> DashboardState {
        try await get("api/state", projectID: projectID)
    }

    func transcript(runID: Int, projectID: String, etag: String?) async throws -> TranscriptResponse {
        try await get("api/transcript/\(runID)", projectID: projectID,
                      query: etag.map { [URLQueryItem(name: "etag", value: $0)] } ?? [])
    }

    func teammateTranscript(sessionID: String, teamID: String, projectID: String,
                            etag: String?) async throws -> TeammateTranscriptResponse {
        var query = [URLQueryItem(name: "team", value: teamID)]
        if let etag { query.append(URLQueryItem(name: "etag", value: etag)) }
        return try await get("api/teammate/\(sessionID)", projectID: projectID, query: query)
    }

    func usage(force: Bool) async throws -> UsageSnapshot {
        try await get("api/usage", query: force ? [URLQueryItem(name: "refresh", value: "1")] : [])
    }

    func stats(projectID: String) async throws -> RuntimeStats {
        try await get("api/stats", projectID: projectID)
    }

    func stop(runID: Int, projectID: String) async throws {
        var request = try request(path: "api/runs/\(runID)/stop", projectID: projectID)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
    }

    private func get<Response: Decodable>(_ path: String, projectID: String? = nil,
                                           query: [URLQueryItem] = []) async throws -> Response {
        let request = try request(path: path, projectID: projectID, query: query)
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
        do {
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            return try decoder.decode(Response.self, from: data)
        } catch {
            throw OrchestraAPIError.decoding(error.localizedDescription)
        }
    }

    private func request(path: String, projectID: String? = nil,
                         query: [URLQueryItem] = []) throws -> URLRequest {
        let normalizedPath = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        var url = baseURL.appending(path: normalizedPath)
        if !query.isEmpty {
            guard var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
                throw OrchestraAPIError.invalidServerURL
            }
            components.queryItems = query
            guard let queryURL = components.url else { throw OrchestraAPIError.invalidServerURL }
            url = queryURL
        }
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData,
                                 timeoutInterval: 15)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let projectID { request.setValue(projectID, forHTTPHeaderField: "X-Orchestra-Project") }
        return request
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let response = response as? HTTPURLResponse else {
            throw OrchestraAPIError.nonHTTPResponse
        }
        guard (200..<300).contains(response.statusCode) else {
            let payload = try? JSONDecoder().decode(APIErrorPayload.self, from: data)
            let fallback = HTTPURLResponse.localizedString(forStatusCode: response.statusCode)
            throw OrchestraAPIError.server(status: response.statusCode,
                                            message: payload?.error ?? fallback)
        }
    }
}

private struct APIErrorPayload: Decodable {
    let error: String
}
