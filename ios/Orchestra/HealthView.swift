import SwiftUI

private enum InboxMode: String, CaseIterable, Identifiable {
    case attention = "Attention"
    case messages = "Inbox / Outbox"
    var id: String { rawValue }
}

struct InboxView: View {
    @EnvironmentObject private var state: AppState
    @State private var kind: String?
    @State private var responding: AttentionItem?
    @State private var mode: InboxMode = .attention
    @State private var selectedRun: Run?

    private var visible: [AttentionItem] {
        guard let kind else { return state.inbox }
        return state.inbox.filter { $0.kind == kind }
    }

    var body: some View {
        List {
            Section {
                Picker("Inbox view", selection: $mode) {
                    ForEach(InboxMode.allCases) { Text($0.rawValue).tag($0) }
                }.pickerStyle(.segmented)
            }
            if mode == .attention {
                Section {
                    Picker("Kind", selection: $kind) {
                        Text("All").tag(String?.none)
                        ForEach(Array(Set(state.inbox.map(\.kind))).sorted(), id: \.self) {
                            Text($0.replacingOccurrences(of: "_", with: " ").capitalized)
                                .tag(String?.some($0))
                        }
                    }.pickerStyle(.menu)
                }
                if visible.isEmpty {
                    Section {
                        ContentUnavailableView("Inbox zero", systemImage: "tray",
                            description: Text("No questions, profile proposals, or alerts need attention."))
                    }
                } else {
                    ForEach(visible) { item in
                        AttentionRow(item: item) { responding = item }
                    }
                    if state.inboxCursor != nil {
                        Button("Load more") { Task { await state.loadMoreInbox() } }
                    }
                }
            } else {
                Section { messageMetrics.listRowInsets(.init()) }
                Section("Filters") {
                    Picker("Direction", selection: $state.messageFilters.direction) {
                        Text("All directions").tag(String?.none)
                        Text("Operator → run").tag(String?.some("inbound"))
                        Text("Run → operator").tag(String?.some("outbound"))
                        Text("System").tag(String?.some("system"))
                    }
                    Picker("Delivery", selection: $state.messageFilters.status) {
                        Text("Every receipt").tag(String?.none)
                        ForEach(["pending", "delivered", "undeliverable"], id: \.self) {
                            Text($0.capitalized).tag(String?.some($0))
                        }
                    }
                }
                Section("Durable message ledger") {
                    if state.messages.isEmpty {
                        ContentUnavailableView("No matching messages", systemImage: "tray.2",
                            description: Text("Inbound, outbound, and system messages appear here with delivery receipts."))
                    }
                    ForEach(state.messages) { message in messageRow(message) }
                    if state.messageCursor != nil {
                        Button("Load more messages") { Task { await state.loadMoreMessages() } }
                    }
                }
            }
        }
        .listStyle(.inset)
        .navigationTitle("Inbox")
        .toolbar { ServerToolbarMenu() }
        .sheet(item: $responding) { AttentionResponseView(item: $0) }
        .navigationDestination(item: $selectedRun) { RunDetailView(run: $0) }
        .refreshable { await state.refresh() }
        .task(id: state.messageFilters) { await state.refreshMessages() }
    }

    private var messageMetrics: some View {
        let counts = state.snapshot?.messages ?? .init()
        return LazyVGrid(columns: [GridItem(.adaptive(minimum: 110))], spacing: 8) {
            MetricCard(value: counts.total.formatted(), label: "messages")
            MetricCard(value: counts.pending.formatted(), label: "pending")
            MetricCard(value: counts.undeliverable.formatted(), label: "undeliverable")
            MetricCard(value: counts.outbound.formatted(), label: "outbound")
        }.padding(.vertical, 8)
    }

    @ViewBuilder private func messageRow(_ message: RunMessage) -> some View {
        if let run = state.runs.first(where: { $0.id == message.runID }) {
            NavigationLink { RunDetailView(run: run) } label: {
                VStack(alignment: .leading, spacing: 5) {
                Text(message.display ?? run.display ?? "Run \(run.id)").font(.caption.bold())
                    MessageReceiptRow(message: message)
                }
            }
        } else {
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(message.display ?? "Run \(message.runID)").font(.caption.bold())
                    Spacer()
                    Button("Open run") { openRun(message.runID) }
                }
                MessageReceiptRow(message: message)
            }
        }
    }

    private func openRun(_ id: Int) {
        Task {
            do { selectedRun = try await state.api().run(id).value }
            catch { state.report(error) }
        }
    }
}

