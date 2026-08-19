import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        if state.isConfigured {
            DashboardTabs()
                .task(id: state.isConfigured) {
                    while !Task.isCancelled {
                        await state.refresh()
                        do {
                            try await Task.sleep(for: .seconds(4))
                        } catch {
                            return
                        }
                    }
                }
        } else {
            ConnectView()
        }
    }
}

/// Five tabs, one concern each — the same division the web dashboard makes,
/// because a person moving between the two should not have to relearn where
/// anything lives.
private struct DashboardTabs: View {
    @EnvironmentObject private var state: AppState
    @State private var tab: Tab = .init(argument: ProcessInfo.processInfo.arguments)

    /// A tab can be opened directly with `-startTab runway` as a launch
    /// argument. That exists so a screenshot of any tab needs no taps —
    /// `simctl launch` reaches it — which is the difference between a screen
    /// that can be verified headlessly and one that needs a granted device.
    enum Tab: Hashable {
        case runs, findings, runway, profiles, health

        init(argument: [String]) {
            guard let at = argument.firstIndex(of: "-startTab"),
                  let name = argument[safe: at + 1] else { self = .runs; return }
            switch name {
            case "findings": self = .findings
            case "runway": self = .runway
            case "profiles": self = .profiles
            case "health": self = .health
            default: self = .runs
            }
        }
    }

    var body: some View {
        TabView(selection: $tab) {
            RunsView()
                .tabItem { Label("Runs", systemImage: "list.bullet.rectangle") }
                .tag(Tab.runs)
                .badge(state.liveRuns.count)

            FindingsView()
                .tabItem { Label("Findings", systemImage: "tray.full") }
                .tag(Tab.findings)
                .badge(state.attentionCount)

            RunwayView()
                .tabItem { Label("Runway", systemImage: "gauge.with.dots.needle.50percent") }
                .tag(Tab.runway)

            ProfilesView()
                .tabItem { Label("Profiles", systemImage: "person.3") }
                .tag(Tab.profiles)

            HealthView()
                .tabItem { Label("Health", systemImage: "heart.text.square") }
                .tag(Tab.health)
        }
    }
}

/// The whole of first run: two fields and a button, on the page.
struct ConnectView: View {
    @EnvironmentObject private var state: AppState
    @FocusState private var focused: Field?
    @State private var serverURL = ""
    @State private var key = ""
    @State private var error: String?

    private enum Field { case url, key }

    private var canConnect: Bool {
        !serverURL.trimmingCharacters(in: .whitespaces).isEmpty
            && !key.trimmingCharacters(in: .whitespaces).isEmpty
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    DromondMark()
                        .frame(width: 76, height: 76)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                        .listRowBackground(Color.clear)
                }

                Section {
                    TextField("http://mac.tailnet:3011/", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .focused($focused, equals: .url)
                        .submitLabel(.next)
                        .onSubmit { focused = .key }
                    SecureField("Shared key", text: $key)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textContentType(.password)
                        .focused($focused, equals: .key)
                        .submitLabel(.go)
                        .onSubmit(connect)
                } header: {
                    Text("Connection")
                } footer: {
                    Text("The daemon prints its URL and key at startup: `dromond doctor`.")
                }

                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }

                Section {
                    Button("Connect", action: connect).disabled(!canConnect)
                }
            }
            .navigationTitle("Dromond")
            .toolbar {
                // Without this the keyboard has no exit on a field that submits.
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { focused = nil }
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .onAppear {
                serverURL = state.serverURL
                key = state.key
            }
        }
    }

    private func connect() {
        focused = nil
        do {
            try state.saveServer(id: nil, label: "", url: serverURL, key: key)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var editing: Server?
    @State private var adding = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(state.servers) { server in
                        Button {
                            state.select(server.id)
                            dismiss()
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(server.displayName)
                                        .font(.body.weight(.medium))
                                        .foregroundStyle(.primary)
                                    Text(server.url)
                                        .font(.caption.monospaced())
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if server.id == state.selectedServer?.id {
                                    Image(systemName: "checkmark")
                                        .foregroundStyle(.tint)
                                }
                            }
                        }
                        .swipeActions {
                            Button("Remove", role: .destructive) {
                                state.removeServer(server)
                            }
                            Button("Edit") { editing = server }.tint(.blue)
                        }
                    }
                } header: {
                    Text("Servers")
                } footer: {
                    Text("One Dromond daemon each. Switching changes which "
                         + "machine every screen is reading.")
                }

                Section {
                    Button("Add a server") { adding = true }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .sheet(isPresented: $adding) { ServerEditor(server: nil) }
            .sheet(item: $editing) { ServerEditor(server: $0) }
        }
    }
}

/// Add or change one server. The key field is empty when editing — an existing
/// secret is never read back onto the screen, and leaving it empty keeps it.
struct ServerEditor: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let server: Server?

    @State private var label = ""
    @State private var url = ""
    @State private var key = ""
    @State private var error: String?
    @FocusState private var focused: Bool

    private var canSave: Bool {
        !url.trimmingCharacters(in: .whitespaces).isEmpty
            && (server != nil || !key.trimmingCharacters(in: .whitespaces).isEmpty)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("mac, windows box, …", text: $label)
                        .focused($focused)
                    TextField("http://host:3011/", text: $url)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .focused($focused)
                    SecureField(server == nil ? "Shared key"
                                              : "Shared key (unchanged if blank)",
                                text: $key)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .textContentType(.password)
                        .focused($focused)
                } header: {
                    Text("Server")
                } footer: {
                    Text("A name is optional; the host is used when it is blank.")
                }
                if let error {
                    Section { Text(error).foregroundStyle(.red) }
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle(server == nil ? "Add a server" : "Edit server")
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { focused = false }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: save).disabled(!canSave)
                }
            }
            .onAppear {
                label = server?.label ?? ""
                url = server?.url ?? ""
            }
        }
    }

    private func save() {
        focused = false
        do {
            // Editing with a blank key keeps the stored one rather than
            // wiping a secret the owner never meant to touch.
            let existing = server.map { Keychain.load(for: $0.keyAccount) } ?? ""
            let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
            try state.saveServer(id: server?.id, label: label, url: url,
                                 key: trimmed.isEmpty ? existing : trimmed)
            dismiss()
        } catch {
            self.error = error.localizedDescription
        }
    }
}
