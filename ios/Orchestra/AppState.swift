import Foundation
import UserNotifications

struct RunFilters: Hashable, Sendable {
    var search = ""
    var groupID: String?
    var profileID: String?
    var status: String?

    var isEmpty: Bool {
        search.isEmpty && groupID == nil && profileID == nil && status == nil
    }
}

struct MessageFilters: Hashable, Sendable {
    var direction: String?
    var status: String?
    var kind: String?
}

@MainActor
final class AppState: ObservableObject {
    @Published private(set) var servers = AppState.loadServers() {
        didSet { Self.saveServers(servers) }
    }
    @Published private(set) var selectedServerID: UUID? =
        UserDefaults.standard.string(forKey: "v2.selectedServerID").flatMap(UUID.init)
    @Published private(set) var snapshot: FleetSnapshot?
    @Published private(set) var globalStatistics: RunStatistics?
    @Published private(set) var contextualStatistics: RunStatistics?
    @Published private(set) var runs: [Run] = []
    @Published private(set) var inbox: [AttentionItem] = []
    @Published private(set) var messages: [RunMessage] = []
    @Published private(set) var groups: [RunGroup] = []
    @Published private(set) var runtimes: [RuntimeConfig] = []
    @Published private(set) var profiles: [Profile] = []
    @Published private(set) var runwaySources: [RunwaySource] = []
    @Published private(set) var devices: [Device] = []
    @Published private(set) var serviceTokens: [ServiceTokenRecord] = []
    @Published private(set) var settings: [FleetSetting] = []
    @Published private(set) var runCursor: String?
    @Published private(set) var inboxCursor: String?
    @Published private(set) var messageCursor: String?
    @Published private(set) var loading = false
    @Published private(set) var runQueryLoading = false
    @Published private(set) var error: String?
    @Published var notice: String?
    @Published var filters = RunFilters()
    @Published var messageFilters = MessageFilters()
    @Published private(set) var notificationsEnabled =
        UserDefaults.standard.bool(forKey: "v2.notificationsEnabled")

    private var lastEventID: String?
    private var invalidationRefreshTask: Task<Void, Never>?
    private var loadedRunFilters = RunFilters()

    var selectedServer: Server? {
        servers.first { $0.id == selectedServerID } ?? servers.first
    }

    var isConfigured: Bool {
        guard let server = selectedServer else { return false }
        return (try? Self.validURL(server.url)) != nil
            && !Keychain.load(for: server.keyAccount).isEmpty
    }

    var filteredRuns: [Run] {
        runs
    }

    func groupName(_ id: String) -> String? { groups.first { $0.id == id }?.name }
    func profileName(_ id: String) -> String? { profiles.first { $0.id == id }?.name }
    func runtimeName(_ id: String) -> String? { runtimes.first { $0.id == id }?.name }

    func api(token: String? = nil, endpoint: String? = nil) throws -> OrchestraAPI {
        let url: URL
        if let endpoint { url = try Self.validURL(endpoint) }
        else if let server = selectedServer { url = try Self.validURL(server.url) }
        else { throw APIError.invalidURL }
        let credential = token ?? selectedServer.map { Keychain.load(for: $0.keyAccount) } ?? ""
        return OrchestraAPI(baseURL: url, token: credential)
    }

    @discardableResult
    func saveServer(id: UUID? = nil, label: String, endpoint: String,
                    token: String, instanceID: String? = nil) throws -> Server {
        let url = try Self.validURL(endpoint).absoluteString
        let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmedToken.isEmpty else { throw APIError.http(400, "A device or service token is required.") }
        let server: Server
        if let id, let index = servers.firstIndex(where: { $0.id == id }) {
            var edited = servers[index]
            edited.label = label.trimmingCharacters(in: .whitespacesAndNewlines)
            edited.url = url
            if let instanceID { edited.instanceID = instanceID }
            try Keychain.save(trimmedToken, for: edited.keyAccount)
            servers[index] = edited
            server = edited
        } else {
            let created = Server(label: label, url: url, instanceID: instanceID)
            try Keychain.save(trimmedToken, for: created.keyAccount)
            servers.append(created)
            server = created
        }
        select(server.id)
        return server
    }

    func removeServer(_ server: Server) {
        Keychain.delete(account: server.keyAccount)
        servers.removeAll { $0.id == server.id }
        if selectedServerID == server.id { select(servers.first?.id) }
    }

