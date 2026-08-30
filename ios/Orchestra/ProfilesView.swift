import SwiftUI

struct ProfilesView: View {
    @EnvironmentObject private var state: AppState
    @State private var editing: Profile?
    @State private var creating = false
    @State private var discovery: ProfileDiscovery?
    @State private var discovering = false

    var body: some View {
        Group {
            if state.profiles.isEmpty {
                EmptyState(icon: "person.crop.rectangle.stack", title: "No profiles",
                           message: "Create an explicit runtime and model configuration for runs.")
            } else {
                List(state.profiles) { profile in
                    Button { editing = profile } label: {
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    Text(profile.name).font(.headline).foregroundStyle(.primary)
                                    StatusChip(status: profile.enabled ? "enabled" : "disabled")
                                    StatusChip(status: profile.observerReady
                                               ? "observer ready" : "worker only")
                                }
                                Text(profile.model ?? "Runtime default model")
                                    .font(.body.monospaced()).foregroundStyle(.primary)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                HStack(spacing: 6) {
                                    Text(state.runtimeName(profile.runtimeID) ?? profile.runtimeID)
                                    Text(profile.effort.map { "\($0) effort" } ?? "default effort")
                                    Text(profile.tierName)
                                    if let source = profile.runwaySourceID,
                                       let name = state.runwaySources.first(where: { $0.id == source })?.name {
                                        Text(name)
                                    }
                                }.font(.caption2).foregroundStyle(.secondary)
                                if profile.envConfigured != nil || profile.configConfigured != nil {
                                    Text("Host env \(profile.envConfigured.map { $0 ? "configured" : "unset" } ?? "unknown") · config \(profile.configConfigured.map { $0 ? "configured" : "unset" } ?? "unknown")")
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                                if let issue = profile.observerIssue {
                                    Text("Observer unavailable: \(issue)")
                                        .font(.caption2).foregroundStyle(.orange)
                                }
                            }
                            Spacer()
                            Image(systemName: "chevron.right").foregroundStyle(.tertiary)
                        }.padding(.vertical, 5)
                    }.buttonStyle(.plain)
                }
            }
        }
        .navigationTitle("Profiles")
        .toolbar {
            ServerToolbarMenu()
            ToolbarItem(placement: .automatic) {
                Button { creating = true } label: { Label("New profile", systemImage: "plus") }
                    .disabled(state.runtimes.isEmpty)
            }
            ToolbarItem(placement: .automatic) {
                Menu {
                    Button("Harness models") { discover(local: false) }
                    Button("Include host-local models") { discover(local: true) }
                } label: {
                    Label(discovering ? "Discovering…" : "Discover", systemImage: "sparkle.magnifyingglass")
                }.disabled(discovering)
            }
        }
        .sheet(item: $editing) { ProfileEditor(profile: $0, creating: false) }
        .sheet(isPresented: $creating) {
            if let runtime = state.runtimes.first {
                ProfileEditor(profile: .init(
                    id: "", name: "", runtimeID: runtime.id, model: nil,
                    effort: nil, tier: 1, priority: 0, activeCap: nil,
                    runwaySourceID: nil, note: nil, enabled: true
                ), creating: true)
            }
        }
        .sheet(isPresented: Binding(
            get: { discovery != nil },
            set: { if !$0 { discovery = nil } }
        )) {
            if let discovery { ProfileDiscoveryView(discovery: discovery) }
        }
        .refreshable { await state.refresh() }
    }

    private func discover(local: Bool) {
        discovering = true
        Task {
            defer { discovering = false }
            discovery = await state.discoverProfiles(local: local)
        }
    }
}

