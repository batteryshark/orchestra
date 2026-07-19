import SwiftUI

struct ProjectsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showConnectionSettings = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    serverCard
                }

                Section("Projects") {
                    ForEach(model.directory?.projects ?? []) { project in
                        Button {
                            Task { await model.selectProject(project) }
                        } label: {
                            ProjectRow(project: project,
                                       isSelected: project.id == model.selectedProjectID,
                                       isDefault: project.id == model.directory?.defaultProjectId)
                        }
                        .buttonStyle(.plain)
                        .disabled(!project.isAvailable)
                    }
                }

                if let dashboard = model.dashboard {
                    Section("Current project") {
                        LabeledContent("Active workers",
                                       value: "\(dashboard.runs.filter { !$0.isTerminal }.count)")
                        LabeledContent("Waiting for input",
                                       value: "\(dashboard.runs.filter(\.isWaiting).count)")
                        LabeledContent("Unread messages",
                                       value: "\(dashboard.messages.filter(\.isUnread).count)")
                        LabeledContent("Project root", value: dashboard.root)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Projects")
            .refreshable {
                await model.refreshProjects()
                await model.refreshState()
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Connection", systemImage: "gearshape") {
                        showConnectionSettings = true
                    }
                }
            }
            .sheet(isPresented: $showConnectionSettings) {
                ConnectionSettingsView()
            }
        }
    }

    private var serverCard: some View {
        HStack(spacing: 12) {
            OrchestraMark()
                .frame(width: 44, height: 44)
            VStack(alignment: .leading, spacing: 3) {
                Text("Orchestra instance")
                    .font(.headline)
                Text(model.serverURL)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .accessibilityLabel("Connected")
        }
        .padding(.vertical, 4)
    }
}

private struct ProjectRow: View {
    let project: OrchestraProject
    let isSelected: Bool
    let isDefault: Bool

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: isSelected ? "folder.fill" : "folder")
                .foregroundStyle(project.isAvailable ? Color.accentColor : .secondary)
                .font(.title3)
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(project.name)
                        .font(.body.weight(isSelected ? .semibold : .regular))
                    if isDefault {
                        Text("default")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                Text(project.root)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            if !project.isAvailable {
                Text("Offline").font(.caption).foregroundStyle(.secondary)
            } else if isSelected {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
        }
        .contentShape(Rectangle())
        .padding(.vertical, 3)
    }
}

private struct ConnectionSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var editedURL = ""
    @FocusState private var urlFieldFocused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://your-mac:4764", text: $editedURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .submitLabel(.go)
                        .focused($urlFieldFocused)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                        .simultaneousGesture(TapGesture().onEnded { urlFieldFocused = true })
                        .onSubmit { reconnect() }
                } header: {
                    Text("Server")
                } footer: {
                    Text("Changing the URL disconnects from the current instance and loads the new instance's project registry.")
                }

                Section {
                    Button("Reconnect") {
                        reconnect()
                    }
                    .disabled(editedURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                    Button("Disconnect", role: .destructive) {
                        model.disconnect()
                        dismiss()
                    }
                }
            }
            .navigationTitle("Connection")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .onAppear { editedURL = model.serverURL }
        }
    }

    private func reconnect() {
        guard !editedURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        urlFieldFocused = false
        model.serverURL = editedURL
        Task {
            await model.connect()
            if case .connected = model.connectionState { dismiss() }
        }
    }
}