private struct AttentionRow: View {
    @EnvironmentObject private var state: AppState
    let item: AttentionItem
    let respond: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                StatusChip(status: item.kind)
                if item.blocking { Label("Blocking", systemImage: "pause.circle").foregroundStyle(.orange) }
                Spacer()
                Text(item.openedAt.relativeAge).font(.caption).foregroundStyle(.secondary)
            }
            Text(item.prompt ?? item.message ?? "Attention")
                .font(.headline)
            if let detail = item.detail { Text(detail).foregroundStyle(.secondary) }
            HStack {
                if let runID = item.runID {
                    Text("Run \(runID)").font(.caption.monospaced()).foregroundStyle(.secondary)
                }
                Spacer()
                if item.kind == "profile_proposal" {
                    Button("Reject") { decide(false) }
                    Button("Approve") { decide(true) }.buttonStyle(.borderedProminent)
                } else if item.kind == "alert" {
                    Button("Acknowledge") { acknowledge() }
                } else {
                    Button("Answer", action: respond).buttonStyle(.borderedProminent)
                }
            }
        }.padding(.vertical, 5)
    }

    private func decide(_ approve: Bool) {
        Task {
            do {
                _ = try await state.api().decideProposal(item.id, approve: approve)
                await state.succeeded(approve ? "Proposal approved" : "Proposal rejected")
            } catch { state.report(error) }
        }
    }

    private func acknowledge() {
        Task {
            do {
                _ = try await state.api().acknowledge(item.id)
                await state.succeeded("Alert acknowledged")
            } catch { state.report(error) }
        }
    }
}

private struct AttentionResponseView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let item: AttentionItem
    @State private var answer = ""
    @State private var selectedChoice: String?
    @State private var sending = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text(item.prompt ?? item.message ?? "Question").font(.headline)
                    if let detail = item.detail { Text(detail).foregroundStyle(.secondary) }
                }
                if !item.choices.isEmpty {
                    Section("Choices") {
                        Picker("Choice", selection: $selectedChoice) {
                            Text("Choose…").tag(String?.none)
                            ForEach(item.choices) { Text($0.label).tag(String?.some($0.id)) }
                        }.pickerStyle(.inline)
                    }
                }
                Section("Answer") { TextEditor(text: $answer).frame(minHeight: 120) }
                if let deadline = item.deadline { Section { LabeledContent("Deadline", value: deadline) } }
                if let fallback = item.fallback {
                    Section { LabeledContent("Fallback", value: fallback.description) }
                }
            }
            .navigationTitle("Answer")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") { send() }
                        .disabled(sending || (answer.trimmed.isEmpty && selectedChoice == nil))
                }
            }
            .disabled(sending)
        }.frame(minWidth: 380, minHeight: 420)
    }

    private func send() {
        sending = true
        let choice = item.choices.first { $0.id == selectedChoice }
        let response = answer.trimmed.isEmpty ? choice?.label ?? "" : answer.trimmed
        Task {
            defer { sending = false }
            do {
                _ = try await state.api().answer(attentionID: item.id, answer: response,
                                                 choice: selectedChoice)
                await state.succeeded("Answer recorded")
                dismiss()
            } catch { state.report(error) }
        }
    }
}

struct FleetView: View {
    @EnvironmentObject private var state: AppState
    @State private var editingRuntime: RuntimeConfig?
    @State private var creatingRuntime = false

