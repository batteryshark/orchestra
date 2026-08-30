import SwiftUI

struct RunwayView: View {
    @EnvironmentObject private var state: AppState
    @State private var creating = false

    var body: some View {
        Group {
            if state.runwaySources.isEmpty {
                EmptyState(icon: "gauge.with.dots.needle.67percent", title: "No runway sources",
                           message: "Profiles without a source keep running. Add named provider/account/lane sources in fleet configuration.")
            } else {
                List(state.runwaySources) { source in
                    NavigationLink { RunwayDetail(source: source) } label: {
                        VStack(alignment: .leading, spacing: 9) {
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(source.name).font(.headline)
                                    Text([source.provider, source.account, source.lane]
                                        .compactMap { $0 }.joined(separator: " · "))
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                StatusChip(status: source.fresh ? source.status : "stale")
                            }
                            ForEach(source.windows) { window in RunwayWindowRow(window: window) }
                            HStack {
                                Text("Observed \(source.observedAt.relativeAge)")
                                if let burn = source.burnRate { Text("Burn \(burn.formatted(.number.precision(.fractionLength(2))))/h") }
                                Spacer()
                                Text("\(source.linkedProfileIDs.count) profiles")
                            }.font(.caption).foregroundStyle(.secondary)
                        }.padding(.vertical, 5)
                    }
                }.listStyle(.inset)
            }
        }
        .navigationTitle("Runway")
        .toolbar {
            ServerToolbarMenu()
            ToolbarItem(placement: .automatic) {
                Button { creating = true } label: { Label("New source", systemImage: "plus") }
            }
        }
        .sheet(isPresented: $creating) { RunwaySourceEditor(source: nil) }
        .refreshable { await state.refresh() }
    }
}

private struct RunwayDetail: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let source: RunwaySource
    @State private var refreshing = false
    @State private var editing = false

    private var current: RunwaySource {
        state.runwaySources.first { $0.id == source.id } ?? source
    }

    var body: some View {
        List {
            Section {
                LabeledContent("Provider", value: current.provider)
                LabeledContent("Account", value: current.account)
                if let lane = current.lane { LabeledContent("Lane", value: lane) }
                LabeledContent("Adapter", value: current.adapter)
                LabeledContent("Enabled", value: current.enabled ? "Yes" : "No")
                LabeledContent("Command", value: current.argvConfigured ? "Configured on host" : "Not configured")
                LabeledContent("Config", value: current.configConfigured ? "Configured on host" : "Not configured")
                LabeledContent("Freshness", value: current.fresh ? "Current" : "Stale")
                LabeledContent("Observed", value: current.observedAt.relativeAge)
                if let burn = current.burnRate {
                    LabeledContent("Burn", value: "\(burn.formatted(.number.precision(.fractionLength(2)))) per hour")
                }
            } header: { Text("Source") } footer: {
                Text("Only a fresh definitive zero holds new runs. Stale, unknown, and unlinked sources never block.")
            }
            Section("Current windows") {
                if current.windows.isEmpty { Text("No observation available.").foregroundStyle(.secondary) }
                ForEach(current.windows) { RunwayWindowRow(window: $0) }
            }
            Section("Linked profiles") {
                if current.linkedProfileIDs.isEmpty { Text("None").foregroundStyle(.secondary) }
                ForEach(current.linkedProfileIDs, id: \.self) { id in
                    Text(state.profileName(id) ?? id)
                }
            }
            Section("Recent observations") {
                if let history = current.history, !history.isEmpty {
                    Text(history.map(observationLine).joined(separator: "\n"))
                    .font(.caption.monospaced()).textSelection(.enabled)
                } else {
                    Text("No retained history.").foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle(current.name)
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button { refresh() } label: { Label("Refresh source", systemImage: "arrow.clockwise") }
                    .disabled(refreshing)
            }
            ToolbarItem(placement: .automatic) {
                Menu {
                    Button("Edit source") { editing = true }
                    Button("Archive source", role: .destructive) { archive() }
                } label: { Label("Manage", systemImage: "ellipsis.circle") }
            }
        }
        .sheet(isPresented: $editing) { RunwaySourceEditor(source: current) }
    }

    private func archive() {
        Task {
            do {
                _ = try await state.api().archiveRunwaySource(source.id)
                await state.succeeded("Runway source archived")
                dismiss()
            } catch { state.report(error) }
        }
    }

    private func refresh() {
        refreshing = true
        Task {
            defer { refreshing = false }
            do {
                _ = try await state.api().refreshRunway(source.id)
                await state.succeeded("Runway refresh requested")
            } catch { state.report(error) }
        }
    }

    private func observationLine(_ observation: RunwayObservation) -> String {
        let windows = observation.windows.map { window in
            window.remainingPercent.map { remaining in
                "\(window.name) \(Int(remaining))%"
            } ?? "\(window.name) unknown"
        }.joined(separator: " · ")
        let burn = observation.burnRate.map { " · burn \(String(format: "%.2f", $0))" } ?? ""
        return "\(observation.observedAt.relativeAge) · \(windows)\(burn)"
    }
}

