import Foundation

@MainActor
final class AppModel: ObservableObject {
    enum ConnectionState: Equatable {
        case disconnected
        case connecting
        case connected
        case failed(String)
    }

    private enum DefaultsKey {
        static let serverURL = "orchestra.serverURL"
        static let selectedProject = "orchestra.selectedProject"
    }

    @Published var serverURL: String
    @Published private(set) var connectionState: ConnectionState = .disconnected
    @Published private(set) var directory: ProjectDirectory?
    @Published private(set) var selectedProjectID: String?
    @Published private(set) var dashboard: DashboardState?
    @Published private(set) var usage: UsageSnapshot?
    @Published private(set) var stats: RuntimeStats?
    @Published private(set) var isRefreshingUsage = false
    @Published private(set) var lastError: String?

    private var client: OrchestraAPIClient?
    private var pollTask: Task<Void, Never>?
    private var consecutivePollFailures = 0
    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        serverURL = defaults.string(forKey: DefaultsKey.serverURL) ?? ""
        selectedProjectID = defaults.string(forKey: DefaultsKey.selectedProject)
    }

    var selectedProject: OrchestraProject? {
        directory?.projects.first { $0.id == selectedProjectID }
    }

    var unreadCount: Int {
        dashboard?.messages.lazy.filter(\.isUnread).count ?? 0
    }

    var waitingCount: Int {
        dashboard?.runs.lazy.filter(\.isWaiting).count ?? 0
    }

    func connect() async {
        stopPolling()
        connectionState = .connecting
        lastError = nil
        dashboard = nil
        usage = nil
        stats = nil

        do {
            let url = try OrchestraAPIClient.validatedURL(from: serverURL)
            let client = try OrchestraAPIClient(baseURL: url)
            let directory = try await client.projects()
            guard !directory.projects.isEmpty else {
                throw OrchestraAPIError.server(status: 503, message: "This Orchestra instance has no available projects.")
            }
            self.client = client
            self.directory = directory
            defaults.set(url.absoluteString, forKey: DefaultsKey.serverURL)
            serverURL = url.absoluteString

            let rememberedIsValid = directory.projects.contains { $0.id == selectedProjectID && $0.isAvailable }
            let fallback = directory.projects.first { $0.id == directory.defaultProjectId && $0.isAvailable }
                ?? directory.projects.first { $0.isAvailable }
            if !rememberedIsValid { selectedProjectID = fallback?.id }
            guard let selectedProjectID else {
                throw OrchestraAPIError.server(status: 503, message: "This Orchestra instance has no available projects.")
            }

            dashboard = try await client.state(projectID: selectedProjectID)
            connectionState = .connected
            consecutivePollFailures = 0
            persistSelectedProject()
            startPolling()

            async let usageRequest = client.usage(force: false)
            async let statsRequest = client.stats(projectID: selectedProjectID)
            usage = try? await usageRequest
            stats = try? await statsRequest
        } catch {
            client = nil
            directory = nil
            dashboard = nil
            connectionState = .failed(error.localizedDescription)
            lastError = error.localizedDescription
        }
    }

    func disconnect() {
        stopPolling()
        client = nil
        directory = nil
        dashboard = nil
        usage = nil
        stats = nil
        connectionState = .disconnected
        lastError = nil
    }

    func selectProject(_ project: OrchestraProject) async {
        guard project.isAvailable, project.id != selectedProjectID, let client else { return }
        selectedProjectID = project.id
        persistSelectedProject()
        dashboard = nil
        stats = nil
        lastError = nil
        do {
            async let stateRequest = client.state(projectID: project.id)
            async let statsRequest = client.stats(projectID: project.id)
            let newDashboard = try await stateRequest
            let newStats = try? await statsRequest
            guard selectedProjectID == project.id else { return }
            dashboard = newDashboard
            stats = newStats
            connectionState = .connected
            consecutivePollFailures = 0
        } catch {
            lastError = error.localizedDescription
        }
    }

    func refreshState() async {
        guard let client, let selectedProjectID else { return }
        do {
            let newDashboard = try await client.state(projectID: selectedProjectID)
            guard self.selectedProjectID == selectedProjectID else { return }
            dashboard = newDashboard
            connectionState = .connected
            lastError = nil
            consecutivePollFailures = 0
        } catch is CancellationError {
            return
        } catch {
            consecutivePollFailures += 1
            lastError = error.localizedDescription
            if consecutivePollFailures >= 2 {
                connectionState = .failed(error.localizedDescription)
            }
        }
    }

    func refreshProjects() async {
        guard let client else { return }
        do {
            let updated = try await client.projects()
            directory = updated
            if !updated.projects.contains(where: { $0.id == selectedProjectID && $0.isAvailable }),
               let replacement = updated.projects.first(where: \.isAvailable) {
                await selectProject(replacement)
            }
        } catch {
            lastError = error.localizedDescription
        }
    }

    func refreshUsage(force: Bool = false) async {
        guard let client, !isRefreshingUsage else { return }
        isRefreshingUsage = true
        defer { isRefreshingUsage = false }
        do {
            usage = try await client.usage(force: force)
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    func refreshStats() async {
        guard let client, let selectedProjectID else { return }
        do {
            let newStats = try await client.stats(projectID: selectedProjectID)
            guard self.selectedProjectID == selectedProjectID else { return }
            stats = newStats
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
    }

    func transcript(runID: Int, etag: String?) async throws -> TranscriptResponse {
        guard let client, let selectedProjectID else { throw OrchestraAPIError.invalidServerURL }
        return try await client.transcript(runID: runID, projectID: selectedProjectID, etag: etag)
    }

    func teammateTranscript(sessionID: String, teamID: String,
                            etag: String?) async throws -> TeammateTranscriptResponse {
        guard let client, let selectedProjectID else { throw OrchestraAPIError.invalidServerURL }
        return try await client.teammateTranscript(sessionID: sessionID, teamID: teamID,
                                                    projectID: selectedProjectID, etag: etag)
    }

    func stop(runID: Int) async throws {
        guard let client, let selectedProjectID else { throw OrchestraAPIError.invalidServerURL }
        try await client.stop(runID: runID, projectID: selectedProjectID)
        await refreshState()
    }

    func startPolling() {
        guard client != nil, pollTask == nil else { return }
        pollTask = Task { [weak self] in
            var cycle = 0
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                guard !Task.isCancelled, let self else { return }
                await self.refreshState()
                cycle += 1
                if cycle.isMultiple(of: 4) { await self.refreshProjects() }
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    private func persistSelectedProject() {
        defaults.set(selectedProjectID, forKey: DefaultsKey.selectedProject)
    }
}
