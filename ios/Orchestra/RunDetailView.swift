import SwiftUI
import AVKit
import PDFKit
#if os(macOS)
import AppKit
#else
import UIKit
#endif

enum RunTab: String, CaseIterable, Identifiable {
    case thread, activity, overview, artifacts, changes, log, lineage, facts, observer
    var id: String { rawValue }
    var title: String {
        switch self {
        case .log: "Raw Log"
        case .facts: "Facts / Usage"
        default: rawValue.capitalized
        }
    }
    var icon: String {
        switch self {
        case .overview: "doc.text"
        case .activity: "waveform.path.ecg"
        case .thread: "bubble.left.and.bubble.right"
        case .artifacts: "paperclip"
        case .changes: "arrow.triangle.branch"
        case .log: "terminal"
        case .lineage: "point.3.connected.trianglepath.dotted"
        case .facts: "list.bullet.rectangle"
        case .observer: "eye"
        }
    }
}

struct RunDetailView: View {
    @EnvironmentObject private var state: AppState
    @State private var current: Run
    @State private var tab: RunTab
    @State private var messages: [RunMessage] = []
    @State private var messageOlderCursor: String?
    @State private var messageResumeCursor: String?
    @State private var events: [RunEvent] = []
    @State private var eventOlderCursor: String?
    @State private var eventResumeCursor: String?
    @State private var artifacts: [Artifact] = []
    @State private var changes: RunChanges?
    @State private var lineage: RunLineage?
    @State private var observer: ObserverRunDetail?
    @State private var log = ""
    @State private var logPartial = false
    @State private var logByteCount = 0
    @State private var fullLogURL: URL?
    @State private var preparingFullLog = false
    @State private var loading = true
    @State private var paneErrors: [RunTab: String] = [:]
    @State private var prompt: ControlPrompt?
    @State private var stopping = false
    @State private var selectedArtifact: Artifact?
    @State private var loadingOlderThread = false
    @State private var loadingOlderEvents = false
    @State private var showSystemMessages = false
    @State private var showLifecycleEvents = false
    @State private var followThreadTail = true
    @State private var followLogTail = true