    func select(_ id: UUID?) {
        guard selectedServerID != id else { return }
        selectedServerID = id
        UserDefaults.standard.set(id?.uuidString, forKey: "v2.selectedServerID")
        clearFleet()
    }

    func refresh(quiet: Bool = false) async {
        guard isConfigured else { return }
        if !quiet { loading = true }
        defer { loading = false }
        do {
            let priorAttentionCount = snapshot == nil ? nil : inbox.count
            let priorUndeliverable = snapshot?.messages.undeliverable
            let client = try api()
            let currentMessageFilters = messageFilters
            let currentRunFilters = filters
            async let snapshotValue = client.snapshot()
            async let runValue = client.runs(
                q: currentRunFilters.search, group: currentRunFilters.groupID,
                profile: currentRunFilters.profileID,
                status: currentRunFilters.status)
            async let statisticsValue = try? await client.statistics()
            async let inboxValue = try? await client.inbox()
            async let messageValue = try? await client.outbox(
                direction: currentMessageFilters.direction,
                status: currentMessageFilters.status,
                kind: currentMessageFilters.kind)
            async let groupValue = try? await client.groups()
            async let runtimeValue = try? await client.runtimes()
            async let profileValue = try? await client.profiles()
            async let runwayValue = try? await client.runwaySources()
            async let deviceValue = try? await client.devices()
            async let tokenValue = try? await client.serviceTokens()
            async let settingValue = try? await client.settings()
            let required = try await (snapshotValue, runValue)
            let optional = await (statisticsValue, inboxValue, messageValue,
                                  groupValue, runtimeValue,
                                  profileValue, runwayValue, deviceValue,
                                  tokenValue, settingValue)
            try accept(instanceID: required.0.instanceID)
            try accept(instanceID: required.1.instanceID)
            snapshot = required.0.value
            if currentRunFilters == filters {
                let firstRuns = required.1.value.items
                let preserveRuns = quiet && loadedRunFilters == currentRunFilters
                    && runs.count > firstRuns.count
                if preserveRuns {
                    let firstIDs = Set(firstRuns.map(\.id))
                    runs = (firstRuns + runs.filter { !firstIDs.contains($0.id) })
                        .sorted { $0.id > $1.id }
                } else {
                    runs = firstRuns
                    runCursor = required.1.value.nextCursor
                }
                loadedRunFilters = currentRunFilters
            }
            if let value = optional.0 {
                try accept(instanceID: value.instanceID)
                globalStatistics = value.value
            }
            if let value = optional.1 {
                try accept(instanceID: value.instanceID)
                let first = value.value.items
                if quiet && inbox.count > first.count {
                    let firstIDs = Set(first.map(\.id))
                    inbox = first + inbox.filter { !firstIDs.contains($0.id) }
                } else {
                    inbox = first
                    inboxCursor = value.value.nextCursor
                }
            }
            if let value = optional.2 {
                try accept(instanceID: value.instanceID)
                if currentMessageFilters == messageFilters {
                    let first = value.value.items
                    if quiet && messages.count > first.count {
                        let firstIDs = Set(first.map(\.id))
                        messages = first + messages.filter { !firstIDs.contains($0.id) }
                    } else {
                        messages = first
                        messageCursor = value.value.nextCursor
                    }
                }
            }
            if let value = optional.3 {
                try accept(instanceID: value.instanceID)
                groups = value.value.items
            }
            if let value = optional.4 {
                try accept(instanceID: value.instanceID)
                runtimes = value.value.items
            }
            if let value = optional.5 {
                try accept(instanceID: value.instanceID)
                profiles = value.value.items
            }
            if let value = optional.6 {
                try accept(instanceID: value.instanceID)
                runwaySources = value.value.items
            }
            if let value = optional.7 {
                try accept(instanceID: value.instanceID)
                devices = value.value.items
            }
            if let value = optional.8 {
                try accept(instanceID: value.instanceID)
                serviceTokens = value.value.items
            }
            if let value = optional.9 {
                try accept(instanceID: value.instanceID)
                settings = value.value.items
            }
            error = nil
            await updateNotifications(previousAttention: priorAttentionCount,
                                      previousUndeliverable: priorUndeliverable)
        } catch {
            report(error)
        }
    }

