import SwiftUI

enum Destination: String, CaseIterable, Identifiable {
    case runs, inbox, groups, profiles, runway, fleet, settings
    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var icon: String {
        switch self {
        case .runs: "play.square.stack"
        case .inbox: "tray.full"
        case .groups: "square.3.layers.3d"
        case .profiles: "person.crop.rectangle.stack"
        case .runway: "gauge.with.dots.needle.67percent"
        case .fleet: "server.rack"
        case .settings: "gearshape"
        }
    }
}

struct ContentView: View {
    @EnvironmentObject private var state: AppState
#if os(iOS)
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
#endif

    var body: some View {
        Group {
            if state.isConfigured {
#if os(iOS)
                if horizontalSizeClass == .compact { PhoneRoot() }
                else { SidebarRoot() }
#else
                SidebarRoot()
#endif
            } else {
                ConnectView()
            }
        }
        .safeAreaInset(edge: .top) { ConnectionBanner() }
        .task(id: state.selectedServerID) { await state.refresh() }
        .task(id: state.selectedServerID) { await state.monitorInvalidations() }
        .task(id: state.selectedServerID) {
            while !Task.isCancelled, state.isConfigured {
                try? await Task.sleep(for: .seconds(45))
                await state.refresh(quiet: true)
            }
        }
        .alert("Orchestra", isPresented: Binding(
            get: { state.notice != nil },
            set: { if !$0 { state.notice = nil } }
        )) { Button("OK") { state.notice = nil } } message: {
            Text(state.notice ?? "")
        }
    }
}

private struct SidebarRoot: View {
    @EnvironmentObject private var state: AppState
    @State private var selection: Destination? = .runs

    var body: some View {
        NavigationSplitView {
            List(Destination.allCases, selection: $selection) { item in
                HStack {
                    Label(item.title, systemImage: item.icon)
                    if item == .inbox {
                        let urgent = state.inbox.count
                            + (state.snapshot?.messages.undeliverable ?? 0)
                        if urgent > 0 {
                            Spacer()
                            Text(urgent.formatted()).font(.caption2.bold())
                                .padding(.horizontal, 7).padding(.vertical, 2)
                                .background(.red.opacity(0.15), in: Capsule())
                        }
                    }
                }.tag(item)
            }
            .navigationTitle("Orchestra")
        } detail: {
            NavigationStack { destination(selection ?? .runs) }
        }
    }

    @ViewBuilder private func destination(_ item: Destination) -> some View {
        switch item {
        case .runs: RunsView()
        case .inbox: InboxView()
        case .groups: GroupsView()
        case .profiles: ProfilesView()
        case .runway: RunwayView()
        case .fleet: FleetView()
        case .settings: SettingsView()
        }
    }
}

private struct PhoneRoot: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        TabView {
            NavigationStack { RunsView() }
                .tabItem { Label("Runs", systemImage: Destination.runs.icon) }
            NavigationStack { InboxView() }
                .tabItem { Label("Inbox", systemImage: Destination.inbox.icon) }
                .badge(state.inbox.count + (state.snapshot?.messages.undeliverable ?? 0))
            NavigationStack { GroupsView() }
                .tabItem { Label("Groups", systemImage: Destination.groups.icon) }
            NavigationStack { RunwayView() }
                .tabItem { Label("Runway", systemImage: Destination.runway.icon) }
            NavigationStack { MoreView() }
                .tabItem { Label("More", systemImage: "ellipsis.circle") }
        }
    }
}

private struct MoreView: View {
    var body: some View {
        List {
            NavigationLink { ProfilesView() } label: {
                Label("Profiles", systemImage: Destination.profiles.icon)
            }
            NavigationLink { FleetView() } label: {
                Label("Fleet", systemImage: Destination.fleet.icon)
            }
            NavigationLink { SettingsView() } label: {
                Label("Settings", systemImage: Destination.settings.icon)
            }
        }
        .navigationTitle("More")
    }
}

