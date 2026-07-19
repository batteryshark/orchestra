import SwiftUI

struct UsageView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationStack {
            Group {
                if let usage = model.usage {
                    ScrollView {
                        LazyVStack(spacing: 14) {
                            HStack {
                                Label(usage.status == "ok" ? "Live usage" : "Partial usage",
                                      systemImage: usage.status == "ok" ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                                    .foregroundStyle(usage.status == "ok" ? .green : .orange)
                                Spacer()
                                Text("Updated \(OrchestraFormatting.timestamp(usage.generatedAt))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }

                            ForEach(usage.providers) { provider in
                                ProviderCard(provider: provider)
                            }
                        }
                        .padding()
                    }
                } else {
                    ProgressView("Loading provider usage…")
                }
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Usage")
            .refreshable { await model.refreshUsage(force: true) }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Refresh", systemImage: "arrow.clockwise") {
                        Task { await model.refreshUsage(force: true) }
                    }
                    .disabled(model.isRefreshingUsage)
                }
            }
            .task {
                if model.usage == nil { await model.refreshUsage() }
            }
        }
    }
}

private struct ProviderCard: View {
    let provider: UsageProvider

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(spacing: 12) {
                Text(monogram)
                    .font(.caption.bold())
                    .frame(width: 38, height: 38)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                VStack(alignment: .leading, spacing: 2) {
                    Text(provider.name).font(.headline)
                    Text([provider.plan, statusLabel].compactMap { $0 }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Text(percent(provider.headroomPercent))
                    .font(.title3.bold().monospacedDigit())
                    .foregroundStyle(tone(provider.headroomPercent))
            }

            if let credits = provider.rateLimitResets {
                Label("\(credits.availableCount) reset credit\(credits.availableCount == 1 ? "" : "s") available",
                      systemImage: "arrow.counterclockwise.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            ForEach(provider.windows) { window in
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(window.label).font(.subheadline.weight(.semibold))
                            Text(window.scope).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Text(percent(window.remainingPercent))
                            .font(.subheadline.bold().monospacedDigit())
                    }
                    ProgressView(value: max(0, min(100, window.remainingPercent)), total: 100)
                        .tint(tone(window.remainingPercent))
                    HStack {
                        if let reset = resetText(window.resetsAt) { Text(reset) }
                        Spacer()
                        if let burn = window.burnRatePercentPerHour {
                            Text("−\(percent(burn))/hr")
                        }
                    }
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }
            }

            if provider.windows.isEmpty {
                Text(provider.message ?? "No usage snapshot is available.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }

            if let source = provider.source, !source.isEmpty {
                Text("via \(source)").font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding()
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18))
    }

    private var monogram: String {
        let pieces = provider.name.split(separator: " ")
        if pieces.count > 1 { return pieces.prefix(2).compactMap(\.first).map(String.init).joined() }
        return String(provider.name.prefix(2)).uppercased()
    }

    private var statusLabel: String? {
        switch provider.status {
        case "ok": "live"
        case "stale": "cached"
        case "auth_required": "login needed"
        case "not_configured": "setup needed"
        default: provider.status
        }
    }

    private func percent(_ value: Double?) -> String {
        guard let value else { return "—" }
        return value.rounded() == value ? "\(Int(value))%" : String(format: "%.1f%%", value)
    }

    private func tone(_ value: Double?) -> Color {
        guard let value else { return .secondary }
        if value <= 15 { return .red }
        if value <= 35 { return .orange }
        return .green
    }

    private func resetText(_ value: String?) -> String? {
        guard let date = OrchestraFormatting.date(from: value) else { return nil }
        let seconds = max(0, Int(date.timeIntervalSinceNow))
        return seconds == 0 ? "resetting now" : "resets in \(OrchestraFormatting.duration(seconds))"
    }
}
