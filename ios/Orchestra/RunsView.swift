import SwiftUI

/// The fleet, newest first, live work at the top.
///
/// The web dashboard sorts live runs above history because a live run is the
/// only one you can still change. Two sections say the same thing without a
/// filter control the owner has to remember to set.
struct RunsView: View {
    @EnvironmentObject private var state: AppState
    @State private var query = ""
    @State private var actionError: String?

    /// Honours `-openRun <id>` once, when that run is in the snapshot.
    private func openRequestedRun() {
        let arguments = ProcessInfo.processInfo.arguments
        guard path.isEmpty,
              let at = arguments.firstIndex(of: "-openRun"),
              let id = arguments[safe: at + 1].flatMap(Int.init),
              let run = state.runs.first(where: { $0.id == id })
        else { return }
        path = [.run(run)]
    }

    private var matching: [Run] {
        let runs = state.runs.sorted { $0.id > $1.id }
        let needle = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !needle.isEmpty else { return runs }
        return runs.filter { run in
            [String(run.id), run.title, run.workItem, run.slug, run.profile,
             run.project, run.status]
                .compactMap { $0 }
                .contains { $0.lowercased().contains(needle) }
        }
    }

    @State private var path: [RunsRoute] = []

    var body: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                ConnectionBanner()
                list
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Runs")
            .toolbar { ServerToolbarMenu(); ProjectToolbarMenu() }
            .navigationDestination(for: RunsRoute.self) { route in
                switch route {
                case let .run(run): RunDetailView(run: run)
                case .decisions: DecisionLogView()
                }
            }
            .searchable(text: $query, prompt: "id, title, work item, profile")
            // Everything searched here is an identifier — a slug, a profile
            // name, W-0171. Autocapitalising the first letter is wrong every
            // time, and autocorrect turns "ds-flash" into something else.
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            // The sibling of ContentView's `-startTab`: `-openRun 30` pushes
            // that run's detail as soon as a snapshot is available, so a
            // screenshot of any sub-tab needs no taps at all.
            //
            // This used to hang off `.onChange(of: state.runs.count)` alone and
            // silently did nothing whenever a snapshot was ALREADY loaded when
            // the view appeared: the count never changed, so the deep link
            // never fired and the screenshot agent gave up and tapped instead.
            // Fire on appear as well, and keep trying while the count moves,
            // since the run may not be in the first snapshot either.
            .onAppear { openRequestedRun() }
            .onChange(of: state.runs.count) { _, _ in openRequestedRun() }
            .alert("That did not go through", isPresented: .init(
                get: { actionError != nil },
                set: { if !$0 { actionError = nil } }
            )) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(actionError ?? "")
            }
        }
    }

    @ViewBuilder
    private var list: some View {
        let live = matching.filter(\.live)
        let rest = matching.filter { !$0.live }
        List {
            // W-0214: the most recent control turn for THIS project — the
            // staffing, merge, or observer decision that just happened —
            // pinned above the fleet.
            // It is never in `state.runs`, so the badge and the live count
            // do not move for it. It opens the same detail screen as a run.
            // I-0081: the latest decision is one line of a series. The second
            // row is the way into the rest of it — the turns going back, which
            // is where the observer's reasoning is actually readable.
            if query.isEmpty, let turn = state.pinnedTurn {
                Section("Latest decision") {
                    NavigationLink(value: RunsRoute.run(turn)) { TurnRow(turn: turn) }
                    NavigationLink(value: RunsRoute.decisions) {
                        Label("Decision log", systemImage: "list.bullet.rectangle")
                            .font(.subheadline)
                    }
                }
            }
            if !live.isEmpty {
                Section("Live · \(live.count)") {
                    ForEach(live) { row($0) }
                }
            }
            if !rest.isEmpty {
                Section(live.isEmpty ? "Runs" : "History") {
                    ForEach(rest) { row($0) }
                }
            }
            if matching.isEmpty {
                ContentUnavailableView(
                    query.isEmpty ? "No runs" : "No match",
                    systemImage: query.isEmpty ? "tray" : "magnifyingglass",
                    description: Text(query.isEmpty
                        ? "Nothing has run in this project yet."
                        : "No run matches “\(query)”.")
                )
            }
        }
        .listStyle(.insetGrouped)
        .refreshable { await state.refresh() }
    }

    private func row(_ run: Run) -> some View {
        NavigationLink(value: RunsRoute.run(run)) { RunRow(run: run) }
            .swipeActions(edge: .trailing) {
                if run.live {
                    Button("Stop", systemImage: "stop.circle", role: .destructive) {
                        Task {
                            actionError = await state.perform { try await $0.stop(runID: run.id) }
                        }
                    }
                }
            }
    }
}

