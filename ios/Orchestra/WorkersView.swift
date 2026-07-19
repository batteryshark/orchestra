import SwiftUI

enum WorkerFilter: String, CaseIterable, Identifiable {
    case active = "Active"
    case waiting = "Waiting"
    case all = "All"

    var id: String { rawValue }
}

struct WorkersView: View {
    @EnvironmentObject private var model: AppModel
    @State private var filter: WorkerFilter = .active
    @State private var searchText = ""

    var body: some View {
        NavigationStack {
            Group {
                if model.dashboard == nil {
                    ProgressView("Loading workers…")
                } else if filteredRuns.isEmpty && filteredMembers.isEmpty {
                    EmptyStateView(icon: "person.3", title: "No workers",
                                   message: emptyMessage)
                } else {
                    List {
                        if !filteredRuns.isEmpty {
                            Section("Runs") {
                                ForEach(filteredRuns) { run in
                                    NavigationLink(value: run) {
                                        RunRow(run: run)
                                    }
                                }
                            }
                        }

                        ForEach(filteredTeams) { team in
                            Section("\(team.name) · Ensemble") {
                                ForEach(team.members.filter(memberMatches)) { member in
                                    NavigationLink {
                                        TeammateDetailView(team: team, member: member)
                                    } label: {
                                        EnsembleMemberRow(member: member)
                                    }
                                }
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle("Workers")
            .navigationDestination(for: Run.self) { run in
                RunDetailView(initialRun: run)
            }
            .safeAreaInset(edge: .top, spacing: 0) {
                VStack(spacing: 8) {
                    ConnectionBanner()
                    Picker("Worker filter", selection: $filter) {
                        ForEach(WorkerFilter.allCases) { item in
                            Text(item.rawValue).tag(item)
                        }
                    }
                    .pickerStyle(.segmented)
                    .padding(.horizontal)
                    .padding(.bottom, 8)
                }
                .background(.bar)
            }
            .searchable(text: $searchText, prompt: "Worker, model, or task")
            .refreshable { await model.refreshState() }
            .toolbar { ProjectToolbarMenu() }
        }
        .id(model.selectedProjectID)
    }

    private var filteredRuns: [Run] {
        let runs = model.dashboard?.runs.reversed() ?? [].reversed()
        return runs.filter { run in
            switch filter {
            case .active: !run.isTerminal
            case .waiting: run.isWaiting
            case .all: true
            }
        }.filter { run in
            searchText.isEmpty || [run.agent, run.model ?? "", run.title ?? "", run.slug ?? ""]
                .contains { $0.localizedCaseInsensitiveContains(searchText) }
        }
    }

    private var filteredTeams: [EnsembleTeam] {
        model.dashboard?.ensemble.filter { team in
            team.members.contains(where: memberMatches)
        } ?? []
    }

    private var filteredMembers: [EnsembleMember] {
        filteredTeams.flatMap(\.members).filter(memberMatches)
    }

    private func memberMatches(_ member: EnsembleMember) -> Bool {
        let statusMatches: Bool
        switch filter {
        case .active: statusMatches = member.status == "busy" || member.status == "ready"
        case .waiting: statusMatches = member.status == "ready"
        case .all: statusMatches = true
        }
        return statusMatches && (searchText.isEmpty || [member.name, member.model ?? ""]
            .contains { $0.localizedCaseInsensitiveContains(searchText) })
    }

    private var emptyMessage: String {
        switch filter {
        case .active: "No workers are currently active in this project."
        case .waiting: "No workers are waiting for input."
        case .all: "This project has no runs yet."
        }
    }
}

private struct RunRow: View {
    let run: Run

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            ZStack {
                Circle().fill(run.statusColor.opacity(0.14))
                Image(systemName: run.isTerminal ? "checkmark" : run.isWaiting ? "questionmark" : "waveform")
                    .foregroundStyle(run.statusColor)
                    .font(.caption.bold())
            }
            .frame(width: 34, height: 34)

            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Text("#\(run.id)")
                        .font(.caption.monospaced().weight(.semibold))
                    Text(run.displayName)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    StatusChip(status: run.status, color: run.statusColor)
                }
                Text(run.title?.isEmpty == false ? run.title! : "Untitled run")
                    .lineLimit(2)
                HStack(spacing: 6) {
                    Label(run.agent, systemImage: "person")
                    Text("·")
                    Text(run.modelDisplayName)
                    Spacer()
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text(OrchestraFormatting.elapsed(from: run.startedAt,
                                                         to: run.finishedAt,
                                                         now: context.date))
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct EnsembleMemberRow: View {
    let member: EnsembleMember

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "person.crop.circle.badge.clock")
                .font(.title2)
                .foregroundStyle(member.status == "busy" ? .blue : .green)
            VStack(alignment: .leading, spacing: 3) {
                Text(member.name).font(.headline)
                Text(member.model?.split(separator: "/").last.map(String.init) ?? "Ensemble worker")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            StatusChip(status: member.executionStatus ?? member.status,
                       color: member.status == "busy" ? .blue : .green)
        }
        .padding(.vertical, 3)
    }
}

struct TeammateDetailView: View {
    @EnvironmentObject private var model: AppModel
    let team: EnsembleTeam
    let member: EnsembleMember
    @State private var etag: String?
    @State private var items: [TranscriptItem] = []
    @State private var messages: [EnsembleMessage] = []
    @State private var errorMessage: String?

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        StatusChip(status: member.executionStatus ?? member.status,
                                   color: member.status == "busy" ? .blue : .green)
                        Text(team.name).foregroundStyle(.secondary)
                    }
                    Text(member.model ?? "Model unavailable")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
                .background(Color(.secondarySystemGroupedBackground),
                            in: RoundedRectangle(cornerRadius: 16))

                if !team.tasks.isEmpty {
                    DisclosureGroup("Team board") {
                        VStack(alignment: .leading, spacing: 9) {
                            ForEach(Array(team.tasks.enumerated()), id: \.offset) { _, task in
                                HStack(alignment: .top) {
                                    Image(systemName: task.status == "completed" ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(task.status == "completed" ? .green : .secondary)
                                    VStack(alignment: .leading) {
                                        Text(task.content)
                                        if let assignee = task.assignee {
                                            Text(assignee).font(.caption).foregroundStyle(.secondary)
                                        }
                                    }
                                }
                            }
                        }
                        .padding(.top, 8)
                    }
                    .padding()
                    .background(Color(.secondarySystemGroupedBackground),
                                in: RoundedRectangle(cornerRadius: 16))
                }

                if !messages.isEmpty {
                    DisclosureGroup("Team messages (\(messages.count))") {
                        VStack(spacing: 10) {
                            ForEach(Array(messages.enumerated()), id: \.offset) { _, message in
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("\(message.fromName) → \(message.toName)")
                                        .font(.caption).foregroundStyle(.secondary)
                                    WrappedText(text: message.content)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                        .padding(.top, 8)
                    }
                    .padding()
                    .background(Color(.secondarySystemGroupedBackground),
                                in: RoundedRectangle(cornerRadius: 16))
                }

                if let errorMessage {
                    Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                }

                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    TranscriptItemView(item: item, index: index)
                }
            }
            .padding()
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle(member.name)
        .navigationBarTitleDisplayMode(.inline)
        .task { await pollTranscript() }
    }

    private func pollTranscript() async {
        while !Task.isCancelled {
            do {
                let response = try await model.teammateTranscript(sessionID: member.sessionId,
                                                                  teamID: team.id,
                                                                  etag: etag)
                if response.unchanged != true {
                    etag = response.etag
                    items = response.items ?? []
                    messages = response.messages ?? []
                }
                errorMessage = nil
            } catch is CancellationError {
                return
            } catch {
                errorMessage = error.localizedDescription
            }
            try? await Task.sleep(for: .seconds(2))
        }
    }
}