    var body: some View {
        List {
            Section { metrics.listRowInsets(.init()) }
            Section("Scheduler") {
                LabeledContent("Admission", value: state.snapshot?.scheduler.paused == true ? "Paused" : "FIFO")
                LabeledContent("Active", value: state.snapshot?.scheduler.active.formatted() ?? "0")
                LabeledContent("Queued", value: state.snapshot?.scheduler.queued.formatted() ?? "0")
                LabeledContent("Global capacity", value: state.snapshot?.scheduler.maxActive.formatted() ?? "—")
                Button(state.snapshot?.scheduler.paused == true ? "Resume starts" : "Pause new starts") {
                    setPaused(state.snapshot?.scheduler.paused != true)
                }
            }
            Section("Message delivery") {
                let messages = state.snapshot?.messages ?? .init()
                LabeledContent("Total", value: messages.total.formatted())
                LabeledContent("Pending", value: messages.pending.formatted())
                LabeledContent("Delivered", value: messages.delivered.formatted())
                LabeledContent("Undeliverable", value: messages.undeliverable.formatted())
                    .foregroundStyle(messages.undeliverable > 0 ? .red : .primary)
            }
            Section("Fleet settings") {
                NavigationLink("Capacity and delegation") { FleetSettingsView() }
                Text("Global admission and child-run bounds are fleet policy.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Runtimes") {
                ForEach(state.runtimes) { runtime in
                    Button { editingRuntime = runtime } label: {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(runtime.name).foregroundStyle(.primary)
                                Text("\(runtime.kind) · \(runtime.argv.joined(separator: " "))")
                                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                if let configured = runtime.configConfigured {
                                    Text("Host config \(configured ? "configured" : "unset")")
                                        .font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            StatusChip(status: runtime.enabled ? "enabled" : "disabled")
                        }
                    }.buttonStyle(.plain)
                }
                Button("Add exec / ACP runtime", systemImage: "plus") { creatingRuntime = true }
            }
            Section("Daemon") {
                LabeledContent("Status", value: state.snapshot?.daemon.status ?? "unknown")
                LabeledContent("Last tick", value: state.snapshot?.daemon.lastTickAt.relativeAge ?? "—")
                LabeledContent("Instance", value: state.selectedServer?.instanceID ?? "—")
            }
        }
        .navigationTitle("Fleet")
        .toolbar { ServerToolbarMenu() }
        .sheet(item: $editingRuntime) { RuntimeEditor(runtime: $0, creating: false) }
        .sheet(isPresented: $creatingRuntime) {
            RuntimeEditor(runtime: .init(id: "", name: "", kind: "exec", argv: [],
                                         enabled: true, supportsSteering: nil,
                                         supportsInterrupt: nil), creating: true)
        }
        .refreshable { await state.refresh() }
    }

    private var metrics: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 120))], spacing: 8) {
            MetricCard(value: state.snapshot?.scheduler.active.formatted() ?? "—", label: "active")
            MetricCard(value: state.snapshot?.scheduler.queued.formatted() ?? "—", label: "queued")
            MetricCard(value: state.snapshot?.scheduler.maxActive.formatted() ?? "—", label: "capacity")
            MetricCard(value: state.runtimes.filter(\.enabled).count.formatted(), label: "runtimes")
        }.padding(.vertical, 8)
    }

    private func setPaused(_ paused: Bool) {
        Task {
            do {
                _ = try await state.api().scheduler(paused: paused)
                await state.succeeded(paused ? "New starts paused" : "Scheduler resumed")
            } catch { state.report(error) }
        }
    }
}

private struct FleetSettingsView: View {
    @EnvironmentObject private var state: AppState
    @State private var instanceName = "Orchestra"
    @State private var maxActive = 8
    @State private var maxDepth = 2
    @State private var maxChildren = 3
    @State private var maxActiveChildren = 3
    @State private var seeded = false
    @State private var saving = false

    var body: some View {
        Form {
            Section("Identity") {
                TextField("Instance name", text: $instanceName)
                    .onChange(of: instanceName) { _, value in
                        if value.count > 100 { instanceName = String(value.prefix(100)) }
                    }
            }
            Section("Admission") {
                Stepper("Maximum active runs: \(maxActive)", value: $maxActive, in: 1...256)
            }
            Section {
                Stepper("Maximum depth: \(maxDepth)", value: $maxDepth, in: 0...10)
                Stepper("Children per run: \(maxChildren)", value: $maxChildren, in: 1...100)
                Stepper("Active children per run: \(maxActiveChildren)",
                        value: $maxActiveChildren, in: 1...100)
            } header: {
                Text("Delegation")
            } footer: {
                Text("These bounds apply to new child admissions. They do not route work or change profile tiers.")
            }
        }
        .navigationTitle("Fleet settings")
        .toolbar {
            ToolbarItem(placement: .confirmationAction) {
                Button("Save") { save() }
                    .disabled(saving || instanceName.trimmed.isEmpty || instanceName.count > 100)
            }
        }
        .disabled(saving)
        .task { seed() }
    }

    private func seed() {
        guard !seeded else { return }
        instanceName = string("instance_name", fallback: state.snapshot?.instance.name ?? "Orchestra")
        maxActive = integer("max_active_runs", fallback: state.snapshot?.scheduler.maxActive ?? 8)
        maxDepth = integer("delegation_max_depth", fallback: 2)
        maxChildren = integer("delegation_max_children", fallback: 3)
        maxActiveChildren = integer("delegation_max_active_children", fallback: 3)
        seeded = true
    }

