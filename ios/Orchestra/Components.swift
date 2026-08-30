import SwiftUI

struct OrchestraMark: View {
    var tile = true

    private static let bar = Color(red: 0.953, green: 0.957, blue: 0.949)
    private static let ink = Color(red: 0.043, green: 0.051, blue: 0.063)

    var body: some View {
        GeometryReader { proxy in
            let side = min(proxy.size.width, proxy.size.height)
            let scale = side / 128
            ZStack(alignment: .topLeading) {
                if tile {
                    RoundedRectangle(cornerRadius: 24 * scale).fill(Self.ink)
                }
                bar(scale, x: 25, y: 55, height: 42, opacity: 0.45)
                bar(scale, x: 54, y: 30, height: 67, opacity: 1)
                bar(scale, x: 83, y: 46, height: 51, opacity: 0.7)
            }
            .frame(width: side, height: side)
        }
        .aspectRatio(1, contentMode: .fit)
        .accessibilityLabel("Orchestra")
    }

    private func bar(_ scale: CGFloat, x: CGFloat, y: CGFloat,
                     height: CGFloat, opacity: Double) -> some View {
        RoundedRectangle(cornerRadius: 10 * scale)
            .fill(Self.bar.opacity(opacity))
            .frame(width: 20 * scale, height: height * scale)
            .offset(x: x * scale, y: y * scale)
    }
}

struct StatusChip: View {
    let status: String

    private var color: Color {
        switch status {
        case "completed", "healthy", "current", "enabled": .green
        case "queued", "waiting", "stale", "paused": .orange
        case "failed", "timed_out", "stopped", "disabled": .red
        default: .secondary
        }
    }

    var body: some View {
        Text(status.replacingOccurrences(of: "_", with: " "))
            .font(.caption2.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.12), in: Capsule())
            .accessibilityLabel("Status: \(status)")
    }
}

struct MetricCard: View {
    let value: String
    let label: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value).font(.title2.bold()).monospacedDigit()
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .combine)
    }
}

struct LoadingState: View {
    let label: String

    var body: some View {
        VStack(spacing: 12) {
            ProgressView()
            Text(label).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(32)
    }
}

struct EmptyState: View {
    let icon: String
    let title: String
    let message: String

    var body: some View {
        ContentUnavailableView(title, systemImage: icon,
                               description: Text(message))
    }
}

struct ConnectionBanner: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        if let error = state.error {
            HStack(alignment: .top) {
                Image(systemName: "exclamationmark.triangle.fill")
                Text(error).frame(maxWidth: .infinity, alignment: .leading)
                Button("Retry") { Task { await state.refresh() } }
            }
            .font(.callout)
            .foregroundStyle(.red)
            .padding(10)
            .background(.red.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
            .padding(.horizontal)
        }
    }
}

struct ServerToolbarMenu: ToolbarContent {
    @EnvironmentObject private var state: AppState

    var body: some ToolbarContent {
        ToolbarItem(placement: .automatic) {
            Menu {
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
            } label: {
                Label(state.selectedServer?.displayName ?? "Fleet",
                      systemImage: "server.rack")
            }
            .accessibilityLabel("Selected fleet: \(state.selectedServer?.displayName ?? "none")")
        }
    }
}

struct RunRow: View {
    @EnvironmentObject private var state: AppState
    let run: Run

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 7) {
                Circle()
                    .fill(run.isLive ? Color.green : Color.secondary.opacity(0.35))
                    .frame(width: 8, height: 8)
                Text(run.display ?? "\(state.groupName(run.groupID) ?? "General") #\(run.groupNumber)")
                    .font(.caption.weight(.bold)).monospacedDigit()
                Spacer()
                StatusChip(status: run.status)
            }
            Text(run.title ?? run.context ?? "Run \(run.id)")
                .font(.headline).lineLimit(2)
            HStack(spacing: 8) {
                Text(state.profileName(run.profileID) ?? run.profileID)
                if let hold = run.hold { Text("Held: \(hold.detail ?? hold.kind)") }
                if let waiting = run.waitingKind { Text("Waiting: \(waiting)") }
                Spacer(minLength: 0)
                Text((run.startedAt ?? run.queuedAt).relativeAge)
            }
            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding(.vertical, 5)
        .accessibilityElement(children: .combine)
    }
}

struct UsageView: View {
    let usage: Usage?
    let title: String

    var body: some View {
        GroupBox(title) {
            HStack {
                Fact(label: "Input", value: usage?.inputTokens?.formatted() ?? "—")
                Spacer()
                Fact(label: "Output", value: usage?.outputTokens?.formatted() ?? "—")
                Spacer()
                Fact(label: "Metered API cost", value: (usage?.costUSD).money)
            }
            .frame(maxWidth: .infinity)
        }
    }
}

struct Fact: View {
    let label: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(.secondary)
            Text(value).textSelection(.enabled)
        }
        .accessibilityElement(children: .combine)
    }
}

extension String? {
    var relativeAge: String {
        guard let self,
              let date = ISO8601DateFormatter().date(from: self) else { return "—" }
        return date.formatted(.relative(presentation: .named))
    }
}

extension Int {
    var byteCount: String { ByteCountFormatter.string(fromByteCount: Int64(self), countStyle: .file) }
}

extension Double? {
    var money: String {
        guard let self else { return "—" }
        return self.formatted(.currency(code: "USD").precision(.fractionLength(self >= 1 ? 2 : 4)))
    }
}
