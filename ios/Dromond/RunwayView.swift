import SwiftUI

/// Headroom first, history second: what the providers will still let the
/// fleet spend, then what it has already spent.
struct RunwayView: View {
    @EnvironmentObject private var state: AppState
    @State private var polling = false
    @State private var pollError: String?

    private var runway: [Runway] { state.snapshot?.runway ?? [] }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    ConnectionBanner()
                    providers
                    if let statistics = state.snapshot?.statistics {
                        StatisticsSection(stats: statistics)
                    }
                }
                .padding(.vertical)
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Runway")
            .toolbar { ProjectToolbarMenu() }
            .refreshable { await state.refresh() }
        }
    }

    private var providers: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text("Provider runway").font(.title3.bold())
                Spacer()
                Button(action: { Task { await poll() } }) {
                    if polling {
                        ProgressView().controlSize(.small)
                    } else {
                        Label("Poll providers", systemImage: "antenna.radiowaves.left.and.right")
                            .font(.subheadline)
                    }
                }
                .buttonStyle(.bordered)
                .disabled(polling)
            }
            .padding(.horizontal)

            if let pollError {
                Text(pollError)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.horizontal)
            }

            if runway.isEmpty {
                ContentUnavailableView(
                    "No providers",
                    systemImage: "gauge.with.dots.needle.50percent"
                )
                .frame(height: 180)
            } else {
                ForEach(runway) { entry in
                    ProviderCard(entry: entry).padding(.horizontal)
                }
            }
        }
    }

    /// Deliberate: this asks every provider live and spends the owner's own
    /// request budget. Never on a timer, never on appear.
    private func poll() async {
        polling = true
        pollError = await state.perform { _ = try await $0.runway(refresh: true) }
        polling = false
    }
}

// MARK: - Providers

