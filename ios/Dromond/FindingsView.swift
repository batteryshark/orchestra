import SwiftUI

/// The needs-you queue: what a worker noticed and did not fix, what it wants
/// to do next, and which runs are waiting on other runs.
///
/// Everything here is a read plus the two writes the daemon actually serves
/// (`check`, `stop`). There is no route that dismisses a finding, so there is
/// no button that claims to.
struct FindingsView: View {
    @EnvironmentObject private var state: AppState
    @State private var actionError: String?
    @State private var acting = false

    var body: some View {
        NavigationStack {
            ScrollView {
                ConnectionBanner()
                LazyVStack(alignment: .leading, spacing: 12) {
                    if findings.isEmpty && proposals.isEmpty && blockedRuns.isEmpty {
                        quiet
                    }

                    if !findings.isEmpty {
                        header("Findings", findings.count)
                        // A finding carries no guaranteed id — the daemon's
                        // table is `SELECT *` over rows that may predate one —
                        // so position is the only identity that cannot collide.
                        ForEach(Array(findings.enumerated()), id: \.offset) { _, finding in
                            FindingCard(finding: finding, run: run(finding.run))
                        }
                    }

                    if !proposals.isEmpty {
                        header("Proposals", proposals.count)
                        ForEach(Array(proposals.enumerated()), id: \.offset) { _, proposal in
                            ProposalCard(proposal: proposal, run: run(proposal.run))
                        }
                    }

                    if !blockedRuns.isEmpty {
                        header("Blocked runs", blockedRuns.count)
                        ForEach(blockedRuns) { blocked in
                            BlockedCard(run: blocked, blockers: blocked.blockedOn.map { ($0, run($0)) },
                                        acting: acting,
                                        check: { Task { await check(blocked.id) } })
                        }
                    }
                }
                .padding(.horizontal)
                .padding(.top, 4)
                .padding(.bottom, 24)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Findings")
            .toolbar { ProjectToolbarMenu() }
            .navigationDestination(for: Run.self) { RunDetailView(run: $0) }
            .refreshable { await state.refresh() }
            .alert("Check failed", isPresented: Binding(
                get: { actionError != nil },
                set: { if !$0 { actionError = nil } }
            )) {
                Button("OK") { actionError = nil }
            } message: {
                Text(actionError ?? "")
            }
        }
    }

    // --- what the queue holds ---------------------------------------------

    /// Observed before suspected — one was seen, the other was guessed — then
    /// newest first inside each group.
    private var findings: [Finding] {
        forSelectedProject(state.snapshot?.findings ?? [], run: \.run).sorted { a, b in
            let seen = (a.confidence == "observed", b.confidence == "observed")
            if seen.0 != seen.1 { return seen.0 }
            return (a.at ?? "") > (b.at ?? "")
        }
    }

    /// Unevaluated first: no planner turn ran, so a person is the only judge
    /// left. Then pivots, then the ones a planner already called aligned.
    private var proposals: [Proposal] {
        forSelectedProject(state.snapshot?.proposals ?? [], run: \.run).sorted { a, b in
            let rank = (Self.verdictRank(a.verdict), Self.verdictRank(b.verdict))
            if rank.0 != rank.1 { return rank.0 < rank.1 }
            return (a.at ?? "") > (b.at ?? "")
        }
    }

    /// Oldest first: the run that has waited longest is the one waiting.
    private var blockedRuns: [Run] { state.blockedRuns.sorted { $0.id < $1.id } }

    private static func verdictRank(_ verdict: String?) -> Int {
        switch verdict {
        case "aligned": 2
        case "pivot": 1
        default: 0 // nil, empty, or anything a planner did not produce
        }
    }

    /// The toolbar's project pick has to mean something on this tab too. A
    /// record with no run cannot be attributed to a project, so it stays.
    private func forSelectedProject<T>(_ items: [T], run key: KeyPath<T, Int?>) -> [T] {
        guard state.selectedProjectID != nil else { return items }
        let visible = Set(state.runs.map(\.id))
        return items.filter { item in
            guard let id = item[keyPath: key] else { return true }
            return visible.contains(id)
        }
    }

    private func run(_ id: Int?) -> Run? {
        guard let id else { return nil }
        return state.snapshot?.runs.first { $0.id == id }
    }

    private func check(_ runID: Int) async {
        acting = true
        defer { acting = false }
        actionError = await state.perform { try await $0.check(runID: runID) }
    }

    // --- chrome ------------------------------------------------------------

    private func header(_ title: String, _ count: Int) -> some View {
        HStack(spacing: 8) {
            Text(title).font(.headline)
            Text("\(count)")
                .font(.caption.weight(.bold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 7)
                .padding(.vertical, 2)
                .background(Color(.secondarySystemGroupedBackground), in: Capsule())
            Spacer()
        }
        .padding(.top, 8)
    }

    private var quiet: some View {
        ContentUnavailableView {
            Label {
                Text("Nothing needs you")
            } icon: {
                Image(systemName: "checkmark.circle").foregroundStyle(.green)
            }
        } description: {
            Text("No findings, no proposals, nothing blocked.")
        }
        .frame(maxWidth: .infinity, minHeight: 420)
    }
}

// --- cards ------------------------------------------------------------------

private struct FindingCard: View {
    let finding: Finding
    let run: Run?

    private var observed: Bool { finding.confidence == "observed" }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                WrappedText(text: finding.claim ?? "(no claim)",
                            font: observed ? .headline : .subheadline.weight(.semibold))
                ConfidenceChip(confidence: finding.confidence).fixedSize()
            }

            if let with = finding.with, !with.isEmpty {
                DetailRow(icon: "mappin.and.ellipse", text: with, monospaced: true)
            }
            if let why = finding.whyNotFixed, !why.isEmpty {
                DetailRow(icon: "wrench.and.screwdriver", text: "Not fixed: \(why)")
            }
            if let filed = finding.filedAs, !filed.isEmpty {
                DetailRow(icon: "tray.and.arrow.down", text: "Filed as \(filed)", tint: .blue)
            }

            CardFooter(run: run, runID: finding.run, at: finding.at)
        }
        .cardStyle(stroke: observed ? .orange.opacity(0.45) : nil)
    }
}

