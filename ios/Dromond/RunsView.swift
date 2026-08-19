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
        path = [run]
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

    @State private var path: [Run] = []

    var body: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                ConnectionBanner()
                list
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Runs")
            .toolbar { ServerToolbarMenu(); ProjectToolbarMenu() }
            .navigationDestination(for: Run.self) { RunDetailView(run: $0) }
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
        NavigationLink(value: run) { RunRow(run: run) }
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