    init(run: Run) {
        _current = State(initialValue: run)
        _tab = State(initialValue: .thread)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack {
                Label(tab.title, systemImage: tab.icon).font(.headline)
                Spacer()
                Picker("Pane", selection: $tab) {
                    ForEach(RunTab.allCases) { Label($0.title, systemImage: $0.icon).tag($0) }
                }
                .pickerStyle(.menu)
            }
            .padding(.horizontal).padding(.vertical, 8)
            Divider()
            pane
        }
        .navigationTitle(current.display ?? "Run \(current.id)")
        .toolbar {
            ServerToolbarMenu()
            ToolbarItem(placement: .automatic) { controls }
        }
        .sheet(item: $prompt) { prompt in ControlPromptView(prompt: prompt) { text in
            control(prompt.action, text: text)
        } }
        .sheet(item: $selectedArtifact) { ArtifactPreview(artifact: $0) }
        .task(id: current.id) {
            await loadEverything()
            await monitor()
        }
        .task(id: current.id) { await refreshLiveTail() }
        .refreshable { await loadEverything() }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(current.display ?? "\(state.groupName(current.groupID) ?? "General") #\(current.groupNumber)")
                    .font(.title2.bold()).monospacedDigit()
                StatusChip(status: current.status)
                Spacer()
                if loading { ProgressView().controlSize(.small) }
            }
            Text(current.title ?? current.context ?? "Run \(current.id)")
                .foregroundStyle(.secondary).lineLimit(2)
            HStack(spacing: 10) {
                Label(state.profileName(current.profileID) ?? current.profileID, systemImage: "person.crop.rectangle")
                if let source = current.cwdSource {
                    Label(source.replacingOccurrences(of: "_", with: " "),
                          systemImage: "folder")
                }
                if let hold = current.hold {
                    Label(hold.detail ?? hold.kind, systemImage: "pause.circle")
                }
                if let waiting = current.waitingKind {
                    Label(waiting, systemImage: "hourglass")
                }
            }
            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding()
    }

    @ViewBuilder private var pane: some View {
        if let error = paneErrors[tab] {
            ContentUnavailableView("Could not load \(tab.title)",
                                   systemImage: "exclamationmark.triangle",
                                   description: Text(error))
        } else {
            switch tab {
            case .overview: overviewPane
            case .activity: activityPane
            case .thread: threadPane
            case .artifacts: artifactsPane
            case .changes: changesPane
            case .log: logPane
            case .lineage: lineagePane
            case .facts: factsPane
            case .observer: observerPane
            }
        }
    }

    private var overviewPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                GroupBox("Outcome") {
                    Text(current.resultText.isEmpty ? "No terminal result yet." : current.resultText)
                        .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                }
                if let request = current.context {
                    GroupBox("Context") {
                        Text(request).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                UsageView(usage: current.usage, title: "Worker usage")
                UsageView(usage: current.combinedUsage, title: "Combined usage")
            }.padding()
        }
    }

    private var hiddenSystemMessageCount: Int {
        messages.filter { $0.direction == "system" && $0.status != "undeliverable" }.count
    }

    private var visibleMessages: [RunMessage] {
        guard !showSystemMessages else { return messages }
        return messages.filter { $0.direction != "system" || $0.status == "undeliverable" }
    }

    private var hiddenLifecycleCount: Int {
        events.filter { $0.kind == "lifecycle" }.count
    }

    private var visibleEvents: [RunEvent] {
        guard !showLifecycleEvents else { return events }
        return events.filter { $0.kind != "lifecycle" }
    }

    private var activityPane: some View {
        Group {
            if events.isEmpty {
                EmptyState(icon: "waveform.path.ecg", title: "No activity yet",
                           message: "Normalized runtime events will appear here live.")
            } else {
                List {
                    if eventOlderCursor != nil {
                        Button { loadOlderEvents() } label: {
                            if loadingOlderEvents { ProgressView() }
                            else { Label("Load older activity", systemImage: "clock.arrow.circlepath") }
                        }
                        .disabled(loadingOlderEvents)
                    }
                    if hiddenLifecycleCount > 0 {
                        Button {
                            showLifecycleEvents.toggle()
                        } label: {
                            Label(showLifecycleEvents
                                  ? "Hide lifecycle chatter"
                                  : "Show \(hiddenLifecycleCount) lifecycle events",
                                  systemImage: "gearshape.2")
                        }
                    }
                    ForEach(visibleEvents) { event in RunEventRow(event: event) }
                }.listStyle(.plain)
            }
        }
    }

    private var threadPane: some View {
        VStack(spacing: 0) {
            if messages.isEmpty {
                EmptyState(icon: "bubble.left.and.bubble.right", title: "No messages",
                           message: "Tell, questions, answers, and delivery receipts appear here.")
            } else {
                ScrollViewReader { proxy in
                    List {
                        if messageOlderCursor != nil {
                            Button { loadOlderThread() } label: {
                                if loadingOlderThread { ProgressView() }
                                else { Label("Load older messages", systemImage: "clock.arrow.circlepath") }
                            }
                            .disabled(loadingOlderThread)
                        }
                        if hiddenSystemMessageCount > 0 {
                            Button {
                                showSystemMessages.toggle()
                            } label: {
                                Label(showSystemMessages
                                      ? "Hide system receipts"
                                      : "Show \(hiddenSystemMessageCount) system receipts",
                                      systemImage: "gearshape")
                            }
                        }
                        ForEach(visibleMessages) { message in
                            MessageReceiptRow(message: message, compact: true)
                                .id(message.id)
                        }
                    }
                    .listStyle(.plain)
                    .onChange(of: visibleMessages.last?.id) { _, id in
                        guard followThreadTail, let id else { return }
                        withAnimation { proxy.scrollTo(id, anchor: .bottom) }
                    }
                }
            }
            Divider()
            HStack {
                Toggle(isOn: $followThreadTail) {
                    Label("Follow", systemImage: "arrow.down.to.line")
                }
                .toggleStyle(.button)
                Spacer()
                if current.isLive {
                    Button { prompt = .init(action: "tell", title: "Tell this run", label: "Message") } label: {
                        Label("Tell this run", systemImage: "paperplane")
                    }
                } else {
                    Button { prompt = .init(action: "continue", title: "Continue as a new run", label: "Context") } label: {
                        Label("Continue", systemImage: "arrow.right.circle")
                    }
                }
            }.padding()
        }
    }

    private var artifactsPane: some View {
        Group {
            if artifacts.isEmpty {
                EmptyState(icon: "paperclip", title: "No published artifacts",
                           message: "Only files explicitly published by the run appear here.")
            } else {
                List(artifacts) { artifact in
                    Button { selectedArtifact = artifact } label: {
                        HStack {
                            Image(systemName: artifact.mediaType.systemImage)
                            VStack(alignment: .leading) {
                                Text(artifact.name).foregroundStyle(.primary)
                                Text("\(artifact.mediaType) · \(artifact.byteSize.byteCount) · \(artifact.sha256.prefix(12))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").foregroundStyle(.tertiary)
                        }
                    }.buttonStyle(.plain)
                }.listStyle(.plain)
            }
        }
    }

    private var changesPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let changes {
                    GroupBox("Git evidence") {
                        Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 6) {
                            factRow("Branch", changes.branch)
                            factRow("Base", changes.base)
                            factRow("Head", changes.head)
                            factRow("Checkpoints", changes.checkpoints.count.formatted())
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                    GroupBox(changes.truncated == true ? "Diff (truncated)" : "Diff") {
                        Text(changes.diff ?? changes.patch ?? "No Git changes.")
                            .font(.caption.monospaced()).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else { ProgressView().frame(maxWidth: .infinity) }
            }.padding()
        }
    }

    private var logPane: some View {
        VStack(spacing: 0) {
            HStack {
                Text(logPartial
                     ? "Tail · last \(logByteCount.byteCount) of the retained log"
                     : "Complete retained log · \(logByteCount.byteCount)")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                if preparingFullLog { ProgressView().controlSize(.small) }
                if let fullLogURL {
                    ShareLink(item: fullLogURL) {
                        Label("Open / Share full log", systemImage: "square.and.arrow.up")
                    }
                } else {
                    Button { prepareFullLog() } label: {
                        Label("Prepare full log", systemImage: "arrow.down.doc")
                    }.disabled(preparingFullLog)
                }
                Toggle(isOn: $followLogTail) {
                    Label("Follow", systemImage: "arrow.down.to.line")
                }.toggleStyle(.button)
            }.padding(.horizontal).padding(.vertical, 8)
            Divider()
            ScrollViewReader { proxy in
                ScrollView([.horizontal, .vertical]) {
                    VStack(alignment: .leading, spacing: 0) {
                        Text(log.isEmpty ? "No retained raw log." : log)
                            .font(.caption.monospaced()).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading).padding()
                        Color.clear.frame(height: 1).id("log-tail")
                    }
                }
                .onChange(of: logByteCount) {
                    guard followLogTail else { return }
                    proxy.scrollTo("log-tail", anchor: .bottom)
                }
            }
        }
    }

    private var lineagePane: some View {
        List {
            Section {
                lineageRows(lineage?.items ?? [])
            } header: {
                Text("Run family")
            } footer: {
                if let root = lineage?.rootRunID {
                    Text("Root run \(root). Parent, child, retry, and continuation relationships are retained on each run.")
                }
            }
        }.listStyle(.inset)
    }

    private var factsPane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                GroupBox("Run facts") {
                    Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 7) {
                        factRow("Global ID", current.id.formatted())
                        factRow("Display", current.display ?? "#\(current.groupNumber)")
                        factRow("Status", current.status)
                        factRow("Group", state.groupName(current.groupID) ?? current.groupID)
                        factRow("Profile", state.profileName(current.profileID) ?? current.profileID)
                        factRow("Working directory", current.cwdSource?.replacingOccurrences(
                            of: "_", with: " ").capitalized)
                        factRow("Request", current.requestID)
                        factRow("Queued", current.queuedAt)
                        factRow("Started", current.startedAt)
                        factRow("Finished", current.finishedAt)
                        factRow("Parent", current.parentRunID?.formatted())
                        factRow("Retry of", current.retryOf?.formatted())
                        factRow("Continuation of", current.continuationOf?.formatted())
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                UsageView(usage: current.usage, title: "Worker")
                UsageView(usage: current.observerUsage, title: "Observer")
                UsageView(usage: current.combinedUsage, title: "Combined")
                if let snapshot = current.profileSnapshot {
                    GroupBox("Frozen profile") {
                        Text(snapshot.description).font(.caption.monospaced())
                            .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                    }
                }
                if let snapshot = current.runtimeSnapshot {
                    GroupBox("Frozen runtime") {
                        Text(snapshot.description).font(.caption.monospaced())
                            .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                    }
                }
            }.padding()
        }
    }

    private var observerPane: some View {
        Group {
            if let observer, !observer.checks.isEmpty {
                List {
                    if let usage = observer.usage {
                        Section { UsageView(usage: usage, title: "Observer usage") }
                    }
                    Section("Checks") {
                        ForEach(observer.checks) { check in
                            VStack(alignment: .leading, spacing: 5) {
                                HStack {
                                    StatusChip(status: check.action)
                                    Text(check.judgment ?? "Check").font(.headline)
                                    Spacer()
                                    Text(check.createdAt.relativeAge).font(.caption).foregroundStyle(.secondary)
                                }
                                if let rationale = check.rationale { Text(rationale) }
                                if let from = check.evidenceFrom, let to = check.evidenceTo {
                                    Text("Evidence \(from)…\(to)")
                                        .font(.caption.monospaced()).foregroundStyle(.secondary)
                                }
                            }.padding(.vertical, 4)
                        }
                    }
                }.listStyle(.inset)
            } else {
                EmptyState(icon: "eye", title: "No Observer checks",
                           message: "Observer activity is separate from worker runs and group numbering.")
            }
        }
    }

    private var controls: some View {
        Menu {
            if current.isLive {
                Button { prompt = .init(action: "tell", title: "Tell", label: "Message") } label: {
                    Label("Tell", systemImage: "paperplane")
                }
                if ["starting", "running"].contains(current.status) {
                    Button { prompt = .init(action: "interrupt", title: "Interrupt", label: "New direction") } label: {
                        Label("Interrupt", systemImage: "arrow.uturn.forward")
                    }
                }
                Button { control("check") } label: { Label("Check now", systemImage: "eye") }
                Divider()
                Button(role: .destructive) { stopping = true; control("stop") } label: {
                    Label("Stop", systemImage: "stop.circle")
                }.disabled(stopping)
                Button(role: .destructive) { stopping = true; control("stop-tree") } label: {
                    Label("Stop run and children", systemImage: "xmark.octagon")
                }.disabled(stopping)
            } else {
                Button { prompt = .init(action: "retry", title: "Retry as a new run", label: "Context") } label: {
                    Label("Retry", systemImage: "arrow.clockwise")
                }
                Button { prompt = .init(action: "continue", title: "Continue as a new run", label: "Context") } label: {
                    Label("Continue", systemImage: "arrow.right.circle")
                }
            }
        } label: { Label("Control", systemImage: "slider.horizontal.3") }
    }

    @ViewBuilder private func lineageRows(_ runs: [Run]) -> some View {
        if runs.isEmpty { Text("None").foregroundStyle(.secondary) }
        else {
            ForEach(runs) { run in
                NavigationLink { RunDetailView(run: run) } label: { RunRow(run: run) }
            }
        }
    }

    @ViewBuilder private func factRow(_ label: String, _ value: String?) -> some View {
        if let value, !value.isEmpty {
            GridRow {
                Text(label).foregroundStyle(.secondary)
                Text(value).textSelection(.enabled)
            }
        }
    }

    private func control(_ action: String, text: String? = nil) {
        Task {
            defer { stopping = false }
            do {
                _ = try await state.api().control(runID: current.id, action: action, text: text)
                await state.succeeded("\(action.replacingOccurrences(of: "-", with: " ").capitalized) requested")
                await loadEverything(preservingHistory: true)
            } catch { state.report(error) }
        }
    }

    private func loadEverything(preservingHistory: Bool = false) async {
        loading = true
        defer { loading = false }
        paneErrors = [:]
        do {
            let client = try state.api()
            await withTaskGroup(of: DetailLoad.self) { group in
                group.addTask { await DetailLoad.capture(.run, tab: .overview) { .run(try await client.run(current.id).value) } }
                group.addTask { await DetailLoad.capture(.thread, tab: .thread) { .thread(try await client.thread(current.id).value) } }
                group.addTask { await DetailLoad.capture(.events, tab: .activity) { .events(try await client.events(current.id).value) } }
                group.addTask { await DetailLoad.capture(.artifacts, tab: .artifacts) { .artifacts(try await client.artifacts(current.id).value.items) } }
                group.addTask { await DetailLoad.capture(.changes, tab: .changes) { .changes(try await client.changes(current.id).value) } }
                group.addTask { await DetailLoad.capture(.lineage, tab: .lineage) { .lineage(try await client.lineage(current.id).value) } }
                group.addTask { await DetailLoad.capture(.observer, tab: .observer) { .observer(try await client.observer(current.id).value) } }
                group.addTask { await DetailLoad.capture(.log, tab: .log) { .log(try await client.rawLog(current.id)) } }
                for await value in group { apply(value, preservingHistory: preservingHistory) }
            }
        } catch { state.report(error) }
    }

    private func apply(_ value: DetailLoad, preservingHistory: Bool) {
        switch value {
        case let .run(value): current = value
        case let .thread(value):
            if preservingHistory && !messages.isEmpty {
                messages = mergeMessages(messages, value.items)
            } else {
                messages = value.items
                messageOlderCursor = value.nextCursor
            }
            messageResumeCursor = value.resumeCursor
        case let .events(value):
            if preservingHistory && !events.isEmpty {
                events = mergeEvents(events, value.items)
            } else {
                events = value.items
                eventOlderCursor = value.nextCursor
            }
            eventResumeCursor = value.resumeCursor
        case let .artifacts(value): artifacts = value
        case let .changes(value): changes = value
        case let .lineage(value): lineage = value
        case let .observer(value): observer = value
        case let .log(value):
            log = value.text
            logPartial = value.partial
            logByteCount = value.byteCount
            fullLogURL = nil
        case let .failure(tab, message): paneErrors[tab] = message
        }
    }

    private func monitor() async {
        guard current.isLive else { return }
        do {
            for try await event in try state.api().runEvents(current.id,
                                                              lastEventID: events.last.map { String($0.id) }) {
                if !events.contains(where: { $0.id == event.id }) { events.append(event) }
            }
            await loadEverything(preservingHistory: true)
        } catch is CancellationError {
        } catch {
            paneErrors[.activity] = error.localizedDescription
        }
    }

    private func refreshLiveTail() async {
        while !Task.isCancelled, current.isLive {
            do { try await Task.sleep(for: .seconds(3)) }
            catch { return }
            guard !Task.isCancelled else { return }
            guard let client = try? state.api() else { return }

            if let refreshed = try? await client.run(current.id).value {
                current = refreshed
            }
            let threadPage: APIPage<RunMessage>?
            if let cursor = messageResumeCursor {
                threadPage = try? await client.thread(
                    current.id, cursor: cursor, direction: "newer").value
            } else {
                threadPage = try? await client.thread(current.id).value
            }
            if let threadPage {
                messages = mergeMessages(messages, threadPage.items)
                messageResumeCursor = threadPage.resumeCursor ?? messageResumeCursor
            }
            if tab == .log, let tail = try? await client.rawLog(current.id) {
                apply(.log(tail), preservingHistory: true)
            }
        }
    }

    private func mergeMessages(_ first: [RunMessage], _ second: [RunMessage]) -> [RunMessage] {
        var values = Dictionary(uniqueKeysWithValues: first.map { ($0.id, $0) })
        second.forEach { values[$0.id] = $0 }
        return values.values.sorted { $0.id < $1.id }
    }

    private func mergeEvents(_ first: [RunEvent], _ second: [RunEvent]) -> [RunEvent] {
        var values = Dictionary(uniqueKeysWithValues: first.map { ($0.id, $0) })
        second.forEach { values[$0.id] = $0 }
        return values.values.sorted { $0.id < $1.id }
    }

    private func loadOlderThread() {
        guard let cursor = messageOlderCursor else { return }
        loadingOlderThread = true
        Task {
            defer { loadingOlderThread = false }
            do {
                let page = try await state.api().thread(current.id, cursor: cursor).value
                let known = Set(messages.map(\.id))
                messages = page.items.filter { !known.contains($0.id) } + messages
                messageOlderCursor = page.nextCursor
                messageResumeCursor = messageResumeCursor ?? page.resumeCursor
            } catch { state.report(error) }
        }
    }

    private func loadOlderEvents() {
        guard let cursor = eventOlderCursor else { return }
        loadingOlderEvents = true
        Task {
            defer { loadingOlderEvents = false }
            do {
                let page = try await state.api().events(current.id, cursor: cursor).value
                let known = Set(events.map(\.id))
                events = page.items.filter { !known.contains($0.id) } + events
                eventOlderCursor = page.nextCursor
                eventResumeCursor = eventResumeCursor ?? page.resumeCursor
            } catch { state.report(error) }
        }
    }

    private func prepareFullLog() {
        preparingFullLog = true
        Task {
            defer { preparingFullLog = false }
            do {
                let data = logPartial
                    ? try await state.api().fullRawLog(current.id)
                    : Data(log.utf8)
                let url = FileManager.default.temporaryDirectory
                    .appendingPathComponent("Orchestra-\(current.id)-raw.log")
                try data.write(to: url, options: .atomic)
                fullLogURL = url
            } catch { state.report(error) }
        }
    }
}

