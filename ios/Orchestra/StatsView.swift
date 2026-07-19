import SwiftUI

struct StatsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationStack {
            Group {
                if let stats = model.stats {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                                MetricCard(title: "Worker time",
                                           value: OrchestraFormatting.duration(stats.totalSeconds),
                                           systemImage: "clock.arrow.circlepath")
                                MetricCard(title: "Total runs", value: "\(stats.totalRuns)",
                                           systemImage: "number")
                                MetricCard(title: "Active now", value: "\(stats.activeRuns)",
                                           systemImage: "waveform", tint: .green)
                                MetricCard(title: "Timed runs", value: "\(stats.timedRuns)",
                                           systemImage: "stopwatch")
                            }

                            runtimeSection("By worker", icon: "person.2") {
                                ForEach(stats.byAgent) { agent in
                                    RuntimeRow(title: agent.agent,
                                               subtitle: [agent.role, agent.models.joined(separator: ", ")]
                                                .filter { !$0.isEmpty }.joined(separator: " · "),
                                               seconds: agent.seconds,
                                               runs: agent.runs,
                                               activeRuns: agent.activeRuns,
                                               totalSeconds: stats.totalSeconds)
                                }
                            }

                            runtimeSection("By model", icon: "cpu") {
                                ForEach(stats.byModel) { item in
                                    RuntimeRow(title: item.model,
                                               subtitle: "\(item.backend) · \(item.agents.joined(separator: ", "))",
                                               seconds: item.seconds,
                                               runs: item.runs,
                                               activeRuns: item.activeRuns,
                                               totalSeconds: stats.totalSeconds)
                                }
                            }
                        }
                        .padding()
                    }
                } else {
                    ProgressView("Calculating runtime…")
                }
            }
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Stats")
            .refreshable { await model.refreshStats() }
            .toolbar { ProjectToolbarMenu() }
            .task { if model.stats == nil { await model.refreshStats() } }
        }
    }

    private func runtimeSection<Content: View>(_ title: String, icon: String,
                                               @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: icon).font(.headline)
            VStack(spacing: 0) { content() }
                .background(Color(.secondarySystemGroupedBackground),
                            in: RoundedRectangle(cornerRadius: 16))
        }
    }
}

private struct RuntimeRow: View {
    let title: String
    let subtitle: String
    let seconds: Int
    let runs: Int
    let activeRuns: Int
    let totalSeconds: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.subheadline.weight(.semibold)).lineLimit(1)
                    if !subtitle.isEmpty {
                        Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(OrchestraFormatting.duration(seconds)).font(.subheadline.monospacedDigit())
                    Text("\(runs) run\(runs == 1 ? "" : "s")\(activeRuns > 0 ? " · \(activeRuns) active" : "")")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            }
            ProgressView(value: totalSeconds == 0 ? 0 : Double(seconds) / Double(totalSeconds))
        }
        .padding()
    }
}