private struct RunwaySourceEditor: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let source: RunwaySource?
    @State private var name: String
    @State private var provider: String
    @State private var account: String
    @State private var lane: String
    @State private var adapter: String
    @State private var enabled: Bool
    @State private var argv = ""
    @State private var config = ""
    @State private var saving = false
    @State private var localError: String?

    private let adapters = ["claude", "codex", "deepseek", "kimi", "minimax", "xai", "command"]

    init(source: RunwaySource?) {
        self.source = source
        _name = State(initialValue: source?.name ?? "")
        _provider = State(initialValue: source?.provider ?? "")
        _account = State(initialValue: source?.account ?? "")
        _lane = State(initialValue: source?.lane ?? "")
        _adapter = State(initialValue: source?.adapter ?? "codex")
        _enabled = State(initialValue: source?.enabled ?? true)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Identity") {
                    TextField("Name", text: $name)
                    TextField("Provider", text: $provider)
                    TextField("Account", text: $account)
                    TextField("Lane", text: $lane)
                    Toggle("Enabled", isOn: $enabled)
                }
                Section("Adapter") {
                    Picker("Adapter", selection: $adapter) {
                        ForEach(adapters, id: \.self) { Text($0).tag($0) }
                        if !adapters.contains(adapter) { Text(adapter).tag(adapter) }
                    }
                    if adapter == "command" {
                        TextEditor(text: $argv).font(.body.monospaced()).frame(minHeight: 100)
                            .accessibilityLabel("Replacement argument vector")
                        Text(source?.adapter == "command"
                             ? "Optional replacement argv, one non-empty argument per line. Blank preserves the private host value."
                             : "The command adapter requires argv, one non-empty argument per line.")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("Built-in adapters do not accept argv. Switching from command clears its private argv atomically.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                Section("Configuration") {
                    TextEditor(text: $config).font(.body.monospaced()).frame(minHeight: 120)
                        .accessibilityLabel("Replacement JSON configuration")
                    Text(source == nil
                         ? "Optional JSON object. Do not put tokens, keys, passwords, secrets, or credentials here."
                         : "Optional replacement JSON object; blank preserves the private host value. Secret-shaped keys are rejected.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let source {
                    Section("Host state") {
                        LabeledContent("Argument vector", value: source.argvConfigured ? "Configured" : "Not configured")
                        LabeledContent("Configuration", value: source.configConfigured ? "Configured" : "Not configured")
                    }
                }
                if let localError {
                    Section { Label(localError, systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.red) }
                }
            }
            .navigationTitle(source == nil ? "New runway source" : "Edit runway source")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { save() }
                        .disabled(saving || name.trimmed.isEmpty || provider.trimmed.isEmpty || adapter.isEmpty)
                }
            }
            .disabled(saving)
        }.frame(minWidth: 460, minHeight: 620)
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            do {
                let parsedArgv = argv.split(whereSeparator: \.isNewline)
                    .map { String($0).trimmingCharacters(in: .whitespaces) }
                    .filter { !$0.isEmpty }
                let argvValue: [String]?
                if adapter == "command" {
                    if argv.trimmed.isEmpty && source?.adapter != "command" {
                        throw ValidationError("The command adapter requires at least one argument.")
                    }
                    argvValue = argv.trimmed.isEmpty ? nil : parsedArgv
                } else {
                    argvValue = source?.adapter == "command" ? [] : nil
                }
                let configValue = try replacementObject(config, label: "Configuration")
                let draft = RunwaySourceDraft(
                    name: name.trimmed, provider: provider.trimmed,
                    account: account.trimmed, lane: lane.trimmed, adapter: adapter,
                    enabled: enabled, argv: argvValue, config: configValue)
                if let source { _ = try await state.api().updateRunwaySource(source.id, source: draft) }
                else { _ = try await state.api().createRunwaySource(draft) }
                await state.succeeded("Runway source saved")
                dismiss()
            } catch { localError = error.localizedDescription }
        }
    }
}

private struct ValidationError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

private struct RunwayWindowRow: View {
    let window: RunwayWindow
    private var remaining: Double? {
        window.remainingPercent.map { min(100, max(0, $0)) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(window.name).font(.subheadline.weight(.semibold))
                Spacer()
                Text(remaining.map { "\(Int($0))% · resets \(window.resetsAt.relativeAge)" }
                     ?? "Unknown · resets \(window.resetsAt.relativeAge)")
                    .font(.caption).foregroundStyle(remaining == 0 ? .red : .secondary)
            }
            if let remaining {
                ProgressView(value: remaining, total: 100)
                    .tint(remaining == 0 ? .red : remaining < 20 ? .orange : .green)
                    .accessibilityLabel("\(window.name) remaining")
                    .accessibilityValue("\(Int(remaining)) percent")
            } else {
                ProgressView().accessibilityLabel("\(window.name) capacity is unknown")
            }
        }
    }
}
