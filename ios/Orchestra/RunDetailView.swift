import SwiftUI

struct RunDetailView: View {
    @EnvironmentObject private var model: AppModel
    let initialRun: Run

    @State private var run: Run
    @State private var items: [TranscriptItem] = []
    @State private var etag: String?
    @State private var errorMessage: String?
    @State private var followTail = true
    @State private var isStopping = false
    @State private var confirmStop = false

    init(initialRun: Run) {
        self.initialRun = initialRun
        _run = State(initialValue: initialRun)
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    runHeader
                    configuration

                    if let summary = run.summary, !summary.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Label("Summary", systemImage: "text.quote")
                                .font(.headline)
                            WrappedText(text: summary)
                        }
                        .panelStyle()
                    }

                    if let errorMessage {
                        Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .panelStyle()
                    }

                    if items.isEmpty && errorMessage == nil {
                        HStack(spacing: 10) {
                            ProgressView()
                            Text("Waiting for worker output…").foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .center)
                        .padding(.vertical, 30)
                    }

                    ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                        TranscriptItemView(item: item, index: index)
                            .id(index)
                    }
                    Color.clear.frame(height: 1).id("tail")
                }
                .padding()
            }
            .background(Color(.systemGroupedBackground))
            .onChange(of: items.count) { _, _ in
                guard followTail else { return }
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("tail", anchor: .bottom) }
            }
        }
        .navigationTitle(run.displayName)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Toggle(isOn: $followTail) {
                    Image(systemName: followTail ? "arrow.down.to.line.compact" : "pause")
                }
                .toggleStyle(.button)
                .help("Follow new output")

                if !run.isTerminal {
                    Button(role: .destructive) { confirmStop = true } label: {
                        if isStopping { ProgressView() } else { Image(systemName: "stop.fill") }
                    }
                    .disabled(isStopping)
                    .accessibilityLabel("Stop run")
                }
            }
        }
        .confirmationDialog("Stop run #\(run.id)?", isPresented: $confirmStop,
                            titleVisibility: .visible) {
            Button("Stop Run", role: .destructive) { Task { await stopRun() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This uses the same cancellation path as `orchestra kill`.")
        }
        .task { await pollTranscript() }
    }

    private var runHeader: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("#\(run.id)")
                    .font(.headline.monospaced())
                StatusChip(status: run.status, color: run.statusColor)
                Spacer()
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text(OrchestraFormatting.elapsed(from: run.startedAt,
                                                     to: run.finishedAt,
                                                     now: context.date))
                        .font(.subheadline.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
            Text(run.title?.isEmpty == false ? run.title! : "Untitled run")
                .font(.title3.weight(.semibold))
            HStack(spacing: 14) {
                Label(run.agent, systemImage: "person")
                Label(run.modelDisplayName, systemImage: "cpu")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .panelStyle()
    }

    private var configuration: some View {
        DisclosureGroup {
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 8) {
                configurationRow("Backend", run.backend)
                configurationRow("Model", run.model ?? "default")
                configurationRow("Requested by", run.requestedBy)
                if let team = run.team { configurationRow("Team", team) }
                if let work = run.workItem { configurationRow("Work item", work) }
                if let branch = run.branch { configurationRow("Branch", branch) }
                if let lead = run.leadRun { configurationRow("Lead run", "#\(lead)") }
                if let parent = run.parentRun { configurationRow("Follow-up", "#\(parent)") }
            }
            .font(.caption)
            .padding(.top, 10)
        } label: {
            Label("Configuration", systemImage: "gearshape.2")
                .font(.headline)
        }
        .panelStyle()
    }

    private func configurationRow(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary)
            Text(value).font(.caption.monospaced()).textSelection(.enabled)
        }
    }

    private func pollTranscript() async {
        while !Task.isCancelled {
            do {
                let response = try await model.transcript(runID: run.id, etag: etag)
                if response.unchanged != true {
                    etag = response.etag
                    if let updatedRun = response.run { run = updatedRun }
                    items = response.items ?? []
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

    private func stopRun() async {
        isStopping = true
        defer { isStopping = false }
        do {
            try await model.stop(runID: run.id)
            etag = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

struct TranscriptItemView: View {
    let item: TranscriptItem
    let index: Int
    @State private var expanded = false

    var body: some View {
        switch item.kind {
        case "text":
            WrappedText(text: item.body ?? "")
                .padding(.horizontal, 2)
        case "thinking":
            disclosure(title: "Thinking", icon: "brain.head.profile", tint: .purple) {
                WrappedText(text: item.body ?? "", font: .caption.monospaced())
            }
        case "tool":
            disclosure(title: item.name ?? "Tool", icon: "wrench.and.screwdriver", tint: .cyan) {
                VStack(alignment: .leading, spacing: 12) {
                    if let input = item.input, !input.isEmpty {
                        labeledCode("Input", input)
                    }
                    if let output = item.output, !output.isEmpty {
                        labeledCode("Output", output)
                    }
                }
            }
        case "prompt":
            disclosure(title: "Initial runner prompt", icon: "doc.text", tint: .blue) {
                WrappedText(text: item.body ?? "", font: .caption.monospaced())
            }
        case "delivery":
            deliveryCard
        case "question":
            questionCard
        case "error":
            Label(item.body ?? "Worker error", systemImage: "exclamationmark.octagon.fill")
                .foregroundStyle(.red)
                .panelStyle(tint: .red)
        default:
            if let body = item.body, !body.isEmpty {
                WrappedText(text: body, font: .caption.monospaced(), color: .secondary)
            }
        }
    }

    private func disclosure<Content: View>(title: String, icon: String, tint: Color,
                                           @ViewBuilder content: @escaping () -> Content) -> some View {
        DisclosureGroup(isExpanded: $expanded) {
            content().padding(.top, 10)
        } label: {
            HStack {
                Label(title, systemImage: icon)
                    .foregroundStyle(tint)
                    .font(.subheadline.weight(.semibold))
                Spacer()
                if let status = item.status, !status.isEmpty {
                    Text(status).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .panelStyle(tint: tint)
    }

    private func labeledCode(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased())
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
            WrappedText(text: value, font: .caption.monospaced())
        }
    }

    private var deliveryCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(deliveryTitle, systemImage: deliveryIcon)
                    .font(.subheadline.weight(.semibold))
                Spacer()
                if let phase = item.phase {
                    Text(phase).font(.caption).foregroundStyle(.secondary)
                }
            }
            if let sender = item.sender, let recipient = item.recipient {
                Text("\(sender) → \(recipient)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let body = item.body, !body.isEmpty { WrappedText(text: body) }
        }
        .panelStyle(tint: item.delivery == "interrupt" ? .red : .blue)
    }

    private var deliveryTitle: String {
        switch item.delivery {
        case "interrupt": "Interrupt"
        case "checkin": "Supervisor check-in"
        default: "Queued message"
        }
    }

    private var deliveryIcon: String {
        switch item.delivery {
        case "interrupt": "exclamationmark.bubble.fill"
        case "checkin": "clock.badge.checkmark"
        default: "tray.and.arrow.down.fill"
        }
    }

    private var questionCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(item.status == "waiting" ? "Worker needs an answer" : "Worker question",
                  systemImage: item.status == "waiting" ? "questionmark.bubble.fill" : "checkmark.bubble.fill")
                .font(.headline)
                .foregroundStyle(item.status == "waiting" ? .orange : .green)
            if let question = item.question { WrappedText(text: question) }
            if let fallback = item.recommendedDefault, !fallback.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    Text("RECOMMENDED FALLBACK")
                        .font(.caption2.weight(.bold)).foregroundStyle(.secondary)
                    WrappedText(text: fallback, font: .subheadline)
                }
            }
            if let answer = item.answer, !answer.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    Text("ANSWER\(item.answeredBy.map { " · \($0)" } ?? "")")
                        .font(.caption2.weight(.bold)).foregroundStyle(.secondary)
                    WrappedText(text: answer, font: .subheadline)
                }
            } else if item.status == "waiting" {
                Text("Answer from the Orchestra CLI. Fallback at \(OrchestraFormatting.timestamp(item.deadlineAt)).")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .panelStyle(tint: item.status == "waiting" ? .orange : .green)
    }
}

private extension View {
    func panelStyle(tint: Color = .clear) -> some View {
        self
            .padding()
            .background(Color(.secondarySystemGroupedBackground),
                        in: RoundedRectangle(cornerRadius: 16))
            .overlay(alignment: .leading) {
                if tint != .clear {
                    RoundedRectangle(cornerRadius: 2).fill(tint).frame(width: 3).padding(.vertical, 10)
                }
            }
    }
}
