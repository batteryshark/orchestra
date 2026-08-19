import SwiftUI

/// The machine's own state: the daemon, the observer that watches it, the
/// dispatch gate, and where its files are.
struct HealthView: View {
    @EnvironmentObject private var state: AppState
    @State private var configPath: String?
    @State private var confirmingRestart = false
    @State private var busy = false
    @State private var error: String?
    @State private var lastAction: String?

    private var daemon: Daemon { state.snapshot?.daemon ?? Daemon() }
    private var dispatch: Dispatch { state.snapshot?.dispatch ?? Dispatch(paused: false, since: nil) }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    ConnectionBanner()
                    if let error {
                        Banner(text: error, icon: "exclamationmark.triangle.fill", tint: .red)
                    } else if let lastAction {
                        Banner(text: lastAction, icon: "checkmark.circle.fill", tint: .green)
                    }
                    daemonCard
                    observerCard
                    dispatchCard
                    locationsCard
                }
                .padding()
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Health")
            .toolbar { ServerToolbarMenu(); ProjectToolbarMenu() }
            .refreshable { await state.refresh() }
            .task { await loadConfigPath() }
            .confirmationDialog(
                "Restart the daemon?",
                isPresented: $confirmingRestart,
                titleVisibility: .visible
            ) {
                Button("Restart daemon", role: .destructive) { Task { await restart() } }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Running work keeps going; the daemon re-reads its config and resumes sweeping.")
            }
        }
    }

    // --- daemon -----------------------------------------------------------

    private var daemonCard: some View {
        Card("Daemon", icon: "gearshape.2") {
            if let pid = daemon.pid {
                Row("Running", value: "pid \(pid)", tint: .green)
            } else {
                Row("Not running", value: "no pid", tint: .red)
            }
            if let started = daemon.startedAt {
                Row("Started", value: Self.stamp(started))
            }
            if let sweep = daemon.lastSweepAt {
                Row("Last sweep", value: Self.stamp(sweep))
            }
            if let outcome = daemon.outcome {
                Row("Outcome", value: outcome, tint: outcome == "ok" ? .green : .orange)
            }

            HStack(spacing: 12) {
                MetricCard(title: "Actions", value: "\(daemon.actions ?? 0)",
                           systemImage: "bolt")
                MetricCard(title: "Released", value: "\(daemon.released ?? 0)",
                           systemImage: "lock.open", tint: .teal)
                MetricCard(title: "Reaped", value: "\(daemon.reaped ?? 0)",
                           systemImage: "trash", tint: .orange)
            }
            .padding(.top, 4)

            if let problem = daemon.error, !problem.isEmpty {
                Banner(text: problem, icon: "exclamationmark.octagon.fill", tint: .red)
            }
            // History, not current state: last_error sticks until the next
            // failure, so a fixed crash from days ago used to sit here in
            // alarm orange next to a healthy daemon. Collapsed by default —
            // the daemon log holds the traceback worth acting on.
            if let last = daemon.lastError, !last.isEmpty {
                DisclosureGroup {
                    WrappedText(text: last, font: .caption, color: .secondary)
                        .padding(.top, 2)
                } label: {
                    Text(daemon.lastErrorAt.map { "Last error · \(Self.stamp($0))" } ?? "Last error")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                .tint(.secondary)
                .padding(.top, 4)
            }

            HStack(spacing: 12) {
                Button {
                    Task { await act("Sweep queued.") { try await $0.sweep() } }
                } label: {
                    Label("Sweep now", systemImage: "arrow.triangle.2.circlepath")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)

                Button(role: .destructive) {
                    confirmingRestart = true
                } label: {
                    Label("Restart", systemImage: "power")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
            }
            .disabled(busy)
            .padding(.top, 8)
        }
    }

    // --- observer ---------------------------------------------------------

    private var observerCard: some View {
        Card("Observer", icon: "eye") {
            if let observer = daemon.observer {
                if let problem = observer.problem, !problem.isEmpty {
                    // The observer saying it cannot run is the whole point of
                    // this card, so it goes above the settings it cannot use.
                    Banner(text: problem, icon: "eye.slash.fill", tint: .red)
                }
                Row(observer.enabled ? "Watching" : "Off",
                    value: observer.profile ?? "no profile set",
                    tint: observer.enabled ? .green : .secondary)
                Row("First look", value: Self.minutes(observer.firstLook))
                Row("Then every", value: Self.minutes(observer.interval))
            } else {
                Text("The daemon reports no observer.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // --- dispatch ---------------------------------------------------------

    private var dispatchCard: some View {
        Card("Dispatch", icon: "arrow.triangle.branch") {
            Row(dispatch.paused ? "Paused" : "Running",
                value: dispatch.since.map(Self.stamp) ?? "—",
                tint: dispatch.paused ? .orange : .green)
            Text(dispatch.paused
                 ? "Nothing new launches while dispatch is paused. Running work is untouched."
                 : "New work launches as the queue allows.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                let pause = !dispatch.paused
                Task {
                    await act(pause ? "Dispatch paused." : "Dispatch resumed.") {
                        try await $0.pauseDispatch(pause)
                    }
                }
            } label: {
                Label(dispatch.paused ? "Resume dispatch" : "Pause dispatch",
                      systemImage: dispatch.paused ? "play.fill" : "pause.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(dispatch.paused ? .green : .orange)
            .disabled(busy)
            .padding(.top, 4)
        }
    }

    // --- where things live ------------------------------------------------

    private var locationsCard: some View {
        Card("Where things live", icon: "folder") {
            VStack(alignment: .leading, spacing: 4) {
                Text("Home").font(.caption).foregroundStyle(.secondary)
                WrappedText(text: state.snapshot?.home ?? "unknown", font: .footnote.monospaced())
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Config").font(.caption).foregroundStyle(.secondary)
                WrappedText(text: configPath ?? "unknown", font: .footnote.monospaced())
            }
            .padding(.top, 4)
            VStack(alignment: .leading, spacing: 4) {
                Text("Server").font(.caption).foregroundStyle(.secondary)
                WrappedText(text: state.serverURL, font: .footnote.monospaced())
            }
            .padding(.top, 4)
        }
    }

    // --- actions ----------------------------------------------------------

    private func act(_ done: String, _ body: @escaping (DromondAPI) async throws -> Void) async {
        busy = true
        defer { busy = false }
        if let problem = await state.perform(body) {
            error = problem
            lastAction = nil
        } else {
            error = nil
            lastAction = done
        }
    }

    /// The reply to a restart may never arrive — the process answering is the
    /// one going away — and the client already treats that transport failure
    /// as success, so nothing here turns it back into an error.
    private func restart() async {
        await act("Daemon restarting.") { try await $0.restart() }
    }

    private func loadConfigPath() async {
        configPath = try? await state.api().config().path
    }

    // --- formatting -------------------------------------------------------

    /// Seconds as the minutes the owner set them in.
    private static func minutes(_ seconds: Int?) -> String {
        guard let seconds else { return "—" }
        let m = Double(seconds) / 60
        return m < 1
            ? "\(seconds)s"
            : (m == m.rounded() ? "\(Int(m)) min" : String(format: "%.1f min", m))
    }

    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    private static let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .full
        return f
    }()

    /// "2 minutes ago" beats a UTC stamp for everything on this screen — but
    /// an unparseable one is shown as it came, never dropped.
    private static func stamp(_ text: String) -> String {
        guard let date = iso.date(from: text) else { return text }
        return relative.localizedString(for: date, relativeTo: Date())
    }
}

// --- small pieces this screen builds from -----------------------------------

private struct Card<Content: View>: View {
    let title: String
    let icon: String
    @ViewBuilder let content: Content

    init(_ title: String, icon: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.icon = icon
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: icon)
                .font(.headline)
                .foregroundStyle(.primary)
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(Color(.secondarySystemGroupedBackground),
                    in: RoundedRectangle(cornerRadius: 16))
    }
}

private struct Row: View {
    let label: String
    let value: String
    var tint: Color = .primary

    init(_ label: String, value: String, tint: Color = .primary) {
        self.label = label
        self.value = value
        self.tint = tint
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label).font(.subheadline.weight(.medium)).foregroundStyle(tint)
            Spacer(minLength: 12)
            Text(value)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.trailing)
                .textSelection(.enabled)
        }
    }
}

private struct Banner: View {
    let text: String
    let icon: String
    let tint: Color

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).foregroundStyle(tint)
            WrappedText(text: text, font: .footnote)
        }
        .padding(10)
        .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 12))
    }
}