private struct RunEventRow: View {
    let event: RunEvent
    @State private var expanded = false

    private var collapsible: Bool {
        ["reasoning", "tool_call", "tool_result"].contains(event.kind)
    }

    private var title: String {
        switch event.kind {
        case "assistant_text": "Assistant"
        case "human_injection": "Operator"
        case "permission_request": "Permission request"
        case "tool_call": event.name.map { "Tool · \($0)" } ?? "Tool call"
        case "tool_result": event.name.map { "Result · \($0)" } ?? "Tool result"
        default: event.kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    private var detail: String? {
        let value = event.text ?? event.payload?.description
        return value?.isEmpty == false ? value : nil
    }

    var body: some View {
        Group {
            if collapsible {
                DisclosureGroup(isExpanded: $expanded) {
                    detailText.padding(.top, 5)
                } label: { header }
            } else {
                VStack(alignment: .leading, spacing: 5) {
                    header
                    detailText
                }
            }
        }
        .padding(.vertical, 3)
    }

    private var header: some View {
        HStack {
            Label(title, systemImage: icon).font(.subheadline.bold())
            Spacer()
            Text(event.createdAt.relativeAge).font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private var detailText: some View {
        if let detail {
            Text(detail)
                .font(event.kind.hasPrefix("tool_") ? .caption.monospaced() : .callout)
                .foregroundStyle(event.kind == "reasoning" ? .secondary : .primary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var icon: String {
        switch event.kind {
        case "reasoning": "brain"
        case "tool_call": "hammer"
        case "tool_result": "checkmark.square"
        case "human_injection": "person"
        case "permission_request": "hand.raised"
        case "lifecycle": "gearshape.2"
        default: "text.bubble"
        }
    }
}

struct MessageReceiptRow: View {
    let message: RunMessage
    var compact = false

    private var direction: String {
        switch message.direction {
        case "inbound": "Operator → run"
        case "outbound": "Run → operator"
        case "system": "System"
        default: message.direction ?? "Message"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Label(direction, systemImage: message.direction == "outbound"
                      ? "arrow.up.right" : message.direction == "inbound"
                      ? "arrow.down.left" : "gearshape")
                    .font(.caption.bold())
                if let kind = message.kind { StatusChip(status: kind) }
                Spacer()
                if !compact || message.status != "delivered" {
                    StatusChip(status: message.status)
                }
            }
            Text(message.body).textSelection(.enabled)
            if compact {
                Text(message.createdAt.relativeAge)
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                HStack(spacing: 10) {
                    Text("Created \(message.createdAt.relativeAge)")
                    if let delivered = message.deliveredAt {
                        Label("Delivered \(Optional(delivered).relativeAge)", systemImage: "checkmark.circle")
                    }
                    if let failed = message.undeliverableAt {
                        Label("Undeliverable \(Optional(failed).relativeAge)",
                              systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.red)
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
            if let failure = message.deliveryError {
                Text(failure).font(.caption).foregroundStyle(.red)
            }
            if !compact, message.correlationID != nil || message.replyTo != nil {
                Text([message.correlationID.map { "Correlation \($0)" },
                      message.replyTo.map { "Reply to #\($0)" }]
                    .compactMap { $0 }.joined(separator: " · "))
                    .font(.caption2.monospaced()).foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}

private enum DetailLoad: Sendable {
    case run(Run), thread(APIPage<RunMessage>), events(APIPage<RunEvent>), artifacts([Artifact])
    case changes(RunChanges), lineage(RunLineage), observer(ObserverRunDetail), log(RawLogTail)
    case failure(RunTab, String)

    enum Kind { case run, thread, events, artifacts, changes, lineage, observer, log }

    static func capture(_ kind: Kind, tab: RunTab,
                        operation: () async throws -> DetailLoad) async -> DetailLoad {
        do { return try await operation() }
        catch { return .failure(tab, error.localizedDescription) }
    }
}

private struct ControlPrompt: Identifiable {
    let id = UUID()
    let action: String
    let title: String
    let label: String
}

private struct ControlPromptView: View {
    @Environment(\.dismiss) private var dismiss
    let prompt: ControlPrompt
    let submit: (String) -> Void
    @State private var text = ""

    var body: some View {
        NavigationStack {
            Form {
                Section(prompt.label) { TextEditor(text: $text).frame(minHeight: 140) }
                if prompt.action == "interrupt" {
                    Section { Text("Interrupt cancels the active turn and resumes this same run. A fallback restart is audited for replay risk.")
                            .font(.caption).foregroundStyle(.secondary) }
                }
            }
            .navigationTitle(prompt.title)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") { submit(text.trimmed); dismiss() }.disabled(text.trimmed.isEmpty)
                }
            }
        }
        .frame(minWidth: 360, minHeight: 300)
    }
}

private struct ArtifactPreview: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let artifact: Artifact
    @State private var data: Data?
    @State private var fileURL: URL?
    @State private var player: AVPlayer?
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Group {
                if let error {
                    ContentUnavailableView("Preview unavailable", systemImage: "exclamationmark.triangle",
                                           description: Text(error))
                } else if let data {
                    preview(data)
                } else { LoadingState(label: "Downloading artifact…") }
            }
            .navigationTitle(artifact.name)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                if let fileURL {
                    ToolbarItem(placement: .automatic) { ShareLink(item: fileURL) }
                }
            }
            .task { await load() }
            .onDisappear { player?.pause() }
        }
        .frame(minWidth: 360, minHeight: 360)
    }

    @ViewBuilder private func preview(_ data: Data) -> some View {
        if artifact.mediaType == "text/markdown",
           let text = String(data: data, encoding: .utf8) {
            ScrollView { Text((try? AttributedString(markdown: text)) ?? AttributedString(text))
                    .frame(maxWidth: .infinity, alignment: .leading).padding().textSelection(.enabled) }
        } else if artifact.mediaType.hasPrefix("text/") || artifact.mediaType == "application/json",
                  let text = String(data: data, encoding: .utf8) {
            ScrollView([.horizontal, .vertical]) {
                Text(text).font(.body.monospaced()).frame(maxWidth: .infinity, alignment: .leading)
                    .padding().textSelection(.enabled)
            }
        } else if artifact.mediaType.hasPrefix("image/") {
#if os(macOS)
            if let image = NSImage(data: data) {
                ScrollView([.horizontal, .vertical]) { Image(nsImage: image).resizable().scaledToFit().padding() }
            } else { unsupported }
#else
            if let image = UIImage(data: data) {
                ScrollView([.horizontal, .vertical]) { Image(uiImage: image).resizable().scaledToFit().padding() }
            } else { unsupported }
#endif
        } else if artifact.mediaType == "application/pdf" {
            PDFPreview(data: data)
        } else if artifact.mediaType.hasPrefix("audio/") || artifact.mediaType.hasPrefix("video/"),
                  let player {
            VStack {
                VideoPlayer(player: player)
                Text(artifact.mediaType).font(.caption).foregroundStyle(.secondary)
            }.padding()
        } else { unsupported }
    }

    private var unsupported: some View {
        ContentUnavailableView("No inline preview", systemImage: "doc",
                               description: Text("Use Share to open this \(artifact.mediaType) artifact in another app."))
    }

    private func load() async {
        do {
            let value = try await state.api().artifactContent(artifact.id)
            let ext = URL(fileURLWithPath: artifact.name).pathExtension
            let safe = artifact.sha256.prefix(20) + (ext.isEmpty ? "" : ".\(ext)")
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(String(safe))
            try value.write(to: url, options: .atomic)
            data = value
            fileURL = url
            if artifact.mediaType.hasPrefix("audio/") || artifact.mediaType.hasPrefix("video/") {
                player = AVPlayer(url: url)
            }
        } catch { self.error = error.localizedDescription }
    }
}

#if os(macOS)
private struct PDFPreview: NSViewRepresentable {
    let data: Data
    func makeNSView(context: Context) -> PDFView { let view = PDFView(); view.autoScales = true; return view }
    func updateNSView(_ view: PDFView, context: Context) { view.document = PDFDocument(data: data) }
}
#else
private struct PDFPreview: UIViewRepresentable {
    let data: Data
    func makeUIView(context: Context) -> PDFView { let view = PDFView(); view.autoScales = true; return view }
    func updateUIView(_ view: PDFView, context: Context) { view.document = PDFDocument(data: data) }
}
#endif

private extension String {
    var systemImage: String {
        if hasPrefix("image/") { return "photo" }
        if hasPrefix("audio/") { return "waveform" }
        if hasPrefix("video/") { return "film" }
        if self == "application/pdf" { return "doc.richtext" }
        if hasPrefix("text/") { return "doc.text" }
        return "doc"
    }
}
