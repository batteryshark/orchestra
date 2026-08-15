import SwiftUI

/// One run, in the seven views the web dashboard divides it into.
///
/// The header does not scroll: which run this is, and whether it is still
/// alive, are the two things you never want to have to scroll back up for.
struct RunDetailView: View {
    @EnvironmentObject private var state: AppState
    let run: Run

    /// `-runTab facts` opens straight onto a pane, the same way `-startTab`
    /// opens a tab: seven panes are seven screenshots, and none of them
    /// should need a granted device to reach.
    @State private var tab: RunTab = {
        let arguments = ProcessInfo.processInfo.arguments
        guard let at = arguments.firstIndex(of: "-runTab"),
              let name = arguments[safe: at + 1] else { return .trace }
        return RunTab(rawValue: name) ?? .trace
    }()
    /// Trace events live here, not in the pane, so leaving the trace tab
    /// closes the stream without throwing away what it already delivered.
    @State private var events: [TraceEvent] = []
    @State private var confirmingStop = false
    @State private var actionError: String?
    @State private var verdict: String?
    @State private var acting = false

    /// The snapshot refreshes every four seconds; the run passed in is a
    /// copy from whenever the row was tapped.
    private var live: Run {
        state.snapshot?.runs.first { $0.id == run.id } ?? run
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            tabStrip
            Divider()
            pane
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(.systemGroupedBackground))
        }
        .navigationTitle("#\(run.id)")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if live.live {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button("Check", systemImage: "eye") { check() }
                        .disabled(acting)
                    Button("Stop", systemImage: "stop.circle", role: .destructive) {
                        confirmingStop = true
                    }
                    .disabled(acting)
                }
            }
        }
        .confirmationDialog(
            "Stop run #\(run.id)?",
            isPresented: $confirmingStop,
            titleVisibility: .visible
        ) {
            Button("Stop run", role: .destructive) {
                act { try await $0.stop(runID: run.id) }
            }
        } message: {
            Text("The run is killed where it stands. Its branch and worktree stay.")
        }
        .alert("That did not go through", isPresented: .init(
            get: { actionError != nil },
            set: { if !$0 { actionError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(actionError ?? "")
        }
        .alert("Observer", isPresented: .init(
            get: { verdict != nil },
            set: { if !$0 { verdict = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(verdict ?? "")
        }
    }

    private func act(_ body: @escaping (DromondAPI) async throws -> Void) {
        Task {
            acting = true
            defer { acting = false }
            actionError = await state.perform(body)
        }
    }

    /// A check spends a real observer turn, and its answer — "working, log
    /// written 12s ago" — is the reason for asking. Throwing it away and only
    /// refreshing would make the button look like it did nothing.
    private func check() {
        Task {
            acting = true
            defer { acting = false }
            var answer: String?
            actionError = await state.perform { answer = try await $0.check(runID: run.id) }
            if actionError == nil {
                verdict = answer ?? "The observer had nothing to add."
            }
        }
    }

    // --- header and strip -------------------------------------------------

    private var header: some View {
        let r = live
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                if r.live {
                    Circle().fill(.green).frame(width: 7, height: 7)
                }
                Text("#\(r.id)").font(.subheadline.monospaced().weight(.semibold))
                if let slug = r.slug, !slug.isEmpty {
                    Text(slug).font(.caption.monospaced()).foregroundStyle(.secondary)
                }
                Spacer(minLength: 6)
                StatusChip(status: r.status)
            }
            Text(r.displayTitle).font(.headline).lineLimit(2)
            HStack(spacing: 6) {
                Text(harness(r)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer(minLength: 4)
                if let elapsed = r.elapsedSeconds {
                    Label(elapsed.durationLabel, systemImage: "clock")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal)
        .padding(.top, 4)
        .padding(.bottom, 8)
    }

    private func harness(_ r: Run) -> String {
        var parts = [r.profile].filter { !$0.isEmpty }
        if let model = r.model, !model.isEmpty { parts.append(model) }
        else if !r.backend.isEmpty { parts.append(r.backend) }
        if let item = r.workItem, !item.isEmpty { parts.append(item) }
        return parts.joined(separator: " · ")
    }

    /// Seven panes is more than a segmented control can hold on a phone, so
    /// the strip scrolls — the same shape the web dashboard uses.
    private var tabStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(RunTab.allCases) { item in
                    Button {
                        tab = item
                    } label: {
                        Text(item.rawValue)
                            .font(.footnote.weight(.semibold))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .background(
                                tab == item ? Color.accentColor : Color(.secondarySystemFill),
                                in: Capsule()
                            )
                            .foregroundStyle(tab == item ? Color.white : Color.primary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal)
            .padding(.bottom, 8)
        }
    }

    @ViewBuilder
    private var pane: some View {
        switch tab {
        case .trace:
            TracePane(run: live, events: $events)
        case .thread:
            ThreadPane(run: live)
        case .brief:
            BriefPane(runID: run.id)
        case .facts:
            FactsPane(run: live)
        case .summary:
            ScrollView {
                if live.summary.isEmpty {
                    EmptyPane(
                        text: live.live
                            ? "No summary yet. A run writes one when it finishes."
                            : "This run finished without writing a summary."
                    )
                } else {
                    Card { WrappedText(text: live.summary) }
                }
            }
        case .merge:
            MergePane(run: live)
        case .findings:
            FindingsPane(runID: run.id)
        }
    }
}

enum RunTab: String, CaseIterable, Identifiable {
    case trace, thread, brief, facts, summary, merge, findings
    var id: String { rawValue }
}

// --- trace -------------------------------------------------------------------

/// The live stream. Terminal runs replay what was recorded and then end, so
/// this same pane is the history view too.
private struct TracePane: View {
    @EnvironmentObject private var state: AppState
    let run: Run
    @Binding var events: [TraceEvent]
    @State private var error: String?
    @State private var closed = false
    @State private var follow = true

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if events.isEmpty && error == nil {
                        if closed {
                            EmptyPane(text: "The stream closed without sending anything for this run.")
                        } else {
                            ProgressView("Reading the trace…")
                                .frame(maxWidth: .infinity)
                                .padding(.top, 40)
                        }
                    }
                    ForEach(events) { TraceRow(event: $0).id($0.id) }
                    if let error {
                        Label(error, systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(.orange)
                            .padding(.horizontal)
                    }
                    Color.clear.frame(height: 1).id(Self.tailID)
                }
                .padding(.vertical, 12)
            }
            // Only a live run follows its own tail. Replaying a finished run
            // means reading it, and 237 events flying past to land on the
            // last one is the opposite of what you opened it for.
            .onChange(of: events.count) { _, _ in
                guard follow, run.live else { return }
                proxy.scrollTo(Self.tailID, anchor: .bottom)
            }
            .safeAreaInset(edge: .bottom) {
                if run.live {
                    Toggle("Follow the tail", isOn: $follow)
                        .font(.caption)
                        .padding(.horizontal)
                        .padding(.vertical, 8)
                        .background(.bar)
                }
            }
        }
        .task(id: run.id) { await stream() }
    }

    private static let tailID = "trace-tail"

    private func stream() async {
        while !Task.isCancelled {
            do {
                let api = try state.api()
                for try await event in api.trace(runID: run.id, afterID: events.last?.id) {
                    events.append(event)
                    error = nil
                }
                closed = true
                return // A clean close is the server's `end` on a terminal run.
            } catch is CancellationError {
                return
            } catch {
                self.error = error.localizedDescription + " Retrying…"
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            }
        }
    }
}

/// One trace event.
///
/// The proportions decide the design: run 30 is 237 events, of which 110 are
/// lifecycle and 75 are tool results, and only 38 are the model thinking.
/// Assistant text is what a person opened this for and is legible with no tap.
/// Everything else — thinking, tools, lifecycle — starts closed and opens in
/// steps, because a single reasoning block runs to thousands of characters and
/// four of them in a row is the same wall of text the tool payloads were.
private struct TraceRow: View {
    let event: TraceEvent
    @State private var reveal: Reveal = .collapsed

    /// Tool payloads open in two steps, not one. A single tap that dumps
    /// 22,000 characters into the column buries the next forty events.
    enum Reveal { case collapsed, preview, full }

    /// The preview is bounded by characters as well as lines: ten lines of a
    /// minified blob is still the whole blob.
    private static let previewLimit = 400

    var body: some View {
        Card(tint: tint) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: icon).foregroundStyle(tint).font(.caption)
                    Text(label).font(.caption.weight(.bold)).foregroundStyle(tint)
                    if let name = event.name, !name.isEmpty, name != label {
                        Text(name).font(.caption.monospaced()).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 4)
                    if let signal {
                        Text(signal).font(.caption2).foregroundStyle(.secondary)
                    }
                }
                if let text, !text.isEmpty {
                    Text(text)
                        .font(mono ? .caption.monospaced() : .callout)
                        .foregroundStyle(event.kind == "reasoning" ? Color.secondary : Color.primary)
                        .textSelection(.enabled)
                        .lineLimit(lineLimit)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let next {
                    Button(next.label) {
                        withAnimation { reveal = next.reveal }
                    }
                    .font(.caption)
                }
            }
        }
    }

    private var isTool: Bool { event.kind.hasPrefix("tool_") }

    /// Opens in three steps: header, bounded preview, whole payload.
    private var isStaged: Bool { isTool || event.kind == "reasoning" }

    /// What the row says with nothing opened: how much there is, and whether
    /// the daemon already cut it.
    private var signal: String? {
        var parts: [String] = []
        if isStaged { parts.append("\(event.payloadLength.grouped) characters") }
        if event.truncated { parts.append("truncated") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Lifecycle payloads are the harness's own JSON, and the event's name
    /// already says what happened. The one field worth reading is lifted out;
    /// the blob itself appears only when it is asked for.
    private var text: String? {
        switch event.kind {
        case "lifecycle":
            if let reason { return reason }
            return reveal == .collapsed ? nil : event.payload
        case "tool_call", "tool_result", "reasoning":
            let payload = plain(event.payload)
            switch reveal {
            case .collapsed: return nil
            case .preview:
                guard payload.count > Self.previewLimit else { return payload }
                return String(payload.prefix(Self.previewLimit)) + "…"
            case .full: return payload
            }
        default:
            return plain(event.payload)
        }
    }

    /// OpenCode writes a step's stop reason inside `part`, not at the top, so
    /// looking only at the root left every `step_finish` as an unopened blob.
    private var reason: String? {
        jsonString(event.payload, key: "reason")
            ?? jsonString(event.payload, key: "reason", within: "part")
    }

    /// The one control the row offers, and where it goes. Naming the next
    /// state is what tells the reader another step exists.
    private var next: (label: String, reveal: Reveal)? {
        if event.kind == "lifecycle" {
            guard event.payload.count > 2, reason == nil else { return nil }
            return reveal == .collapsed
                ? ("Show the raw event", .full)
                : ("Collapse", .collapsed)
        }
        guard isStaged else { return nil }
        let whole = event.kind == "reasoning"
            ? "Show the whole thought"
            : "Show all \(event.payloadLength.grouped) characters"
        switch reveal {
        case .collapsed:
            // A payload that fits the preview has nothing to preview towards,
            // so it opens whole rather than promising a step that does nothing.
            let opener = event.kind == "reasoning" ? "Show what it was thinking" : "Show a preview"
            return plain(event.payload).count > Self.previewLimit
                ? (opener, .preview)
                : (whole, .full)
        case .preview: return (whole, .full)
        case .full: return ("Collapse", .collapsed)
        }
    }

    /// Assistant text is prose and is never clipped. A preview is clipped by
    /// lines as well as characters, so a wall of short lines cannot beat the
    /// character cap.
    private var lineLimit: Int? {
        guard isStaged else { return nil }
        return reveal == .preview ? 10 : nil
    }

    private var mono: Bool {
        ["tool_call", "tool_result", "lifecycle"].contains(event.kind)
    }

    private var label: String {
        switch event.kind {
        case "assistant_text": "assistant"
        case "reasoning": "thinking"
        case "tool_call": "tool call"
        case "tool_result": "tool result"
        case "permission_request": "permission"
        case "human_injection": "told"
        case "lifecycle": "lifecycle"
        default: event.kind
        }
    }

    private var icon: String {
        switch event.kind {
        case "assistant_text": "text.bubble"
        case "reasoning": "brain"
        case "tool_call": "wrench.and.screwdriver"
        case "tool_result": "doc.text"
        case "permission_request": "lock"
        case "human_injection": "tray.and.arrow.down"
        case "lifecycle": "gearshape"
        default: "circle"
        }
    }

    private var tint: Color {
        switch event.kind {
        case "assistant_text": .primary
        case "reasoning": .indigo
        case "permission_request": .orange
        case "human_injection": .blue
        case "tool_call", "tool_result": .purple
        default: .secondary
        }
    }
}

/// Terminal output arrives with the colour codes still in it, which render as
/// literal escape gibberish in a Text.
private func plain(_ text: String) -> String {
    guard text.contains("\u{1B}") else { return text }
    return text.replacingOccurrences(
        of: "\u{1B}\\[[0-9;?]*[a-zA-Z]",
        with: "",
        options: .regularExpression
    )
}

private func jsonString(_ text: String, key: String, within nested: String? = nil) -> String? {
    guard let data = text.data(using: .utf8),
          var object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return nil }
    if let nested {
        guard let inner = object[nested] as? [String: Any] else { return nil }
        object = inner
    }
    guard let value = object[key] as? String, !value.isEmpty else { return nil }
    return value
}