struct ConnectView: View {
    @EnvironmentObject private var state: AppState
    @State private var endpoint = "https://"
    @State private var label = ""
    @State private var claim = ""
    @State private var token = ""
    @State private var busy = false
    @State private var localError: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 12) {
                        OrchestraMark().frame(width: 48, height: 48)
                        VStack(alignment: .leading) {
                            Text("Connect to Orchestra").font(.title2.bold())
                            Text("Pair this device with one standalone fleet.")
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                Section("Fleet") {
                    TextField("https://orchestra.example/", text: $endpoint)
                        .textContentType(.URL)
                    TextField("Device label", text: $label,
                              prompt: Text(defaultDeviceLabel))
                    if endpoint.lowercased().hasPrefix("http://") {
                        Label("Use plain HTTP only over a trusted encrypted private network.",
                              systemImage: "exclamationmark.shield")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                Section("Pairing code or URI") {
                    TextField("Code or Orchestra pairing URI", text: $claim)
                    Button("Pair device") { pair() }
                        .disabled(busy || claim.trimmed.isEmpty
                                  || (!claim.contains("://") && endpoint.trimmed.isEmpty))
                }
                Section("Or use an existing token") {
                    SecureField("Device or service token", text: $token)
                    Button("Connect") { connect() }
                        .disabled(busy || token.trimmed.isEmpty || endpoint.trimmed.isEmpty)
                }
                if let localError {
                    Section { Label(localError, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.red) }
                }
            }
            .disabled(busy)
            .overlay { if busy { ProgressView().controlSize(.large) } }
            .navigationTitle("Orchestra")
        }
    }

    private var deviceLabel: String { label.trimmed.isEmpty ? defaultDeviceLabel : label.trimmed }
    private var defaultDeviceLabel: String {
#if os(macOS)
        Host.current().localizedName ?? "Mac"
#else
        "Apple device"
#endif
    }

    private func pair() {
        busy = true
        Task {
            defer { busy = false }
            do {
                try await state.redeemPairing(endpoint: endpoint, claim: claim,
                                              label: deviceLabel)
                localError = nil
            } catch { localError = error.localizedDescription }
        }
    }

    private func connect() {
        busy = true
        Task {
            defer { busy = false }
            do {
                try await state.connect(endpoint: endpoint, token: token,
                                        label: deviceLabel)
                localError = nil
            } catch { localError = error.localizedDescription }
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var state: AppState
    @State private var pairLabel = ""
    @State private var pairing: PairingCode?
    @State private var observerProfile = ""
    @State private var observerConcurrency = 1
    @State private var firstCheckSeconds = 300
    @State private var minimumEvents = 5
    @State private var subsequentCheckSeconds = 1800
    @State private var observerAuthority = "correct_then_stop"
    @State private var tokenLabel = ""
    @State private var tokenAuthorities: Set<String> = ["read"]
    @State private var createdToken: String?
    @State private var prunePlan: String?

    private var observerProfiles: [Profile] {
        state.profiles.filter(\.observerReady)
    }

    private var observerSelectionIssue: String? {
        guard !observerProfile.isEmpty else { return nil }
        guard let profile = state.profiles.first(where: { $0.id == observerProfile }) else {
            return "The configured profile is no longer available."
        }
        return profile.observerIssue
    }

    var body: some View {
        Form {
            Section("Fleet endpoint") {
                ForEach(state.servers) { server in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(server.displayName)
                            Text(server.url).font(.caption).foregroundStyle(.secondary)
                            Text(server.instanceID ?? "Unpinned")
                                .font(.caption2).monospaced().foregroundStyle(.secondary)
                        }
                        Spacer()
                        if server.id == state.selectedServer?.id { Image(systemName: "checkmark") }
                        Button("Use") { state.select(server.id) }
                            .disabled(server.id == state.selectedServer?.id)
                        Button("Forget", role: .destructive) { state.removeServer(server) }
                    }
                }
                NavigationLink("Add another fleet") { ConnectView() }
            }

            Section("Pair another device") {
                TextField("Device label", text: $pairLabel)
                Button("Create one-time code") { createPairing() }
                    .disabled(pairLabel.trimmed.isEmpty)
                if let pairing {
                    LabeledContent("Code") { Text(pairing.code).font(.title3.monospaced().bold()) }
                    if let uri = pairing.pairingURI {
                        Text(uri).font(.caption.monospaced()).textSelection(.enabled)
                    }
                    LabeledContent("Expires", value: pairing.expiresAt)
                }
            }

            Section("Observer") {
                Picker("Default profile", selection: $observerProfile) {
                    Text("Disabled").tag("")
                    if !observerProfile.isEmpty
                        && !observerProfiles.contains(where: { $0.id == observerProfile }) {
                        Text("Configured profile · unavailable")
                            .tag(observerProfile).disabled(true)
                    }
                    ForEach(observerProfiles) {
                        Text($0.name).tag($0.id)
                    }
                }
                if let issue = observerSelectionIssue {
                    Label("Configured Observer unavailable: \(issue) Choose a replacement or disable it.",
                          systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange)
                }
                Text("Observer profiles are tool-free Claude, OpenCode, or Reasonix launches.")
                    .font(.caption).foregroundStyle(.secondary)
                Stepper("Concurrent checks: \(observerConcurrency)",
                        value: $observerConcurrency, in: 1...8)
                Stepper("First check: \(firstCheckSeconds / 60) min",
                        value: $firstCheckSeconds, in: 60...3600, step: 60)
                Stepper("Minimum evidence events: \(minimumEvents)",
                        value: $minimumEvents, in: 1...100)
                Stepper("Later checks: \(subsequentCheckSeconds / 60) min",
                        value: $subsequentCheckSeconds, in: 60...86_400, step: 60)
                Picker("Authority", selection: $observerAuthority) {
                    Text("Observe only").tag("advisory")
                    Text("May redirect").tag("tell_only")
                    Text("Redirect, then stop").tag("correct_then_stop")
                }
                Button("Save Observer") { saveObserver() }
                    .disabled(observerSelectionIssue != nil)
            }

            Section("Paired devices") {
                if state.devices.isEmpty { Text("No devices returned.").foregroundStyle(.secondary) }
                ForEach(state.devices) { device in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(device.label)
                            Text("Last used \(device.lastUsedAt.relativeAge)")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if device.revokedAt == nil {
                            Button("Revoke", role: .destructive) { revokeDevice(device) }
                        } else { StatusChip(status: "revoked") }
                    }
                }
            }

            Section("Notifications") {
                Toggle("Inbox badges and local alerts", isOn: Binding(
                    get: { state.notificationsEnabled },
                    set: { enabled in Task { await state.setNotifications(enabled) } }
                ))
                Text("Reliable background push is provided by an external callback adapter; Orchestra does not ship an APNs service.")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Section("Service tokens") {
                TextField("Integration label", text: $tokenLabel)
                HStack {
                    ForEach(["dispatch", "read", "control", "answer"], id: \.self) { authority in
                        Toggle(authority, isOn: Binding(
                            get: { tokenAuthorities.contains(authority) },
                            set: { enabled in
                                if enabled { tokenAuthorities.insert(authority) }
                                else { tokenAuthorities.remove(authority) }
                            }
                        )).toggleStyle(.button)
                    }
                }
                Button("Create service token") { createServiceToken() }
                    .disabled(tokenLabel.trimmed.isEmpty || tokenAuthorities.isEmpty)
                if let createdToken {
                    Text("Copy this now; it will not be shown again.").font(.caption).foregroundStyle(.orange)
                    Text(createdToken).font(.caption.monospaced()).textSelection(.enabled)
                }
                ForEach(state.serviceTokens) { token in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(token.label)
                            Text(token.authorities.joined(separator: ", "))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if token.revokedAt == nil {
                            Button("Revoke", role: .destructive) { revokeToken(token) }
                        }
                    }
                }
            }

            Section("Retained evidence") {
                if let storage = state.snapshot?.storage {
                    LabeledContent("Database", value: storage.databaseBytes?.byteCount ?? "—")
                    LabeledContent("Logs", value: storage.logBytes?.byteCount ?? "—")
                    LabeledContent("Artifacts", value: storage.artifactBytes?.byteCount ?? "—")
                    LabeledContent("Checkpoints", value: storage.checkpointBytes?.byteCount ?? "—")
                }
                Button("Preview prune plan") { previewPrune() }
                if let prunePlan { Text(prunePlan).font(.caption.monospaced()).textSelection(.enabled) }
            }
        }
        .navigationTitle("Settings")
        .toolbar { ServerToolbarMenu() }
        .task { seedObserver() }
        .refreshable { await state.refresh() }
    }

    private func seedObserver() {
        guard let settings = state.snapshot?.observer else { return }
        observerProfile = settings.profileID ?? ""
        observerConcurrency = settings.concurrency
        firstCheckSeconds = settings.firstCheckSeconds
        minimumEvents = settings.minimumEvents ?? 5
        subsequentCheckSeconds = settings.subsequentCheckSeconds ?? 1800
        observerAuthority = settings.authority ?? "correct_then_stop"
    }

    private func createPairing() { perform("Pairing code created") { api in
        pairing = try await api.createPairing(label: pairLabel).value
    } }

    private func saveObserver() { perform("Observer saved") { api in
        _ = try await api.updateObserver(.init(
            profileID: observerProfile.isEmpty ? nil : observerProfile,
            concurrency: observerConcurrency, firstCheckSeconds: firstCheckSeconds,
            minimumEvents: minimumEvents,
            subsequentCheckSeconds: subsequentCheckSeconds,
            authority: observerAuthority
        ))
    } }

    private func revokeDevice(_ device: Device) { perform("Device revoked") { api in
        _ = try await api.revokeDevice(device.id)
    } }

    private func createServiceToken() { perform("Service token created") { api in
        let response = try await api.createServiceToken(
            label: tokenLabel, authorities: tokenAuthorities.sorted())
        createdToken = response.value.token
    } }

    private func revokeToken(_ token: ServiceTokenRecord) { perform("Service token revoked") { api in
        _ = try await api.revokeServiceToken(token.id)
    } }

    private func previewPrune() { perform(nil) { api in
        prunePlan = try await api.prunePlan().value.description
    } }

    private func perform(_ success: String?, operation: @escaping (OrchestraAPI) async throws -> Void) {
        Task {
            do {
                try await operation(state.api())
                if let success { await state.succeeded(success) }
            } catch { state.report(error) }
        }
    }
}

extension String {
    var trimmed: String { trimmingCharacters(in: .whitespacesAndNewlines) }
}
