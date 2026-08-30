import SwiftUI

struct RunsView: View {
    @EnvironmentObject private var state: AppState
    @State private var showingDispatch = false

    var body: some View {
        Group {
            if (state.loading && state.snapshot == nil) ||
                (state.runQueryLoading && state.runs.isEmpty) {
                LoadingState(label: "Loading runs…")
            } else if state.filteredRuns.isEmpty {
                VStack(spacing: 0) {
                    statistics
                    filters
                    EmptyState(icon: "play.square.stack", title: "No matching runs",
                               message: state.filters.isEmpty
                               ? "Start a run to put this fleet to work."
                               : "Change or clear the current filters.")
                }
            } else {
                List {
                    Section { statistics.listRowInsets(.init()) }
                    Section { filters.listRowInsets(.init()) }
                    Section {
                        ForEach(state.filteredRuns) { run in
                            NavigationLink { RunDetailView(run: run) } label: { RunRow(run: run) }
                        }
                    }
                    if state.runCursor != nil {
                        Section { Button("Load older runs") { Task { await state.loadMoreRuns() } } }
                    }
                }
                .listStyle(.plain)
            }
        }
        .navigationTitle("Runs")
        .searchable(text: $state.filters.search, prompt: "Search runs…")
        .toolbar {
            ServerToolbarMenu()
            ToolbarItem(placement: .automatic) {
                Button { showingDispatch = true } label: { Label("New run", systemImage: "plus") }
                    .disabled(state.groups.isEmpty || state.profiles.filter(\.enabled).isEmpty)
            }
        }
        .sheet(isPresented: $showingDispatch) { DispatchView() }
        .refreshable { await state.refresh() }
        .task(id: state.filters) {
            async let runs: Void = state.refreshRunsForFilters()
            async let statistics: Void = state.refreshContextualStatistics()
            _ = await (runs, statistics)
        }
    }

    private var statistics: some View {
        let visible = state.filteredRuns
        let hasContextFilters = state.filters.groupID != nil
            || state.filters.profileID != nil || state.filters.status != nil
        let exact = state.filters.search.trimmed.isEmpty
            ? (hasContextFilters ? state.contextualStatistics : state.globalStatistics)
            : nil
        let total = state.globalStatistics?.runs
            ?? state.snapshot?.counts.runsTotal ?? state.runs.count
        let context = exact?.runs ?? visible.count
        let active = exact.map { stats in
            ["starting", "running", "waiting"].reduce(0) {
                $0 + (stats.byStatus[$1] ?? 0)
            }
        } ?? visible.filter {
            ["starting", "running", "waiting"].contains($0.status)
        }.count
        let loadedUsage = visible.compactMap { $0.combinedUsage ?? $0.usage }
        let tokens = exact?.combinedUsage?.totalTokens
            ?? loadedUsage.compactMap(\.totalTokens).reduce(0, +)
        let cost = exact?.combinedUsage?.costUSD
            ?? loadedUsage.compactMap(\.costUSD).reduce(0, +)
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 120))], spacing: 8) {
            MetricCard(value: "\(context) / \(total)",
                       label: exact == nil ? "visible / fleet" : "context / fleet")
            MetricCard(value: active.formatted(),
                       label: exact == nil ? "active visible" : "active in context")
            MetricCard(value: tokens.formatted(),
                       label: exact == nil ? "tokens visible" : "tokens in context")
            MetricCard(value: Optional(cost).money,
                       label: exact == nil ? "metered API cost visible" : "metered API cost in context")
        }
        .padding(.vertical, 8)
    }

    private var filters: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack {
                FilterMenu(title: "Group", selected: state.filters.groupID,
                           options: state.groups.map { ($0.id, $0.name) }) {
                    state.filters.groupID = $0
                }
                FilterMenu(title: "Profile", selected: state.filters.profileID,
                           options: state.profiles.map { ($0.id, $0.name) }) {
                    state.filters.profileID = $0
                }
                FilterMenu(title: "Status", selected: state.filters.status,
                           options: ["queued", "starting", "running", "waiting",
                                     "completed", "failed", "timed_out", "stopped", "skipped"]
                            .map { ($0, $0.replacingOccurrences(of: "_", with: " ").capitalized) }) {
                    state.filters.status = $0
                }
                if !state.filters.isEmpty {
                    Button("Clear", systemImage: "xmark.circle") { state.filters = RunFilters() }
                        .buttonStyle(.borderless)
                }
            }
            .padding(.vertical, 5)
        }
    }
}