// --- thread ------------------------------------------------------------------

/// Inbox and outbox in one column, plus the way to add to it. The merge
/// report is a message too, but it has its own tab, so it is left out here.
private struct ThreadPane: View {
    @EnvironmentObject private var state: AppState
    let run: Run
    @State private var draft = ""
    @State private var sending = false
    @State private var error: String?
    @FocusState private var composing: Bool

    private var thread: [RunMessage] { run.messages.filter { $0.kind != "merge" } }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                if thread.isEmpty {
                    EmptyPane(text: "No messages. Nothing was told to this run, and it asked nothing.")
                }
                ForEach(thread) { MessageCard(message: $0) }
                if let error {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.orange).padding(.horizontal)
                }
            }
            .padding(.vertical, 12)
        }
        .safeAreaInset(edge: .bottom) {
            if run.live {
                HStack(spacing: 8) {
                    TextField("Tell the run something", text: $draft, axis: .vertical)
                        .lineLimit(1...4)
                        .textFieldStyle(.roundedBorder)
                        .focused($composing)
                        .disabled(sending)
                    Button {
                        Task { await send() }
                    } label: {
                        if sending { ProgressView() }
                        else { Image(systemName: "paperplane.fill") }
                    }
                    .disabled(sending || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                .padding(.horizontal)
                .padding(.vertical, 8)
                .background(.bar)
            }
        }
        .scrollDismissesKeyboard(.interactively)
    }

    private func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sending = true
        defer { sending = false }
        error = await state.perform { try await $0.tell(runID: run.id, text: text) }
        if error == nil {
            draft = ""
            composing = false
        }
    }
}

