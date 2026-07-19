import SwiftUI

private enum UpdatesSection: String, CaseIterable, Identifiable {
    case inbox = "Inbox"
    case findings = "Findings"
    var id: String { rawValue }
}

struct UpdatesView: View {
    @EnvironmentObject private var model: AppModel
    @State private var section: UpdatesSection = .inbox
    @State private var searchText = ""

    var body: some View {
        NavigationStack {
            Group {
                if model.dashboard == nil {
                    ProgressView("Loading updates…")
                } else if section == .inbox {
                    inboxList
                } else {
                    findingsList
                }
            }
            .navigationTitle("Updates")
            .safeAreaInset(edge: .top, spacing: 0) {
                Picker("Update type", selection: $section) {
                    ForEach(UpdatesSection.allCases) { item in
                        Text(item.rawValue).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal)
                .padding(.vertical, 8)
                .background(.bar)
            }
            .searchable(text: $searchText, prompt: section == .inbox ? "Search messages" : "Search findings")
            .refreshable { await model.refreshState() }
            .toolbar { ProjectToolbarMenu() }
        }
    }

    @ViewBuilder
    private var inboxList: some View {
        if filteredMessages.isEmpty {
            EmptyStateView(icon: "tray", title: "Inbox is clear",
                           message: "Messages sent between workers will appear here.")
        } else {
            List {
                ForEach(groupedMessages, id: \.key) { recipient, messages in
                    Section {
                        ForEach(messages) { message in
                            MessageRow(message: message)
                        }
                    } header: {
                        HStack {
                            Text(recipient)
                            let unread = messages.filter(\.isUnread).count
                            if unread > 0 { Text("\(unread) unread").foregroundStyle(.blue) }
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
        }
    }

    @ViewBuilder
    private var findingsList: some View {
        if filteredFindings.isEmpty {
            EmptyStateView(icon: "sparkles", title: "No findings",
                           message: "Shared worker findings will appear here.")
        } else {
            List(filteredFindings) { finding in
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Text(finding.author).font(.subheadline.weight(.semibold))
                        Spacer()
                        Text(OrchestraFormatting.timestamp(finding.createdAt))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    WrappedText(text: finding.body)
                    HStack(spacing: 8) {
                        if let tags = finding.tags, !tags.isEmpty {
                            Label(tags, systemImage: "tag")
                        }
                        if let runID = finding.runId {
                            Text("run #\(runID)")
                        }
                        if let work = finding.workItem { Text(work) }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
            }
            .listStyle(.insetGrouped)
        }
    }

    private var filteredMessages: [InboxMessage] {
        (model.dashboard?.messages ?? []).reversed().filter { message in
            searchText.isEmpty || [message.sender, message.recipient, message.body]
                .contains { $0.localizedCaseInsensitiveContains(searchText) }
        }
    }

    private var groupedMessages: [(key: String, value: [InboxMessage])] {
        Dictionary(grouping: filteredMessages, by: \.recipient)
            .sorted { left, right in
                let leftUnread = left.value.filter(\.isUnread).count
                let rightUnread = right.value.filter(\.isUnread).count
                return leftUnread == rightUnread ? left.key < right.key : leftUnread > rightUnread
            }
    }

    private var filteredFindings: [Finding] {
        (model.dashboard?.feed ?? []).filter { finding in
            searchText.isEmpty || [finding.author, finding.body, finding.tags ?? ""]
                .contains { $0.localizedCaseInsensitiveContains(searchText) }
        }
    }
}

private struct MessageRow: View {
    let message: InboxMessage

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                if message.isUnread {
                    Circle().fill(.blue).frame(width: 7, height: 7)
                }
                Text("\(message.sender) → \(message.recipient)")
                    .font(.subheadline.weight(message.isUnread ? .semibold : .regular))
                Spacer()
                Text(OrchestraFormatting.timestamp(message.createdAt))
                    .font(.caption).foregroundStyle(.secondary)
            }
            WrappedText(text: message.body)
            HStack(spacing: 8) {
                if let runID = message.runId { Text("run #\(runID)") }
                if let work = message.workItem { Text(work) }
                if let kind = message.kind, !kind.isEmpty { Text(kind) }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }
}