private struct FilterMenu: View {
    let title: String
    let selected: String?
    let options: [(String, String)]
    let select: (String?) -> Void

    var body: some View {
        Menu {
            Button("All") { select(nil) }
            ForEach(options, id: \.0) { id, label in
                Button {
                    select(id)
                } label: {
                    if id == selected { Label(label, systemImage: "checkmark") }
                    else { Text(label) }
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(selected.flatMap { id in options.first { $0.0 == id }?.1 } ?? title)
                Image(systemName: "chevron.down").font(.caption2)
            }
        }
        .buttonStyle(.bordered)
    }
}

private struct DispatchView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var groupID = ""
    @State private var profileID = ""
    @State private var title = ""
    @State private var request = ""
    @State private var cwd = ""
    @State private var sending = false

    private var activeGroups: [RunGroup] {
        state.groups.filter { !$0.archived }
    }

    private var workerProfiles: [Profile] {
        state.profiles.filter(\.enabled)
    }

    private var selectedGroup: RunGroup? {
        state.groups.first { $0.id == groupID }
    }

    private var placementIssue: String? {
        if !groupID.isEmpty, !activeGroups.contains(where: { $0.id == groupID }) {
            return "The selected group is no longer available."
        }
        if !profileID.isEmpty, !workerProfiles.contains(where: { $0.id == profileID }) {
            return "The selected worker profile is no longer available."
        }
        return nil
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Run") {
                    TextField("Title (optional)", text: $title)
                }
                Section("Placement") {
                    Picker("Group", selection: $groupID) {
                        if !groupID.isEmpty,
                           !activeGroups.contains(where: { $0.id == groupID }) {
                            Text("Selected group · unavailable").tag(groupID).disabled(true)
                        }
                        ForEach(activeGroups) { Text($0.name).tag($0.id) }
                    }
                    Picker("Profile", selection: $profileID) {
                        if !profileID.isEmpty,
                           !workerProfiles.contains(where: { $0.id == profileID }) {
                            Text("Selected profile · unavailable").tag(profileID).disabled(true)
                        }
                        ForEach(workerProfiles) { profile in
                            Text("\(profile.name) · \(profile.tierName)").tag(profile.id)
                        }
                    }
                    if let placementIssue {
                        Label(placementIssue, systemImage: "exclamationmark.triangle")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                Section {
                    TextEditor(text: $request).frame(minHeight: 160)
                } header: {
                    Text("Context")
                } footer: {
                    Text("Tell the run what to do and include any background it needs.")
                }
                Section {
                    TextField("Optional host path override", text: $cwd)
#if os(iOS)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
#endif
                } header: {
                    Text("Working directory")
                } footer: {
                    Text(selectedGroup?.cwdConfigured == true
                         ? "Leave blank to use this group's configured default. The saved path is private and is never shown here."
                         : "Leave blank to let Orchestra choose a managed working directory.")
                }
            }
            .navigationTitle("New run")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Run") { dispatch() }
                        .disabled(sending || groupID.isEmpty || profileID.isEmpty
                                  || request.trimmed.isEmpty || placementIssue != nil)
                }
            }
            .disabled(sending)
            .overlay { if sending { ProgressView() } }
            .task { seed() }
        }
    }

    private func seed() {
        groupID = activeGroups.first(where: { $0.slug == "general" })?.id
            ?? activeGroups.first?.id ?? ""
        profileID = workerProfiles.first?.id ?? ""
    }

    private func dispatch() {
        sending = true
        Task {
            defer { sending = false }
            do {
                let admission = try await state.api().dispatch(
                    group: groupID, profile: profileID,
                    title: title.trimmed.nilIfEmpty, request: request.trimmed,
                    cwd: cwd.trimmed.nilIfEmpty
                ).value
                await state.succeeded(admission.created
                                      ? "Created \(admission.run.display ?? "run \(admission.run.id)")"
                                      : "Reused \(admission.run.display ?? "run \(admission.run.id)")")
                dismiss()
            } catch { state.report(error) }
        }
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