/// What the Runs tab can push. A plain `[Run]` path could not carry the
/// decision log, and a view-based link beside a value-based path is the one
/// combination SwiftUI does not keep straight.
enum RunsRoute: Hashable {
    case run(Run)
    case decisions
}

/// Every control turn, newest first (I-0081).
///
/// The snapshot pins one turn per project — the newest. That reads as a single
/// line with no history, which is not what a decision is: the observer looked
/// at this run four times before it stopped it. This screen is the series, and
/// each row opens the same detail screen, so the reasoning is the trace tab
/// that already exists.
///
/// Scoped like the board it hangs off: the project picker on the Runs tab
/// decides which project's turns these are.
struct DecisionLogView: View {
    @EnvironmentObject private var state: AppState
    @State private var turns: [Run] = []
    @State private var layer: String?
    @State private var error: String?
    @State private var loaded = false

    /// The four layers a turn can come from. A closed set in the daemon, so
    /// the picker names them rather than asking what exists.
    private static let layers = ["observer", "router", "merge", "conductor"]

    var body: some View {
        List {
            if let error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }
            ForEach(turns) { turn in
                NavigationLink(value: RunsRoute.run(turn)) { TurnRow(turn: turn) }
            }
            if turns.isEmpty && loaded && error == nil {
                ContentUnavailableView(
                    "No decisions",
                    systemImage: "brain",
                    description: Text(layer == nil
                        ? "Nothing has been decided in this project yet."
                        : "No \(layer ?? "") turn in this project yet.")
                )
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Decision log")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button { layer = nil } label: {
                        if layer == nil {
                            Label("All layers", systemImage: "checkmark")
                        } else {
                            Text("All layers")
                        }
                    }
                    Divider()
                    ForEach(Self.layers, id: \.self) { name in
                        Button { layer = name } label: {
                            if layer == name {
                                Label(name, systemImage: "checkmark")
                            } else {
                                Text(name)
                            }
                        }
                    }
                } label: {
                    HStack(spacing: 5) {
                        Text(layer ?? "All layers").lineLimit(1)
                        Image(systemName: "line.3.horizontal.decrease.circle")
                            .font(.caption2)
                    }
                }
            }
        }
        // Reloads when the filter changes: the daemon applies the layer, so
        // the page is a hundred of the turns asked for, not a hundred of
        // everything with four of them left after a local filter.
        .task(id: layer) { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        do {
            turns = try await state.api().turns(projectID: state.selectedProjectID,
                                                layer: layer)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        loaded = true
    }
}

/// One control turn: which layer decided, what it decided, and when. The
/// summary names the escalation it produced. Opens the ordinary run detail
/// screen, where its transcript is the trace tab.
private struct TurnRow: View {
    let turn: Run

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: "brain")
                    .font(.caption)
                    // The observer is the layer the owner reads for; the
                    // other three are the machine staffing and judging itself.
                    .foregroundStyle(turn.layer == "observer" ? Color.accentColor : .secondary)
                    .accessibilityLabel("Control turn")
                Text(turn.layer ?? "turn")
                    .font(.subheadline.monospaced().weight(.semibold))
                Text("· \(turn.profile)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 6)
                StatusChip(status: turn.status)
            }
            if !turn.summary.isEmpty {
                Text(turn.summary)
                    .font(.subheadline)
                    .lineLimit(3)
            }
            if let at = turn.finishedAt ?? turn.startedAt {
                Text(at.relativeStamp)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 3)
    }
}

/// One line of the fleet: what it is, how it is doing, whose work it is.
private struct RunRow: View {
    let run: Run

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Circle()
                    .fill(run.live ? Color.green : Color.clear)
                    .frame(width: 7, height: 7)
                    .accessibilityLabel(run.live ? "Live" : "")
                Text("#\(run.id)")
                    .font(.subheadline.monospaced().weight(.semibold))
                if let tag = run.workItem ?? run.slug, !tag.isEmpty {
                    Text(tag)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 6)
                StatusChip(status: run.status)
            }
            Text(run.displayTitle)
                .font(.subheadline)
                .lineLimit(2)
            HStack(spacing: 6) {
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if let elapsed = run.elapsedSeconds {
                    Text(elapsed.durationLabel)
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            if !run.blockedOn.isEmpty {
                Label(
                    "Blocked on " + run.blockedOn.map { "#\($0)" }.joined(separator: ", "),
                    systemImage: "hand.raised"
                )
                .font(.caption2)
                .foregroundStyle(.orange)
            }
        }
        .padding(.vertical, 3)
    }

    private var subtitle: String {
        [run.project, run.profile]
            .compactMap { $0?.isEmpty == false ? $0 : nil }
            .joined(separator: " · ")
    }
}