    func loadMoreRuns() async {
        guard let runCursor else { return }
        let requested = loadedRunFilters
        guard requested == filters else { return }
        do {
            let value = try await api().runs(
                cursor: runCursor, q: requested.search, group: requested.groupID,
                profile: requested.profileID,
                status: requested.status)
            guard requested == filters, requested == loadedRunFilters else { return }
            try accept(instanceID: value.instanceID)
            let known = Set(runs.map(\.id))
            runs.append(contentsOf: value.value.items.filter { !known.contains($0.id) })
            self.runCursor = value.value.nextCursor
        } catch { report(error) }
    }

    func refreshRunsForFilters() async {
        let requested = filters
        runQueryLoading = true
        runs = []
        runCursor = nil
        defer {
            if requested == filters { runQueryLoading = false }
        }
        if !requested.search.trimmed.isEmpty {
            do { try await Task.sleep(for: .milliseconds(300)) }
            catch { return }
        }
        do {
            let value = try await api().runs(
                q: requested.search, group: requested.groupID,
                profile: requested.profileID,
                status: requested.status)
            guard requested == filters else { return }
            try accept(instanceID: value.instanceID)
            runs = value.value.items
            runCursor = value.value.nextCursor
            loadedRunFilters = requested
        } catch is CancellationError {
        } catch {
            guard requested == filters else { return }
            report(error)
        }
    }

    func loadMoreInbox() async {
        guard let inboxCursor else { return }
        do {
            let value = try await api().inbox(cursor: inboxCursor)
            try accept(instanceID: value.instanceID)
            let known = Set(inbox.map(\.id))
            inbox.append(contentsOf: value.value.items.filter { !known.contains($0.id) })
            self.inboxCursor = value.value.nextCursor
        } catch { report(error) }
    }

    func refreshMessages() async {
        let requested = messageFilters
        do {
            let value = try await api().outbox(
                direction: requested.direction, status: requested.status,
                kind: requested.kind)
            guard requested == messageFilters else { return }
            try accept(instanceID: value.instanceID)
            messages = value.value.items
            messageCursor = value.value.nextCursor
        } catch {
            guard requested == messageFilters else { return }
            report(error)
        }
    }

    func loadMoreMessages() async {
        guard let messageCursor else { return }
        let requested = messageFilters
        do {
            let value = try await api().outbox(
                cursor: messageCursor, direction: requested.direction,
                status: requested.status, kind: requested.kind)
            guard requested == messageFilters else { return }
            try accept(instanceID: value.instanceID)
            let known = Set(messages.map(\.id))
            messages.append(contentsOf: value.value.items.filter { !known.contains($0.id) })
            self.messageCursor = value.value.nextCursor
        } catch { report(error) }
    }

    func refreshContextualStatistics() async {
        let requested = filters
        guard requested.search.trimmed.isEmpty else {
            contextualStatistics = nil
            return
        }
        guard requested.groupID != nil || requested.profileID != nil
                || requested.status != nil else {
            contextualStatistics = globalStatistics
            return
        }
        do {
            let response = try await api().statistics(
                group: requested.groupID, profile: requested.profileID,
                status: requested.status)
            guard requested == filters else { return }
            try accept(instanceID: response.instanceID)
            contextualStatistics = response.value
        } catch {
            guard requested == filters else { return }
            contextualStatistics = nil
        }
    }

    func discoverProfiles(local: Bool) async -> ProfileDiscovery? {
        do {
            let response = try await api().profileDiscovery(local: local)
            try accept(instanceID: response.instanceID)
            return response.value
        } catch {
            report(error)
            return nil
        }
    }

    func monitorInvalidations() async {
        while !Task.isCancelled, isConfigured {
            do {
                let client = try api()
                for try await event in client.invalidations(lastEventID: lastEventID) {
                    if let id = event.id { lastEventID = id }
                    invalidationRefreshTask?.cancel()
                    invalidationRefreshTask = Task {
                        try? await Task.sleep(for: .milliseconds(250))
                        guard !Task.isCancelled else { return }
                        await refresh(quiet: true)
                    }
                }
            } catch is CancellationError {
                return
            } catch {
                try? await Task.sleep(for: .seconds(4))
            }
        }
    }

