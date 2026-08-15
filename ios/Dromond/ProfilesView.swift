import SwiftUI

/// Profiles are GLOBAL presets — one list, shared by every project. What a
/// project picks is not a profile of its own but WHICH of these it may staff a
/// run with. Both halves are on this screen, and kept apart on purpose.
struct ProfilesView: View {
    @EnvironmentObject private var state: AppState
    @State private var editing: Profile?
    @State private var options: [String: HarnessOptions] = [:]
    /// The selected project's enabled set. `nil` means the project has not
    /// said, which the daemon reads as "every profile".
    @State private var enabled: [String]?
    @State private var enabledFor: String?
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                if let error {
                    Section { Text(error).font(.footnote).foregroundStyle(.red) }
                }
                if let project = state.selectedProject {
                    enabledSection(project)
                }
                ForEach(tiers, id: \.tier) { group in
                    Section {
                        ForEach(group.profiles) { profile in
                            Button { editing = profile } label: {
                                ProfileRow(profile: profile)
                            }
                            .buttonStyle(.plain)
                        }
                    } header: {
                        Text("Tier \(group.tier) · \(group.name)")
                    }
                }
                if state.profiles.isEmpty {
                    Section { Text("No profiles configured.").foregroundStyle(.secondary) }
                }
            }
            .navigationTitle("Profiles")
            .toolbar { ProjectToolbarMenu() }
            .refreshable { await state.refresh() }
            .sheet(item: $editing) { profile in
                ProfileEditor(profile: profile, options: options, others: otherNames(profile))
            }
            .task { await loadOptions() }
            .task(id: state.selectedProjectID) { await loadEnabled() }
        }
    }

    // --- the enabled set --------------------------------------------------

    @ViewBuilder
    private func enabledSection(_ project: Project) -> some View {
        Section {
            if enabled == nil {
                Text("Every profile is available to \(project.name). Turn one off to narrow the list.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            ForEach(state.profiles) { profile in
                Toggle(isOn: Binding(
                    get: { enabled?.contains(profile.name) ?? true },
                    set: { on in Task { await setEnabled(profile.name, on) } }
                )) {
                    HStack(spacing: 8) {
                        Text(profile.name)
                        Text(profile.model).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                .disabled(busy)
            }
        } header: {
            Text("\(project.name) may staff runs with")
        } footer: {
            Text("The profiles above are global. This chooses which of them this project is allowed to use.")
        }
    }

    private func setEnabled(_ name: String, _ on: Bool) async {
        let all = state.profiles.map(\.name)
        var names = Set(enabled ?? all)
        if on { names.insert(name) } else { names.remove(name) }
        // An "all on" set is written as no list at all: a literal list of
        // every current name goes stale the moment an eleventh profile is
        // added, and would silently exclude it.
        let next: [String]? = names.count == all.count ? nil : all.filter(names.contains)
        guard let projectID = state.selectedProjectID else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await state.api().setEnabledProfiles(
                projectID: projectID, names: next)
            if let problem = result.error, !problem.isEmpty {
                error = problem
                return
            }
            enabled = next
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func loadEnabled() async {
        guard let projectID = state.selectedProjectID else {
            enabled = nil
            enabledFor = nil
            return
        }
        guard enabledFor != projectID else { return }
        do {
            let detail = try await state.api().project(id: projectID)
            enabled = detail.enabledProfiles
            enabledFor = projectID
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func loadOptions() async {
        // A failed discovery is not an error worth a banner: the editor falls
        // back to typing the model, which is what the claude harness needs
        // anyway.
        options = (try? await state.api().profileOptions()) ?? [:]
    }

    // --- grouping ---------------------------------------------------------

    private struct TierGroup { let tier: Int; let name: String; let profiles: [Profile] }

    private var tiers: [TierGroup] {
        Dictionary(grouping: state.profiles) { $0.tier ?? 0 }
            .map { tier, profiles in
                TierGroup(
                    tier: tier,
                    name: profiles.first?.tierName ?? (tier == 0 ? "untiered" : ""),
                    profiles: profiles.sorted {
                        ($0.priority ?? 99, $0.name) < ($1.priority ?? 99, $1.name)
                    }
                )
            }
            .sorted { ($0.tier == 0 ? 99 : $0.tier) < ($1.tier == 0 ? 99 : $1.tier) }
    }

    private func otherNames(_ profile: Profile) -> [String] {
        state.profiles.map(\.name).filter { $0 != profile.name }.sorted()
    }
}

private struct ProfileRow: View {
    let profile: Profile

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text(profile.name).font(.headline)
                Spacer()
                if let priority = profile.priority {
                    Text("priority \(priority)").font(.caption2).foregroundStyle(.secondary)
                }
                Image(systemName: "chevron.right").font(.caption2).foregroundStyle(.tertiary)
            }
            HStack(spacing: 6) {
                Tag(text: profile.backend, tint: .blue)
                if let effort = profile.effort, !effort.isEmpty {
                    Tag(text: effort, tint: .purple)
                }
                if let variant = profile.variant, !variant.isEmpty {
                    Tag(text: variant, tint: .teal)
                }
            }
            Text(profile.model.isEmpty ? "harness default model" : profile.model)
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
            if let role = profile.role, !role.isEmpty {
                Text(role).font(.caption).foregroundStyle(.secondary)
            }
            Text(profile.spawnProfiles.isEmpty
                 ? "Delegates to: nothing"
                 : "Delegates to: " + profile.spawnProfiles.joined(separator: ", "))
                .font(.caption2)
                .foregroundStyle(.secondary)
            if let note = profile.note, !note.isEmpty {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "note.text").font(.caption2).foregroundStyle(.orange)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(note).font(.caption)
                        if let age = profile.noteAge, !age.isEmpty {
                            Text(age).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
                .padding(8)
                .background(.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 8))
            }
        }
        .padding(.vertical, 4)
        .contentShape(Rectangle())
    }
}

private struct Tag: View {
    let text: String
    var tint: Color = .secondary

    var body: some View {
        Text(text)
            .font(.caption2.weight(.bold))
            .foregroundStyle(tint)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(tint.opacity(0.14), in: Capsule())
    }
}

// --- the editor -------------------------------------------------------------

private struct ProfileEditor: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let profile: Profile
    let options: [String: HarnessOptions]
    let others: [String]

    @State private var harness = ""
    @State private var model = ""
    @State private var effort = ""
    @State private var tier = 2
    @State private var priority = 10
    @State private var spawn: Set<String> = []
    @State private var saving = false
    @State private var error: String?

    /// Every effort the daemon ranks, for a harness that takes one but
    /// publishes no model listing (claude).
    private static let allEfforts = ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    private static let fallbackHarnesses = ["opencode", "codex", "claude", "reasonix"]

    private var harnesses: [String] {
        let known = options.keys.sorted()
        return known.isEmpty ? Self.fallbackHarnesses : known
    }
    private var current: HarnessOptions? { options[harness] }
    private var models: [HarnessModel] { current?.models ?? [] }
    private var supportsEffort: Bool { current?.supportsEffort ?? true }
    private var efforts: [String] {
        let listed = models.first { $0.id == model }?.efforts ?? []
        return listed.isEmpty ? Self.allEfforts : listed
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Harness", selection: $harness) {
                        ForEach(harnesses, id: \.self) { Text($0).tag($0) }
                    }
                    if models.isEmpty {
                        TextField("Model", text: $model)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                    } else {
                        Picker("Model", selection: $model) {
                            Text("harness default").tag("")
                            ForEach(models) { Text($0.id).tag($0.id) }
                        }
                    }
                } header: {
                    Text("Harness and model")
                } footer: {
                    if let problem = current?.error, !problem.isEmpty {
                        Text("Discovery: \(problem)")
                    } else if models.isEmpty {
                        Text("This harness publishes no model listing, so the model is typed.")
                    }
                }

                Section {
                    // No control at all when the harness takes no effort: the
                    // daemon rejects one, so offering the field would only
                    // produce an error the owner could not have avoided.
                    if supportsEffort {
                        Picker("Effort", selection: $effort) {
                            Text("model default").tag("")
                            ForEach(efforts, id: \.self) { Text($0).tag($0) }
                        }
                    } else {
                        Text(current?.effortNote ?? "This harness takes no reasoning effort.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Reasoning effort")
                }

                Section("Routing") {
                    Picker("Tier", selection: $tier) {
                        Text("1 · workhorse").tag(1)
                        Text("2 · generalist").tag(2)
                        Text("3 · heavy").tag(3)
                    }
                    Stepper("Priority \(priority)", value: $priority, in: 0...99)
                }

                Section {
                    if others.isEmpty {
                        Text("No other profiles to delegate to.").foregroundStyle(.secondary)
                    }
                    ForEach(others, id: \.self) { name in
                        Toggle(name, isOn: Binding(
                            get: { spawn.contains(name) },
                            set: { on in
                                if on { spawn.insert(name) } else { spawn.remove(name) }
                            }
                        ))
                    }
                } header: {
                    Text("May delegate to")
                } footer: {
                    Text("Nothing selected means this profile spawns no runs of its own.")
                }

                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .navigationTitle(profile.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await save() } }
                        .disabled(saving || edit.payload.isEmpty)
                }
            }
            .onAppear(perform: load)
            .onChange(of: harness) { _, _ in
                // A model or effort from the old harness is not valid on the
                // new one, and the daemon refuses it rather than guessing.
                if !models.isEmpty, !models.contains(where: { $0.id == model }) { model = "" }
                if !supportsEffort { effort = "" }
                if !effort.isEmpty, !efforts.contains(effort) { effort = "" }
            }
            .onChange(of: model) { _, _ in
                if !effort.isEmpty, !efforts.contains(effort) { effort = "" }
            }
        }
    }

    private func load() {
        harness = profile.backend
        model = profile.model
        effort = profile.effort ?? ""
        tier = profile.tier ?? 2
        priority = profile.priority ?? 10
        spawn = Set(profile.spawnProfiles)
    }

    /// Only what the owner actually changed. Sending an untouched field would
    /// rewrite it, and an empty change set is a 400 the daemon is right to
    /// return — which is why Save is disabled until this has something in it.
    private var edit: ProfileEdit {
        var edit = ProfileEdit()
        if harness != profile.backend { edit.backend = harness }
        if model != profile.model { edit.model = model }
        if effort != (profile.effort ?? "") { edit.effort = effort }
        if tier != profile.tier { edit.tier = tier }
        if priority != profile.priority { edit.priority = priority }
        if spawn != Set(profile.spawnProfiles) { edit.spawnProfiles = spawn.sorted() }
        return edit
    }

    private func save() async {
        saving = true
        defer { saving = false }
        do {
            let result = try await state.api().saveProfile(name: profile.name, edit: edit)
            // An edit an agent may not make comes back applied: false with the
            // reason, and a 200 — so the reply decides, not the status code.
            if let problem = result.error, !problem.isEmpty {
                error = problem
                return
            }
            await state.refresh()
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
