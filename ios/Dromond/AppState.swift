import Foundation

@MainActor
final class AppState: ObservableObject {
    /// Every daemon this phone knows. Persisted as JSON; each server's key
    /// lives in its own Keychain item, never here.
    @Published private(set) var servers: [Server] = AppState.loadServers() {
        didSet { AppState.saveServers(servers) }
    }
    /// Which one the app is talking to. Switching is a context change, not a
    /// filter: the snapshot is dropped so no screen can show one daemon's runs
    /// under another's name.
    @Published private(set) var selectedServerID: UUID? =
        UserDefaults.standard.string(forKey: "selectedServerID").flatMap(UUID.init)
    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var error: String?
    @Published private(set) var loading = false
    /// Nil means every project. Persisted, because the pick is a working
    /// context and losing it on every launch is its own small annoyance.
    @Published var selectedProjectID: String? = UserDefaults.standard.string(forKey: "selectedProjectID") {
        didSet { UserDefaults.standard.set(selectedProjectID, forKey: "selectedProjectID") }
    }

    var selectedServer: Server? {
        servers.first { $0.id == selectedServerID } ?? servers.first
    }

    var serverURL: String { selectedServer?.url ?? "" }
    var key: String { selectedServer.map { Keychain.load(for: $0.keyAccount) } ?? "" }

    var isConfigured: Bool {
        guard let server = selectedServer else { return false }
        return URL(string: server.url) != nil
            && !Keychain.load(for: server.keyAccount).isEmpty
    }

    func api() throws -> DromondAPI {
        guard let server = selectedServer,
              let url = URL(string: server.url), let scheme = url.scheme,
              ["http", "https"].contains(scheme), url.host != nil else {
            throw APIError.invalidURL
        }
        return DromondAPI(baseURL: url, key: Keychain.load(for: server.keyAccount))
    }

    // --- the server list --------------------------------------------------

    /// Add or update one server. The key is written first and rolled back if
    /// the URL turns out to be unusable, so a half-saved server never becomes
    /// the selected one.
    @discardableResult
    func saveServer(id: UUID?, label: String, url: String, key: String) throws -> Server {
        let trimmedURL = url.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedKey = key.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let parsed = URL(string: trimmedURL), let scheme = parsed.scheme,
              ["http", "https"].contains(scheme), parsed.host != nil else {
            throw APIError.invalidURL
        }
        var server: Server
        if let id, let index = servers.firstIndex(where: { $0.id == id }) {
            server = servers[index]
            server.label = label
            server.url = trimmedURL
            try Keychain.save(trimmedKey, for: server.keyAccount)
            servers[index] = server
        } else {
            // The very first server inherits the pre-multi-server Keychain
            // account, so an upgrading phone keeps the key it already had.
            let account = servers.isEmpty ? Keychain.legacyAccount : nil
            server = Server(label: label, url: trimmedURL, keyAccount: account)
            try Keychain.save(trimmedKey, for: server.keyAccount)
            servers.append(server)
        }
        if selectedServerID == nil { select(server.id) }
        return server
    }

    func removeServer(_ server: Server) {
        Keychain.delete(account: server.keyAccount)
        servers.removeAll { $0.id == server.id }
        if selectedServerID == server.id {
            select(servers.first?.id)
        }
    }

    /// Switching daemons drops everything the last one said. A stale snapshot
    /// under a new server's name is worse than an empty screen.
    func select(_ id: UUID?) {
        guard id != selectedServerID else { return }
        selectedServerID = id
        UserDefaults.standard.set(id?.uuidString, forKey: "selectedServerID")
        snapshot = nil
        error = nil
        selectedProjectID = nil  // projects are one daemon's, never shared
    }

    // --- persistence ------------------------------------------------------

    private static let serversKey = "servers"

    private static func loadServers() -> [Server] {
        let defaults = UserDefaults.standard
        if let data = defaults.data(forKey: serversKey),
           let stored = try? JSONDecoder().decode([Server].self, from: data) {
            return stored
        }
        // Migration: the single server this app used to hold. Its key stays
        // where it is, under the legacy account, and the entry points at it.
        guard let url = defaults.string(forKey: "serverURL"), !url.isEmpty,
              !Keychain.load().isEmpty else { return [] }
        let migrated = [Server(label: "", url: url,
                               keyAccount: Keychain.legacyAccount)]
        saveServers(migrated)
        return migrated
    }

    private static func saveServers(_ servers: [Server]) {
        guard let data = try? JSONEncoder().encode(servers) else { return }
        UserDefaults.standard.set(data, forKey: serversKey)
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

    /// The latest control turn for what the tabs display. Scoped like `runs`:
    /// a staffing decision about another project pinned above this board
    /// reads as if it happened here.
    var pinnedTurn: Run? {
        let turns = snapshot?.pinnedTurns ?? []
        guard let selectedProjectID else { return turns.first }
        return turns.first { $0.projectID == selectedProjectID }
    }
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