private struct ProfileDiscoveryView: View {
    @Environment(\.dismiss) private var dismiss
    let discovery: ProfileDiscovery

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(discovery.localRequested
                         ? "Harness catalogs and models served on the Orchestra host."
                         : "Harness catalogs only. Host-local inference was not probed.")
                        .foregroundStyle(.secondary)
                }
                runtimeSection("Codex", error: discovery.runtimes.codex.error) {
                    if let models = discovery.runtimes.codex.data, !models.isEmpty {
                        ForEach(models) { model in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(model.model).font(.body.monospaced())
                                Text(effortSummary(model.efforts, model.defaultEffort))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    } else { noModels }
                }
                runtimeSection("OpenCode", error: discovery.runtimes.opencode.error) {
                    if let providers = discovery.runtimes.opencode.data, !providers.isEmpty {
                        ForEach(providers.keys.sorted(), id: \.self) { provider in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(provider).font(.headline)
                                Text(providers[provider, default: []].joined(separator: " · "))
                                    .font(.caption.monospaced()).foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                            }
                        }
                    } else { noModels }
                }
                runtimeSection("Reasonix", error: discovery.runtimes.reasonix.error) {
                    if let providers = discovery.runtimes.reasonix.data, !providers.isEmpty {
                        ForEach(providers) { provider in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(provider.provider).font(.headline)
                                Text(provider.models.joined(separator: " · "))
                                    .font(.caption.monospaced()).textSelection(.enabled)
                                Text(effortSummary(provider.efforts, provider.defaultEffort))
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                        }
                    } else { noModels }
                }
                runtimeSection("Claude", error: discovery.runtimes.claude.error) { noModels }
                Section("Host-local inference") {
                    if discovery.localModels.isEmpty {
                        Text(discovery.localRequested ? "No local models returned." : "Not requested.")
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(discovery.localModels) { model in
                            LabeledContent(model.id, value: model.source)
                        }
                    }
                }
                Section {
                    Text("Discovery is read-only. Choose a model explicitly when creating or editing a profile.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Available models")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }.frame(minWidth: 520, minHeight: 580)
    }

    @ViewBuilder private func runtimeSection<Content: View>(
        _ title: String, error: String?, @ViewBuilder content: () -> Content
    ) -> some View {
        Section(title) {
            if let error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            } else { content() }
        }
    }

    private var noModels: some View {
        Text("No models returned.").foregroundStyle(.secondary)
    }

    private func effortSummary(_ efforts: [String], _ defaultEffort: String?) -> String {
        let values = efforts.isEmpty ? "No effort levels" : efforts.joined(separator: ", ")
        return defaultEffort.map { "\(values) · default \($0)" } ?? values
    }
}