private struct ProposalCard: View {
    let proposal: Proposal
    let run: Run?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            WrappedText(text: proposal.title ?? "(untitled proposal)", font: .headline)

            if let why = proposal.why, !why.isEmpty {
                WrappedText(text: why, font: .subheadline, color: .secondary)
            }
            if let action = proposal.action, !action.isEmpty {
                DetailRow(icon: "arrow.turn.down.right", text: action)
            }

            // No verdict is not a verdict. A proposal nobody judged is the
            // one most in need of a person, and saying "unevaluated" is the
            // only honest way to draw that.
            VerdictRow(verdict: proposal.verdict)

            CardFooter(run: run, runID: proposal.run, at: proposal.at)
        }
        .cardStyle(stroke: proposal.verdict == nil ? .blue.opacity(0.35) : nil)
    }
}

private struct BlockedCard: View {
    let run: Run
    let blockers: [(id: Int, run: Run?)]
    let acting: Bool
    let check: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                WrappedText(text: run.displayTitle, font: .headline)
                StatusChip(status: run.status)
            }

            Text("Waiting on").font(.caption).foregroundStyle(.secondary)
            ForEach(blockers, id: \.id) { blocker in
                HStack(spacing: 8) {
                    Text("#\(blocker.id)").font(.subheadline.monospaced())
                    if let other = blocker.run {
                        Text(other.displayTitle).font(.subheadline).foregroundStyle(.secondary)
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        StatusChip(status: other.status)
                    } else {
                        Text("not in this snapshot").font(.subheadline).foregroundStyle(.secondary)
                        Spacer(minLength: 4)
                    }
                }
            }

            Divider()
            HStack {
                NavigationLink(value: run) {
                    Label("run #\(run.id)", systemImage: "arrow.forward.circle").font(.caption)
                }
                Spacer()
                Button {
                    check()
                } label: {
                    Label("Check", systemImage: "stethoscope").font(.caption)
                }
                .buttonStyle(.bordered)
                .disabled(acting)
            }
        }
        .cardStyle(stroke: .purple.opacity(0.35))
    }
}