    private func setting(_ key: String) -> FleetSetting? {
        state.settings.first { $0.key == key }
    }

    private func integer(_ key: String, fallback: Int) -> Int {
        guard case let .number(value) = setting(key)?.value else { return fallback }
        return Int(value)
    }

    private func string(_ key: String, fallback: String) -> String {
        guard case let .string(value) = setting(key)?.value else { return fallback }
        return value
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            do {
                let desired: [(String, JSONValue)] = [
                    ("instance_name", .string(instanceName.trimmed)),
                    ("max_active_runs", .number(Double(maxActive))),
                    ("delegation_max_depth", .number(Double(maxDepth))),
                    ("delegation_max_children", .number(Double(maxChildren))),
                    ("delegation_max_active_children", .number(Double(maxActiveChildren))),
                ]
                let client = try state.api()
                for (key, value) in desired {
                    guard let current = setting(key), current.value != value else { continue }
                    _ = try await client.updateSetting(current, value: value)
                }
                await state.succeeded("Fleet settings saved")
            } catch { state.report(error) }
        }
    }
}

private struct RuntimeEditor: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var draft: RuntimeConfig
    @State private var argv: String
    @State private var config = ""
    private let originalArgv: String
    let creating: Bool

    init(runtime: RuntimeConfig, creating: Bool) {
        _draft = State(initialValue: runtime)
        _argv = State(initialValue: runtime.argv.joined(separator: "\n"))
        originalArgv = runtime.argv.joined(separator: "\n")
        self.creating = creating
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Runtime") {
                    TextField("Name", text: $draft.name)
                    Picker("Protocol", selection: $draft.kind) {
                        Text("Command (JSON events)").tag("exec")
                        Text("ACP").tag("acp")
                        if !["exec", "acp"].contains(draft.kind) { Text(draft.kind).tag(draft.kind) }
                    }.disabled(!creating && !["exec", "acp"].contains(draft.kind))
                    Toggle("Enabled", isOn: $draft.enabled)
                }
                if ["exec", "acp"].contains(draft.kind) {
                    Section("Argument vector") {
                        TextEditor(text: $argv).font(.body.monospaced()).frame(minHeight: 150)
                        Text("One argument per line. Orchestra invokes an argv directly; this is not a shell hook.")
                            .font(.caption).foregroundStyle(.secondary)
                        if !creating {
                            Text("Credential-shaped values may be redacted. Leave the vector unchanged to preserve the host configuration.")
                                .font(.caption).foregroundStyle(.orange)
                        }
                    }
                } else {
                    Section {
                        Text("This built-in runtime is launched directly by Orchestra and has no custom argv.")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                Section("Configuration") {
                    TextEditor(text: $config).font(.body.monospaced()).frame(minHeight: 110)
                        .accessibilityLabel("Replacement runtime configuration JSON")
                    Text(creating
                         ? "Optional JSON object. Credential-shaped fields are rejected."
                         : "Blank preserves the private host value; {} clears it. Existing values are never returned or prefilled.")
                        .font(.caption).foregroundStyle(.secondary)
                    if !creating {
                        LabeledContent("Host configuration",
                                       value: draft.configConfigured.map { $0 ? "Configured" : "Not configured" } ?? "Unknown")
                    }
                }
            }
            .navigationTitle(creating ? "New runtime" : draft.name)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { save() }
                        .disabled(draft.name.trimmed.isEmpty ||
                                  (["exec", "acp"].contains(draft.kind) && argv.trimmed.isEmpty))
                }
            }
        }.frame(minWidth: 400, minHeight: 420)
    }

    private func save() {
        let commandRuntime = ["exec", "acp"].contains(draft.kind)
        if commandRuntime {
            draft.argv = argv.split(whereSeparator: \.isNewline).map(String.init)
        }
        Task {
            do {
                let configValue = try replacementObject(config, label: "Configuration")
                if creating {
                    _ = try await state.api().createRuntime(draft, config: configValue)
                }
                else {
                    _ = try await state.api().updateRuntime(
                        draft, updateArgv: commandRuntime && argv != originalArgv,
                        config: configValue)
                }
                await state.succeeded("Runtime saved")
                dismiss()
            } catch { state.report(error) }
        }
    }
}