private struct ProfileEditor: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var draft: Profile
    @State private var env = ""
    @State private var config = ""
    @State private var customEffort: Bool
    let creating: Bool
    @State private var saving = false

    private static let standardEfforts = [
        "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
    ]

    init(profile: Profile, creating: Bool) {
        _draft = State(initialValue: profile)
        _customEffort = State(initialValue: profile.effort.map {
            !Self.standardEfforts.contains($0)
        } ?? false)
        self.creating = creating
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Identity") {
                    TextField("Name", text: $draft.name)
                    Toggle("Enabled", isOn: $draft.enabled)
                    TextField("Operator note", text: Binding(
                        get: { draft.note ?? "" },
                        set: { draft.note = $0.trimmed.isEmpty ? nil : $0 }
                    ), axis: .vertical)
                }
                Section("Runtime") {
                    Picker("Runtime", selection: $draft.runtimeID) {
                        ForEach(state.runtimes.filter(\.enabled)) { Text($0.name).tag($0.id) }
                    }
                }
                Section("Model") {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("Model identifier").font(.caption).foregroundStyle(.secondary)
                        TextField("Runtime default", text: optional($draft.model))
                            .textFieldStyle(.roundedBorder)
                            .frame(maxWidth: .infinity)
                    }
                    Picker("Reasoning effort", selection: effortSelection) {
                        Text("Runtime default").tag("")
                        ForEach(Self.standardEfforts, id: \.self) { effort in
                            Text(effortLabel(effort)).tag(effort)
                        }
                        Text("Custom…").tag("__custom")
                    }
                    if customEffort {
                        TextField("Custom effort", text: optional($draft.effort))
                            .textFieldStyle(.roundedBorder)
                    }
                }
                Section("Launch overrides") {
                    TextEditor(text: $env).font(.body.monospaced()).frame(minHeight: 90)
                        .accessibilityLabel("Replacement environment JSON")
                    Text("Environment JSON must be an object with string values.")
                        .font(.caption).foregroundStyle(.secondary)
                    TextEditor(text: $config).font(.body.monospaced()).frame(minHeight: 90)
                        .accessibilityLabel("Replacement profile configuration JSON")
                    Text(creating
                         ? "Optional configuration JSON object. Credential-shaped fields are rejected."
                         : "Blank preserves each private host value; {} clears it. Existing values are never returned or prefilled.")
                        .font(.caption).foregroundStyle(.secondary)
                    if !creating {
                        LabeledContent("Host environment",
                                       value: draft.envConfigured.map { $0 ? "Configured" : "Not configured" } ?? "Unknown")
                        LabeledContent("Host configuration",
                                       value: draft.configConfigured.map { $0 ? "Configured" : "Not configured" } ?? "Unknown")
                    }
                }
                Section("Fleet tier") {
                    Picker("Tier", selection: $draft.tier) {
                        Text("Workhorse").tag(1)
                        Text("Core").tag(2)
                        Text("Frontier").tag(3)
                    }
                    Text("Workhorse handles routine volume, Core is the default balance, and Frontier is reserved for the hardest runs.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Runway") {
                    Picker("Source", selection: Binding(
                        get: { draft.runwaySourceID ?? "" },
                        set: { draft.runwaySourceID = $0.isEmpty ? nil : $0 }
                    )) {
                        Text("Unlinked").tag("")
                        ForEach(state.runwaySources) { source in
                            Text("\(source.name) · \(source.account)").tag(source.id)
                        }
                    }
                    Text("Only a fresh definitive zero holds new runs. Orchestra never substitutes another profile.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .navigationTitle(creating ? "New profile" : draft.name)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { save() }
                        .disabled(saving || draft.name.trimmed.isEmpty || draft.runtimeID.isEmpty)
                }
            }
            .disabled(saving)
            .overlay { if saving { ProgressView() } }
        }
        .frame(minWidth: 420, minHeight: 520)
    }

    private func optional(_ binding: Binding<String?>) -> Binding<String> {
        Binding(get: { binding.wrappedValue ?? "" },
                set: { binding.wrappedValue = $0.trimmed.isEmpty ? nil : $0 })
    }

    private var effortSelection: Binding<String> {
        Binding(
            get: { customEffort ? "__custom" : draft.effort ?? "" },
            set: { value in
                customEffort = value == "__custom"
                if !customEffort { draft.effort = value.isEmpty ? nil : value }
            }
        )
    }

    private func effortLabel(_ effort: String) -> String {
        switch effort {
        case "none": "None"
        case "xhigh": "Extra high"
        case "max": "Maximum"
        case "ultra": "Ultra"
        default: effort.capitalized
        }
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            do {
                let envValue = try replacementStringMap(
                    env, label: "Environment (with string values)")
                let configValue = try replacementObject(config, label: "Configuration")
                if creating {
                    _ = try await state.api().createProfile(
                        draft, env: envValue, config: configValue)
                } else {
                    _ = try await state.api().updateProfile(
                        draft, env: envValue, config: configValue)
                }
                await state.succeeded("Profile saved")
                dismiss()
            } catch { state.report(error) }
        }
    }
}

struct GroupsView: View {
    @EnvironmentObject private var state: AppState
    @State private var newName = ""
    @State private var newCWD = ""
    @State private var rename: RunGroup?
    @State private var statistics: [String: RunStatistics] = [:]