    func redeemPairing(endpoint: String, claim: String, label: String) async throws {
        let parsed = try PairingClaim.parse(claim)
        let chosenEndpoint = parsed.endpoint ?? endpoint
        let client = try api(token: "", endpoint: chosenEndpoint)
        let response = try await client.redeemPairing(
            pairingID: parsed.pairingID, code: parsed.code, label: label)
        _ = try saveServer(label: label, endpoint: chosenEndpoint,
                           token: response.value.token, instanceID: response.instanceID)
        await refresh()
    }

    func connect(endpoint: String, token: String, label: String) async throws {
        let response = try await api(token: token, endpoint: endpoint).snapshot()
        _ = try saveServer(label: label, endpoint: endpoint, token: token,
                           instanceID: response.instanceID)
        await refresh()
    }

    func report(_ error: Error) {
        self.error = error.localizedDescription
    }

    func setNotifications(_ enabled: Bool) async {
        if enabled {
            do {
                let granted = try await UNUserNotificationCenter.current()
                    .requestAuthorization(options: [.alert, .badge, .sound])
                notificationsEnabled = granted
            } catch {
                notificationsEnabled = false
                report(error)
            }
        } else {
            notificationsEnabled = false
            try? await UNUserNotificationCenter.current().setBadgeCount(0)
        }
        UserDefaults.standard.set(notificationsEnabled, forKey: "v2.notificationsEnabled")
    }

    func succeeded(_ message: String) async {
        notice = message
        error = nil
        await refresh(quiet: true)
    }

    private func accept(instanceID: String) throws {
        guard let server = selectedServer else { throw APIError.invalidURL }
        if let expected = server.instanceID, expected != instanceID {
            throw APIError.instanceChanged(expected: expected, received: instanceID)
        }
        if server.instanceID == nil,
           let index = servers.firstIndex(where: { $0.id == server.id }) {
            servers[index].instanceID = instanceID
        }
    }

    private func updateNotifications(previousAttention: Int?,
                                     previousUndeliverable: Int?) async {
        guard notificationsEnabled else { return }
        let center = UNUserNotificationCenter.current()
        try? await center.setBadgeCount(inbox.count + (snapshot?.messages.undeliverable ?? 0))
        let content = UNMutableNotificationContent()
        if let previousAttention, inbox.count > previousAttention,
           let newest = inbox.first {
            content.title = newest.blocking
                ? "Orchestra needs an answer" : "Orchestra attention"
            content.body = newest.prompt ?? newest.message ?? "Open Inbox for details."
        } else if let previousUndeliverable,
                  let current = snapshot?.messages.undeliverable,
                  current > previousUndeliverable {
            content.title = "Orchestra message undeliverable"
            content.body = "Open Inbox / Outbox to inspect the failed delivery receipt."
        } else {
            return
        }
        content.sound = .default
        try? await center.add(UNNotificationRequest(
            identifier: "orchestra-inbox-\(UUID().uuidString)",
            content: content, trigger: nil))
    }

    private func clearFleet() {
        snapshot = nil
        globalStatistics = nil
        contextualStatistics = nil
        runs = []
        inbox = []
        messages = []
        groups = []
        runtimes = []
        profiles = []
        runwaySources = []
        devices = []
        serviceTokens = []
        settings = []
        runCursor = nil
        loadedRunFilters = RunFilters()
        inboxCursor = nil
        messageCursor = nil
        lastEventID = nil
        invalidationRefreshTask?.cancel()
        invalidationRefreshTask = nil
        filters = RunFilters()
        messageFilters = MessageFilters()
        error = nil
    }

    private static func validURL(_ text: String) throws -> URL {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed),
              let scheme = components.scheme?.lowercased(),
              ["http", "https"].contains(scheme), components.host != nil else {
            throw APIError.invalidURL
        }
        if components.path.isEmpty { components.path = "/" }
        guard let url = components.url else { throw APIError.invalidURL }
        return url
    }

    private static let serversKey = "v2.servers"

    private static func loadServers() -> [Server] {
        guard let data = UserDefaults.standard.data(forKey: serversKey) else { return [] }
        return (try? JSONDecoder().decode([Server].self, from: data)) ?? []
    }

    private static func saveServers(_ servers: [Server]) {
        guard let data = try? JSONEncoder().encode(servers) else { return }
        UserDefaults.standard.set(data, forKey: serversKey)
    }
}