private struct MessageCard: View {
    let message: RunMessage

    private var inbound: Bool { message.direction == "inbound" }

    var body: some View {
        Card(tint: inbound ? .blue : .secondary) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: inbound ? "arrow.down.left" : "arrow.up.right")
                        .font(.caption2)
                        .foregroundStyle(inbound ? Color.blue : Color.secondary)
                    Text(message.kind ?? "message")
                        .font(.caption.weight(.bold))
                    if let sender = message.sender, !sender.isEmpty {
                        Text(sender).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 4)
                    if let state = message.state, !state.isEmpty {
                        Text(state)
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 7).padding(.vertical, 3)
                            .background(stateColor.opacity(0.15), in: Capsule())
                            .foregroundStyle(stateColor)
                    }
                }
                if message.pendingBoundary == true {
                    Label("Waiting for a safe action boundary", systemImage: "hourglass")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                if let reason = message.undeliverableReason, !reason.isEmpty {
                    Label(reason, systemImage: "exclamationmark.triangle")
                        .font(.caption).foregroundStyle(.red)
                }
                if !message.body.isEmpty {
                    WrappedText(text: message.body, font: .callout)
                }
                if let at = message.createdAt, !at.isEmpty {
                    Text(at).font(.caption2.monospaced()).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var stateColor: Color {
        switch message.state {
        case "delivered", "answered": .green
        case "undeliverable", "failed": .red
        case "pending", "queued": .orange
        default: .secondary
        }
    }
}

// --- brief -------------------------------------------------------------------

/// The brief is a file on disk, not part of the snapshot, so it is read once
/// when this tab opens.
private struct BriefPane: View {
    @EnvironmentObject private var state: AppState
    let runID: Int
    @State private var brief: BriefText?
    @State private var error: String?
    @State private var loading = true

    var body: some View {
        ScrollView {
            if loading {
                ProgressView("Reading the brief file…")
                    .frame(maxWidth: .infinity).padding(.top, 40)
            } else if let text = brief?.text, !text.isEmpty {
                Card { WrappedText(text: text, font: .caption.monospaced()) }
                if let path = brief?.path, !path.isEmpty {
                    WrappedText(text: path, font: .caption2.monospaced(), color: .secondary)
                        .padding(.horizontal, 28)
                }
            } else {
                EmptyPane(text: error ?? "No brief file for this run.")
                if let path = brief?.path, !path.isEmpty {
                    WrappedText(text: path, font: .caption2.monospaced(), color: .secondary)
                        .padding(.horizontal, 28)
                }
            }
        }
        .task(id: runID) {
            loading = true
            defer { loading = false }
            do { brief = try await state.api().brief(runID: runID) }
            catch { self.error = error.localizedDescription }
        }
    }
}

// --- facts -------------------------------------------------------------------

/// Everything the run itself records. A missing number says so; nothing here
/// is allowed to read as zero when the truth is that nobody counted.
private struct FactsPane: View {
    let run: Run

    var body: some View {
        ScrollView {
            Card {
                VStack(spacing: 0) {
                    Fact("profile", run.profile)
                    Fact("harness", [run.backend, run.model ?? ""]
                        .filter { !$0.isEmpty }.joined(separator: " · "))
                    Fact("project", run.project ?? run.projectID)
                    Fact("work item", run.workItem)
                    Fact("requested by", run.requestedBy)
                    Fact("branch", run.branch, copyable: true)
                    Fact("workdir", run.workdir, copyable: true)
                    // The range the run is judged on: where it started, and
                    // where its work last landed.
                    Fact("base commit", run.baseCommit.map { String($0.prefix(12)) },
                         copyable: true)
                    Fact("checkpoint", run.checkpointCommit.map { String($0.prefix(12)) },
                         absent: "nothing committed", copyable: true)
                    Fact("session", run.sessionRef, copyable: true)
                    Fact("brief", run.briefPath, copyable: true)
                    Fact("lineage", lineage, absent: "no parent, no retry")
                    Fact("blocked on", run.blockedOn.isEmpty
                        ? nil : run.blockedOn.map { "#\($0)" }.joined(separator: ", "),
                        absent: "nothing")
                    Fact("started", run.startedAt)
                    Fact("finished", run.finishedAt)
                    Fact("elapsed", run.elapsedSeconds?.durationLabel)
                    Fact("exit code", run.exitCode.map { String($0) })
                    Fact("tokens", tokens)
                    Fact("cost", cost)
                    Fact("usage from", run.usageSource)
                    Fact("billing", run.billing, last: true)
                }
            }
        }
    }

    private var lineage: String? {
        var parts: [String] = []
        if let parent = run.parentRun { parts.append("spawned by #\(parent)") }
        if let retry = run.retryOf { parts.append("retry of #\(retry)") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// "not captured" and "0" are different facts. Reasonix reports usage,
    /// a killed run never does, and a dash would hide which happened.
    private var tokens: String? {
        guard run.tokensTotal != nil || run.tokensIn != nil else { return nil }
        let inOut = [run.tokensIn.map { "in \($0.grouped)" },
                     run.tokensOut.map { "out \($0.grouped)" }].compactMap { $0 }
        let total = run.tokensTotal?.grouped ?? "—"
        return inOut.isEmpty ? total : "\(total) (\(inOut.joined(separator: " / ")))"
    }

    /// A plan-backed run has no price. OpenCode reports 0 on a subscription,
    /// and that zero is not free work.
    private var cost: String? {
        if run.billing == "plan" { return "on plan" }
        guard let cost = run.costUSD else { return nil }
        return String(format: "$%.4f", cost)
    }
}

private struct Fact: View {
    let label: String
    let value: String?
    /// What an empty row means. "not captured" is the default because most
    /// gaps here are a number nobody recorded — but a run with no parent is
    /// not an unrecorded parent, and saying so would be a lie.
    var absent = "not captured"
    var copyable = false
    var last = false

    init(
        _ label: String,
        _ value: String?,
        absent: String = "not captured",
        copyable: Bool = false,
        last: Bool = false
    ) {
        self.label = label
        self.value = (value?.isEmpty == true) ? nil : value
        self.absent = absent
        self.copyable = copyable
        self.last = last
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Text(label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(width: 96, alignment: .leading)
                if let value {
                    if copyable {
                        WrappedText(text: value, font: .caption.monospaced())
                    } else {
                        Text(value)
                            .font(.callout)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else {
                    Text(absent)
                        .font(.callout)
                        .foregroundStyle(.tertiary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(.vertical, 7)
            if !last { Divider() }
        }
    }
}

// --- merge -------------------------------------------------------------------

/// The merge report is written into the run's own thread as a message of kind
/// `merge`, so there is no second source to read. The committed diff is a
/// separate read, and only on request: it can be megabytes.
private struct MergePane: View {
    @EnvironmentObject private var state: AppState
    let run: Run
    @State private var diff: DiffText?
    @State private var loadingDiff = false
    @State private var diffError: String?

    private var reports: [RunMessage] { run.messages.filter { $0.kind == "merge" } }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                if reports.isEmpty {
                    EmptyPane(text: run.branch == nil
                        ? "No branch. This run worked in the shared tree, so nothing merges."
                        : "Nothing has landed yet.")
                }
                ForEach(reports) { MessageCard(message: $0) }

                if run.branch != nil {
                    if let diff {
                        if let text = diff.text, !text.isEmpty {
                            if let base = diff.base, let head = diff.head {
                                WrappedText(
                                    text: "\(base.prefix(7)) → \(head.prefix(7))",
                                    font: .caption2.monospaced(), color: .secondary
                                )
                                .padding(.horizontal, 28)
                            }
                            Card { DiffBody(text: text) }
                            if diff.truncated == true {
                                Label("The daemon capped this diff; there is more.",
                                      systemImage: "scissors")
                                    .font(.caption).foregroundStyle(.orange).padding(.horizontal)
                            }
                        } else {
                            EmptyPane(text: diff.message ?? "No committed changes.")
                        }
                    } else if loadingDiff {
                        ProgressView("Reading committed changes…")
                            .frame(maxWidth: .infinity).padding()
                    } else {
                        Button("Show committed changes") { Task { await load() } }
                            .buttonStyle(.bordered)
                            .padding(.horizontal)
                    }
                    if let diffError {
                        Label(diffError, systemImage: "exclamationmark.triangle")
                            .font(.caption).foregroundStyle(.orange).padding(.horizontal)
                    }
                }
            }
            .padding(.vertical, 12)
        }
    }

    private func load() async {
        loadingDiff = true
        defer { loadingDiff = false }
        do { diff = try await state.api().diff(runID: run.id) }
        catch { diffError = error.localizedDescription }
    }
}

/// A diff is only readable if the additions and deletions are told apart.
private struct DiffBody: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(text.split(separator: "\n", omittingEmptySubsequences: false)
                .prefix(600).enumerated()), id: \.offset) { _, line in
                Text(String(line))
                    .font(.caption2.monospaced())
                    .foregroundStyle(colour(String(line)))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .textSelection(.enabled)
    }

    private func colour(_ line: String) -> Color {
        if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@")
            || line.hasPrefix("diff ") { return .secondary }
        if line.hasPrefix("+") { return .green }
        if line.hasPrefix("-") { return .red }
        return .primary
    }
}

// --- findings ----------------------------------------------------------------

/// Findings and proposals are both things a run filed for a human to read,
/// so they sit together the way the web dashboard puts them.
private struct FindingsPane: View {
    @EnvironmentObject private var state: AppState
    let runID: Int

    private var findings: [Finding] {
        (state.snapshot?.findings ?? []).filter { $0.run == runID }
    }

    private var proposals: [Proposal] {
        (state.snapshot?.proposals ?? []).filter { $0.run == runID }
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 10) {
                SectionLabel("findings")
                if findings.isEmpty {
                    EmptyPane(text: "None filed by this run.")
                }
                ForEach(findings) { finding in
                    Card(tint: .orange) {
                        VStack(spacing: 0) {
                            Fact("claim", finding.claim)
                            Fact("where", finding.with, copyable: true)
                            Fact("confidence", finding.confidence)
                            Fact("why not fixed", finding.whyNotFixed)
                            Fact("filed as", finding.filedAs)
                            Fact("at", finding.at, last: true)
                        }
                    }
                }
                SectionLabel("proposals")
                if proposals.isEmpty {
                    EmptyPane(text: "None filed by this run.")
                }
                ForEach(proposals) { proposal in
                    Card(tint: .teal) {
                        VStack(spacing: 0) {
                            Fact("title", proposal.title)
                            Fact("why", proposal.why)
                            Fact("verdict", proposal.verdict)
                            Fact("action", proposal.action)
                            Fact("at", proposal.at, last: true)
                        }
                    }
                }
            }
            .padding(.vertical, 12)
        }
    }
}

// --- shared bits -------------------------------------------------------------

/// The card every pane draws on: the same radius and fill MetricCard uses,
/// so the two tabs look like one app.
private struct Card<Content: View>: View {
    var tint: Color?
    @ViewBuilder var content: Content

    var body: some View {
        content
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(12)
            .background(
                Color(.secondarySystemGroupedBackground),
                in: RoundedRectangle(cornerRadius: 16)
            )
            .overlay(alignment: .leading) {
                if let tint {
                    Capsule().fill(tint.opacity(0.6)).frame(width: 3)
                        .padding(.vertical, 10)
                }
            }
            .padding(.horizontal)
    }
}

private struct EmptyPane: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.callout)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 32)
            .padding(.vertical, 28)
    }
}

private struct SectionLabel: View {
    let text: String

    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text.uppercased())
            .font(.caption2.weight(.bold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 28)
            .padding(.top, 4)
    }
}
