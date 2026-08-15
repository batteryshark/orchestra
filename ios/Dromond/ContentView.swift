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
            try state.saveSettings(serverURL: serverURL, key: key)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var serverURL = ""
    @State private var key = ""
    @State private var error: String?
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            Form {
                Section("Connection") {
                    TextField("http://mac.tailnet:3011/", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .focused($focused)
                    SecureField("Shared key", text: $key)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($focused)
                }
                if let error { Text(error).foregroundStyle(.red) }
            }
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { focused = false }
                }
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        do {
                            try state.saveSettings(serverURL: serverURL, key: key)
                            dismiss()
                            Task { await state.refresh() }
                        } catch {
                            self.error = error.localizedDescription
                        }
                    }
                }
            }
            .onAppear {
                serverURL = state.serverURL
                key = state.key
            }
        }
    }
}