// --- parts ------------------------------------------------------------------

/// Observed is filled and loud; suspected is tinted and quiet. Drawing them
/// the same is how a real bug gets read as a guess.
private struct ConfidenceChip: View {
    let confidence: String?

    var body: some View {
        let observed = confidence == "observed"
        let label = (confidence?.isEmpty == false ? confidence : nil) ?? "unstated"
        Text(label)
            .font(.caption2.weight(.bold))
            .foregroundStyle(observed ? Color.white : .secondary)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(observed ? AnyShapeStyle(Color.orange)
                                 : AnyShapeStyle(Color.secondary.opacity(0.14)),
                        in: Capsule())
            .accessibilityLabel("Confidence: \(label)")
    }
}

private struct VerdictRow: View {
    let verdict: String?

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).foregroundStyle(tint).font(.caption)
                .frame(width: 16, alignment: .center)
            Text(text).font(.caption).foregroundStyle(tint)
            Spacer(minLength: 0)
        }
    }

    private var text: String {
        switch verdict {
        case "aligned": "Planner verdict: aligned"
        case "pivot": "Planner verdict: pivot"
        case let other? where !other.isEmpty: "Planner verdict: \(other)"
        default: "Unevaluated — no planner turn ran"
        }
    }

    private var icon: String {
        switch verdict {
        case "aligned": "checkmark.seal"
        case "pivot": "arrow.triangle.branch"
        default: "person.fill.questionmark"
        }
    }

    private var tint: Color {
        switch verdict {
        case "aligned": .green
        case "pivot": .orange
        default: .blue
        }
    }
}

private struct DetailRow: View {
    let icon: String
    let text: String
    var monospaced = false
    var tint: Color = .secondary

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).font(.caption).foregroundStyle(tint)
                .frame(width: 16, alignment: .center)
            WrappedText(text: text,
                        font: monospaced ? .caption.monospaced() : .subheadline,
                        color: monospaced ? .primary : .secondary)
        }
    }
}

/// Where it came from and when. The run is a link when the snapshot still
/// carries it, and plain text when it does not.
private struct CardFooter: View {
    let run: Run?
    let runID: Int?
    let at: String?

    var body: some View {
        VStack(spacing: 8) {
            Divider()
            HStack(spacing: 8) {
                if let run {
                    NavigationLink(value: run) {
                        Label("run #\(run.id)", systemImage: "arrow.forward.circle").font(.caption)
                    }
                } else if let runID {
                    Label("run #\(runID)", systemImage: "circle.dashed")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if let when = Self.relative(at) {
                    Text(when).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    /// The daemon writes UTC ISO 8601. An unparseable stamp is shown as it
    /// arrived rather than dropped.
    static func relative(_ at: String?) -> String? {
        guard let at, !at.isEmpty else { return nil }
        guard let date = try? Date(at, strategy: .iso8601) else { return at }
        return date.formatted(.relative(presentation: .named))
    }
}

private extension View {
    func cardStyle(stroke: Color? = nil) -> some View {
        self
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(Color(.secondarySystemGroupedBackground),
                        in: RoundedRectangle(cornerRadius: 16))
            .overlay {
                if let stroke {
                    RoundedRectangle(cornerRadius: 16).strokeBorder(stroke, lineWidth: 1)
                }
            }
    }
}