    var body: some View {
        List {
            Section("Create group") {
                HStack {
                    TextField("Research", text: $newName)
                    Button("Create") { create() }.disabled(newName.trimmed.isEmpty)
                }
                TextField("Default host working directory (optional)", text: $newCWD)
#if os(iOS)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
#endif
                Text("Groups organize and number runs. A private default working directory can be inherited by future runs.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Groups") {
                ForEach(state.groups) { group in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(group.name).font(.headline)
                                    if group.archived { StatusChip(status: "archived") }
                                }
                                Text("\(group.runsCount ?? 0) runs · next #\(group.nextNumber ?? 1) · \(group.slug)")
                                    .font(.caption).foregroundStyle(.secondary)
                                Text(group.cwdConfigured == true
                                     ? "Default working directory configured"
                                     : "Managed working directory")
                                    .font(.caption2).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if group.slug == "general" {
                                Text("Permanent default")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Button(group.slug == "general" ? "Working directory" : "Edit") {
                                rename = group
                            }
                            if group.slug != "general", !group.archived {
                                Button("Archive", role: .destructive) { archive(group) }
                            }
                        }
                        if let stats = statistics[group.id] {
                            GroupStatistics(stats: stats)
                        } else {
                            HStack {
                                ProgressView().controlSize(.small)
                                Text("Loading statistics…")
                            }
                            .font(.caption).foregroundStyle(.secondary)
                        }
                    }.padding(.vertical, 4)
                }
            }
        }
        .navigationTitle("Groups")
        .toolbar { ServerToolbarMenu() }
        .sheet(item: $rename) { group in RenameGroupView(group: group) }
        .refreshable {
            await state.refresh()
            await loadStatistics()
        }
        .task(id: state.groups.map(\.id).joined(separator: ",")) {
            await loadStatistics()
        }
    }

    private func create() {
        Task {
            do {
                _ = try await state.api().createGroup(
                    name: newName.trimmed,
                    cwd: newCWD.trimmed.isEmpty ? nil : newCWD.trimmed)
                newName = ""
                newCWD = ""
                await state.succeeded("Group created")
            } catch { state.report(error) }
        }
    }

    private func archive(_ group: RunGroup) {
        Task {
            do {
                _ = try await state.api().updateGroup(group.id, archived: true)
                await state.succeeded("Group archived")
            } catch { state.report(error) }
        }
    }

    private func loadStatistics() async {
        guard !state.groups.isEmpty, let client = try? state.api() else { return }
        let groupIDs = state.groups.map(\.id)
        await withTaskGroup(of: (String, RunStatistics)?.self) { tasks in
            for id in groupIDs {
                tasks.addTask {
                    guard let value = try? await client.statistics(group: id) else { return nil }
                    return (id, value.value)
                }
            }
            for await result in tasks {
                if let (id, value) = result { statistics[id] = value }
            }
        }
    }
}

private struct GroupStatistics: View {
    let stats: RunStatistics

    var body: some View {
        let usage = stats.combinedUsage
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 105))], spacing: 8) {
            Fact(label: "Runs", value: stats.runs.formatted())
            Fact(label: "Agent time", value: (stats.agentSeconds ?? 0).agentDuration)
            Fact(label: "Input tokens", value: (usage?.inputTokens ?? 0).formatted())
            Fact(label: "Output tokens", value: (usage?.outputTokens ?? 0).formatted())
            Fact(label: "Total tokens", value: (usage?.totalTokens ?? 0).formatted())
            Fact(label: "Metered API cost", value: usage?.costUSD.money ?? "—")
        }
    }
}

private extension Int {
    var agentDuration: String {
        let hours = self / 3_600
        let minutes = self % 3_600 / 60
        return hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
    }
}

private struct RenameGroupView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let group: RunGroup
    @State private var name: String
    @State private var cwd = ""
    @State private var clearCWD = false
    @State private var saving = false

    init(group: RunGroup) { self.group = group; _name = State(initialValue: group.name) }

    var body: some View {
        NavigationStack {
            Form {
                Section("Group") {
                    TextField("Name", text: $name)
                        .disabled(group.slug == "general")
                }
                Section("Default working directory") {
                    TextField("New host path", text: $cwd)
#if os(iOS)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
#endif
                        .disabled(clearCWD)
                    if group.cwdConfigured == true {
                        Toggle("Clear configured default", isOn: $clearCWD)
                    }
                    Text(group.cwdConfigured == true
                         ? "The current path is private and is never prefilled. Blank preserves it; enter a replacement or clear it explicitly."
                         : "Blank keeps Orchestra's managed working directory behavior.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
                .navigationTitle(group.slug == "general" ? "General" : "Edit group")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") { save() }
                            .disabled(saving || name.trimmed.isEmpty)
                    }
                }
                .disabled(saving)
        }.frame(minWidth: 420, minHeight: 320)
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            do {
                let client = try state.api()
                if group.slug != "general", name.trimmed != group.name {
                    _ = try await client.updateGroup(group.id, name: name.trimmed)
                }
                if clearCWD {
                    _ = try await client.updateGroupCWD(group.id, cwd: nil)
                } else if !cwd.trimmed.isEmpty {
                    _ = try await client.updateGroupCWD(group.id, cwd: cwd.trimmed)
                }
                await state.succeeded("Group saved")
                dismiss()
            } catch { state.report(error) }
        }
    }
}
