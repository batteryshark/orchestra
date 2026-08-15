import Foundation

@MainActor
final class AppState: ObservableObject {
    @Published var serverURL = UserDefaults.standard.string(forKey: "serverURL") ?? "http://localhost:3011/"
    @Published var key = Keychain.load()
    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var error: String?
    @Published private(set) var loading = false
    /// Nil means every project. Persisted, because the pick is a working
    /// context and losing it on every launch is its own small annoyance.
    @Published var selectedProjectID: String? = UserDefaults.standard.string(forKey: "selectedProjectID") {
        didSet { UserDefaults.standard.set(selectedProjectID, forKey: "selectedProjectID") }
    }

    var isConfigured: Bool { URL(string: serverURL) != nil && !key.isEmpty }

    func api() throws -> DromondAPI {
        guard let url = URL(string: serverURL), let scheme = url.scheme,
              ["http", "https"].contains(scheme), url.host != nil else {
            throw APIError.invalidURL
        }
        return DromondAPI(baseURL: url, key: key)
    }

    func saveSettings(serverURL: String, key: String) throws {
        let oldURL = self.serverURL
        let oldKey = self.key
        self.serverURL = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
        self.key = key.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            _ = try api()
            try Keychain.save(self.key)
            UserDefaults.standard.set(self.serverURL, forKey: "serverURL")
        } catch {
            self.serverURL = oldURL
            self.key = oldKey
            throw error
        }
    }

    func refresh() async {
        guard isConfigured else { return }
        loading = true
        defer { loading = false }
        do {
            snapshot = try await api().snapshot()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Runs the tabs display: every run, or one project's.
    var runs: [Run] {
        let all = snapshot?.runs ?? []
        guard let selectedProjectID else { return all }
        return all.filter { $0.projectID == selectedProjectID }
    }

    var liveRuns: [Run] { runs.filter(\.live) }
    var projects: [Project] { snapshot?.projects ?? [] }
    var profiles: [Profile] { snapshot?.profiles ?? [] }
    var selectedProject: Project? {
        projects.first { $0.projectID == selectedProjectID }
    }

    /// The badge on the Findings tab: things a person has to look at.
    var attentionCount: Int {
        (snapshot?.findings.count ?? 0) + (snapshot?.proposals.count ?? 0)
    }

    /// Runs stopped somewhere a human has to answer.
    var blockedRuns: [Run] { runs.filter { !$0.blockedOn.isEmpty && !$0.isTerminal } }

    // --- actions the views call; each refreshes so the UI cannot drift -----

    func perform(_ body: @escaping (DromondAPI) async throws -> Void) async -> String? {
        do {
            try await body(try api())
            await refresh()
            return nil
        } catch {
            return error.localizedDescription
        }
    }
}
