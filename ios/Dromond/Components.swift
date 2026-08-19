import SwiftUI

/// The fleet mark, drawn rather than shipped as an asset so it scales and
/// tints anywhere. Three hulls in echelon, the middle one leading.
struct DromondMark: View {
    var tile = true

    private static let teal = Color(red: 0.310, green: 0.702, blue: 0.769)
    private static let blue = Color(red: 0.380, green: 0.647, blue: 0.910)
    private static let ink = Color(red: 0.086, green: 0.106, blue: 0.137)

    var body: some View {
        GeometryReader { proxy in
            let side = min(proxy.size.width, proxy.size.height)
            let s = side / 512
            ZStack(alignment: .topLeading) {
                if tile {
                    RoundedRectangle(cornerRadius: 112 * s).fill(Self.ink)
                }
                hull(s, x: 78, y: 96, fill: Self.teal)
                hull(s, x: 118, y: 214, fill: Self.blue)
                hull(s, x: 78, y: 332, fill: Self.teal)
            }
            .frame(width: side, height: side)
        }
        .aspectRatio(1, contentMode: .fit)
        .accessibilityLabel("Dromond")
    }

    private func hull(_ s: CGFloat, x: CGFloat, y: CGFloat, fill: Color) -> some View {
        HullShape()
            .fill(fill)
            .frame(width: 296 * s, height: 92 * s)
            .offset(x: x * s, y: y * s)
    }
}

/// One hull: belly aft, long raking bow. A single filled silhouette, so
/// direction survives downscaling to a favicon.
struct HullShape: Shape {
    func path(in rect: CGRect) -> Path {
        // The source outline is 296 wide by 92 tall; everything below is a
        // fraction of that, so the hull fits whatever frame it is handed.
        let w = rect.width / 296, h = rect.height / 92
        var p = Path()
        p.move(to: CGPoint(x: 0, y: 10 * h))
        p.addLine(to: CGPoint(x: 296 * w, y: 0))
        p.addCurve(to: CGPoint(x: 100 * w, y: 82 * h),
                   control1: CGPoint(x: 238 * w, y: 40 * h),
                   control2: CGPoint(x: 176 * w, y: 74 * h))
        p.addCurve(to: CGPoint(x: 0, y: 10 * h),
                   control1: CGPoint(x: 58 * w, y: 86 * h),
                   control2: CGPoint(x: 16 * w, y: 54 * h))
        p.closeSubpath()
        return p
    }
}

/// A run's state, in the one colour the whole app agrees on.
struct StatusChip: View {
    let status: String

    var body: some View {
        Text(status.replacingOccurrences(of: "_", with: " "))
            .font(.caption2.weight(.bold))
            .foregroundStyle(Self.color(status))
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Self.color(status).opacity(0.14), in: Capsule())
            .accessibilityLabel("Status: \(status)")
    }

    static func color(_ status: String) -> Color {
        switch status {
        case "done": .green
        case "running", "spawning": .blue
        case "failed", "timeout": .red
        case "killed": .orange
        case "blocked", "waiting": .purple
        default: .secondary
        }
    }
}

struct MetricCard: View {
    let title: String
    let value: String
    let systemImage: String
    var tint: Color = .accentColor

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Image(systemName: systemImage).foregroundStyle(tint).font(.title3)
            Text(value).font(.title2.bold()).contentTransition(.numericText())
            Text(title).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
    }
}

/// The project picker every tab carries, so switching context never means
/// finding the one screen that owns it.
struct ProjectToolbarMenu: ToolbarContent {
    @EnvironmentObject private var state: AppState

    var body: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Button {
                    state.selectedProjectID = nil
                } label: {
                    if state.selectedProjectID == nil {
                        Label("All projects", systemImage: "checkmark")
                    } else {
                        Text("All projects")
                    }
                }
                Divider()
                ForEach(state.projects) { project in
                    Button {
                        state.selectedProjectID = project.projectID
                    } label: {
                        if project.projectID == state.selectedProjectID {
                            Label(project.name, systemImage: "checkmark")
                        } else {
                            Text("\(project.name) · \(project.runs)")
                        }
                    }
                }
            } label: {
                HStack(spacing: 5) {
                    Circle()
                        .fill(state.error == nil ? Color.green : Color.orange)
                        .frame(width: 7, height: 7)
                    Text(state.selectedProject?.name ?? "All projects").lineLimit(1)
                    Image(systemName: "chevron.up.chevron.down").font(.caption2)
                }
            }
        }
    }
}

/// Which daemon every screen is reading. Sits beside the project picker rather
/// than inside Settings: switching machines is a thing done while looking at
/// runs, not a thing done while configuring. Hidden entirely with one server,
/// because a picker over a list of one is furniture.
struct ServerToolbarMenu: ToolbarContent {
    @EnvironmentObject private var state: AppState
    @State private var settings = false

    var body: some ToolbarContent {
        ToolbarItem(placement: .topBarLeading) {
            Menu {
                if state.servers.count > 1 {
                    ForEach(state.servers) { server in
                        Button {
                            state.select(server.id)
                        } label: {
                            if server.id == state.selectedServer?.id {
                                Label(server.displayName, systemImage: "checkmark")
                            } else {
                                Text(server.displayName)
                            }
                        }
                    }
                    Divider()
                }
                Button("Servers…", systemImage: "gear") { settings = true }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: "server.rack").font(.caption)
                    if state.servers.count > 1 {
                        Text(state.selectedServer?.displayName ?? "").lineLimit(1)
                        Image(systemName: "chevron.up.chevron.down").font(.caption2)
                    }
                }
            }
            .sheet(isPresented: $settings) { SettingsView() }
        }
    }
}

/// Shown when the last refresh failed. The data on screen is still the last
/// good snapshot, so this says the connection broke — not that anything is
/// wrong with what is displayed.
struct ConnectionBanner: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        if let error = state.error {
            HStack(spacing: 10) {
                Image(systemName: "wifi.exclamationmark").foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Connection interrupted").font(.subheadline.weight(.semibold))
                    Text(error).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                }
                Spacer()
                Button("Retry") { Task { await state.refresh() } }.buttonStyle(.bordered)
            }
            .padding(12)
            .background(.orange.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
            .padding(.horizontal)
        }
    }
}

/// Selectable, wrapping, left-aligned. Used wherever the app shows something
/// the owner will want to copy: a branch, a commit, a path, a summary.
struct WrappedText: View {
    let text: String
    var font: Font = .body
    var color: Color = .primary

    var body: some View {
        Text(text)
            .font(font)
            .foregroundStyle(color)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension Double {
    /// "4m 12s" — durations read as durations, never as 252.0.
    var durationLabel: String {
        let total = Int(self)
        if total < 60 { return "\(total)s" }
        if total < 3600 { return "\(total / 60)m \(total % 60)s" }
        return "\(total / 3600)h \((total % 3600) / 60)m"
    }
}

extension Int {
    /// "12,480" — token counts are read, not computed with.
    var grouped: String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        return f.string(from: NSNumber(value: self)) ?? "\(self)"
    }
}

extension Array {
    /// Bounds-checked read. Lives here rather than beside one view because
    /// every file that parses launch arguments wants it.
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
