import SwiftUI

@main
struct OrchestraApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .task {
                    guard !model.serverURL.isEmpty else { return }
                    await model.connect()
                }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                model.startPolling()
                Task { await model.refreshState() }
            default:
                model.stopPolling()
            }
        }
    }
}

private struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if model.dashboard != nil, model.directory != nil {
            DashboardTabs()
        } else {
            switch model.connectionState {
            case .connected:
                DashboardTabs()
            case .connecting:
                ConnectionView(isConnecting: true)
            case .disconnected, .failed:
                ConnectionView(isConnecting: false)
            }
        }
    }
}

private struct ConnectionView: View {
    @EnvironmentObject private var model: AppModel
    let isConnecting: Bool
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 24) {
                    OrchestraMark()
                        .frame(width: 88, height: 88)
                        .shadow(color: .black.opacity(0.18), radius: 18, y: 8)

                    VStack(spacing: 8) {
                        Text("Orchestra")
                            .font(.largeTitle.bold())
                        Text("Watch your workers from anywhere on your tailnet.")
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }

                    VStack(alignment: .leading, spacing: 10) {
                        Text("Server URL")
                            .font(.headline)
                        TextField("http://your-mac:4764", text: $model.serverURL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .keyboardType(.URL)
                            .textContentType(.URL)
                            .submitLabel(.go)
                            .focused($focused)
                            .padding(.horizontal, 14)
                            .frame(minHeight: 54)
                            .background(.background, in: RoundedRectangle(cornerRadius: 12))
                            .overlay {
                                RoundedRectangle(cornerRadius: 12)
                                    .stroke(focused ? Color.accentColor : Color.secondary.opacity(0.28),
                                            lineWidth: focused ? 2 : 1)
                            }
                            .contentShape(RoundedRectangle(cornerRadius: 12))
                            .simultaneousGesture(TapGesture().onEnded { focused = true })
                            .onSubmit { connect() }

                        Text("Start Orchestra with `orchestra ui --tailscale`, then enter the URL it prints.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: 460)

                    if case let .failed(message) = model.connectionState {
                        Label(message, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .frame(maxWidth: 460, alignment: .leading)
                    }

                    Button {
                        connect()
                    } label: {
                        HStack {
                            if isConnecting { ProgressView().tint(.white) }
                            Text(isConnecting ? "Connecting…" : "Connect")
                        }
                        .frame(maxWidth: 460)
                        .padding(.vertical, 7)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(isConnecting || model.serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                .frame(maxWidth: .infinity)
                .padding(.horizontal, 24)
                .padding(.vertical, 54)
            }
            .background(Color(.systemGroupedBackground))
        }
    }

    private func connect() {
        guard !isConnecting,
              !model.serverURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        focused = false
        Task { await model.connect() }
    }
}

private struct DashboardTabs: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        TabView {
            ProjectsView()
                .tabItem { Label("Projects", systemImage: "square.grid.2x2") }

            WorkersView()
                .tabItem { Label("Workers", systemImage: "person.3") }
                .badge(model.waitingCount)

            UpdatesView()
                .tabItem { Label("Updates", systemImage: "tray.full") }
                .badge(model.unreadCount)

            UsageView()
                .tabItem { Label("Usage", systemImage: "gauge.with.dots.needle.50percent") }

            StatsView()
                .tabItem { Label("Stats", systemImage: "chart.bar.xaxis") }
        }
    }
}

struct OrchestraMark: View {
    var body: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            ZStack {
                RoundedRectangle(cornerRadius: width * 0.19)
                    .fill(Color(red: 0.043, green: 0.051, blue: 0.063))
                HStack(alignment: .bottom, spacing: width * 0.07) {
                    bar(height: 0.33, opacity: 0.45)
                    bar(height: 0.53, opacity: 1)
                    bar(height: 0.40, opacity: 0.70)
                }
                .padding(.bottom, width * 0.24)
            }
        }
        .aspectRatio(1, contentMode: .fit)
    }

    private func bar(height: CGFloat, opacity: Double) -> some View {
        GeometryReader { proxy in
            Capsule()
                .fill(.white.opacity(opacity))
                .frame(width: proxy.size.width, height: proxy.size.width / 0.22 * height)
                .frame(maxHeight: .infinity, alignment: .bottom)
        }
        .frame(maxWidth: .infinity)
    }
}