private struct ProviderCard: View {
    let entry: Runway

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Text(entry.provider).font(.headline)
                if !entry.kind.isEmpty {
                    Text(entry.kind)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Color.secondary.opacity(0.14), in: Capsule())
                }
                Spacer()
                if !entry.known {
                    Text("Not reported")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.orange)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(Color.orange.opacity(0.14), in: Capsule())
                }
            }

            if entry.known {
                // Several providers meter two windows at once. A five-hour
                // window at 100% next to a weekly one at 0% is the whole
                // point, so every window gets its own bar.
                if entry.windows.isEmpty {
                    GaugeRow(
                        label: nil,
                        remaining: entry.remaining,
                        unit: entry.unit,
                        resetsIn: entry.resetsIn
                    )
                } else {
                    ForEach(entry.windows) { window in
                        GaugeRow(
                            label: window.label,
                            remaining: window.remaining,
                            unit: window.unit,
                            resetsIn: window.resetsIn
                        )
                    }
                }
                if let credits = entry.creditsLabel {
                    Label(credits, systemImage: "arrow.clockwise.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else if let reason = entry.reason?.trimmingCharacters(in: .whitespacesAndNewlines),
                      !reason.isEmpty {
                WrappedText(text: reason, font: .caption, color: .secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            Color(.secondarySystemGroupedBackground),
            in: RoundedRectangle(cornerRadius: 16)
        )
    }

}

/// One window's headroom. A missing number stays missing: no bar is drawn
/// for a figure the provider did not give, because an empty bar reads as
/// "you are out" and that is a different statement.
private struct GaugeRow: View {
    let label: String?
    let remaining: Double?
    let unit: String?
    let resetsIn: String?

    private var isPercent: Bool { unit?.lowercased() == "percent" }

    private var percent: Double? {
        guard isPercent, let remaining else { return nil }
        return min(max(remaining, 0), 100)
    }

    private var tint: Color {
        guard let percent else { return .secondary }
        if percent >= 50 { return .green }
        if percent >= 20 { return .orange }
        return .red
    }

    private var valueLabel: String {
        guard let remaining else { return "not reported" }
        if isPercent { return "\(Int(remaining.rounded()))%" }
        let unit = unit ?? ""
        if unit.uppercased() == "USD" { return String(format: "$%.2f", remaining) }
        let number = remaining == remaining.rounded()
            ? "\(Int(remaining))"
            : String(format: "%.1f", remaining)
        return unit.isEmpty ? number : "\(number) \(unit)"
    }

    /// A window that has already rolled over tells the owner nothing, so
    /// "now" and the daemon's placeholders are dropped rather than printed.
    private var resetLabel: String? {
        guard let resetsIn = resetsIn?.trimmingCharacters(in: .whitespacesAndNewlines),
              !resetsIn.isEmpty,
              !["-", "—", "now", "unknown", "never"].contains(resetsIn.lowercased())
        else { return nil }
        return "Resets \(resetsIn)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                if let label, !label.isEmpty {
                    Text(label).font(.subheadline.weight(.medium))
                    Spacer()
                }
                Text(valueLabel)
                    .font(.subheadline.monospacedDigit().weight(.semibold))
                    .foregroundStyle(remaining == nil ? .secondary : tint)
            }
            if let percent {
                ProgressView(value: percent, total: 100).tint(tint)
            }
            if let resetLabel {
                Text(resetLabel).font(.caption).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Statistics

private struct StatisticsSection: View {
    let stats: Statistics

    private let columns = [GridItem(.adaptive(minimum: 150), spacing: 12)]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Statistics").font(.title3.bold()).padding(.horizontal)

            LazyVGrid(columns: columns, spacing: 12) {
                MetricCard(
                    title: "Runs",
                    value: stats.runsTotal.grouped,
                    systemImage: "list.bullet.rectangle"
                )
                MetricCard(
                    title: "Active",
                    value: stats.runsActive.grouped,
                    systemImage: "bolt.fill",
                    tint: stats.runsActive > 0 ? .blue : .secondary
                )
                MetricCard(
                    title: "Worker time",
                    value: stats.workerSeconds.durationLabel,
                    systemImage: "clock",
                    tint: .purple
                )
                MetricCard(
                    title: "Tokens",
                    value: stats.tokensTotal.grouped,
                    systemImage: "number",
                    tint: .teal
                )
                MetricCard(
                    title: "Cost",
                    value: money(stats.costUSD),
                    systemImage: "dollarsign.circle",
                    tint: .green
                )
                MetricCard(
                    title: "On plan",
                    value: stats.planRuns.grouped,
                    systemImage: "creditcard",
                    tint: .indigo
                )
            }
            .padding(.horizontal)

            if !stats.byStatus.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text("By status").font(.subheadline.weight(.semibold))
                    ForEach(stats.byStatus.sorted { $0.value > $1.value }, id: \.key) { status, count in
                        HStack {
                            StatusChip(status: status)
                            Spacer()
                            Text(count.grouped).font(.subheadline.monospacedDigit())
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
                .background(
                    Color(.secondarySystemGroupedBackground),
                    in: RoundedRectangle(cornerRadius: 16)
                )
                .padding(.horizontal)
            }

            if !stats.byProfile.isEmpty {
                profileTable.padding(.horizontal)
            }
        }
    }

    private var profileTable: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("By profile").font(.subheadline.weight(.semibold))
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                GridRow {
                    Text("Profile")
                    Text("Runs").gridColumnAlignment(.trailing)
                    Text("Active").gridColumnAlignment(.trailing)
                    Text("Tokens").gridColumnAlignment(.trailing)
                    Text("Cost").gridColumnAlignment(.trailing)
                }
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)

                ForEach(stats.byProfile) { stat in
                    Divider().gridCellUnsizedAxes(.horizontal)
                    GridRow {
                        Text(stat.profile ?? "—").font(.caption.weight(.medium))
                        cell(stat.runs.map(\.grouped))
                        cell(stat.active.map(\.grouped))
                        cell(stat.tokens.map(\.grouped))
                        cell(stat.cost.map(money))
                    }
                }
            }
            .lineLimit(1)
            .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            Color(.secondarySystemGroupedBackground),
            in: RoundedRectangle(cornerRadius: 16)
        )
    }

    /// A profile on a plan reports no dollar figure and no token count. That
    /// is an em dash, not a zero.
    private func cell(_ text: String?) -> some View {
        Text(text ?? "—")
            .font(.caption.monospacedDigit())
            .foregroundStyle(text == nil ? .secondary : .primary)
            .frame(maxWidth: .infinity, alignment: .trailing)
    }

    private func money(_ value: Double) -> String {
        value >= 1 ? String(format: "$%.2f", value) : String(format: "$%.4f", value)
    }
}
