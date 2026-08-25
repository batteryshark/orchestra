import XCTest
@testable import Orchestra

final class SnapshotDecoderTests: XCTestCase {
    private func decodeFixture(_ name: String) throws -> Snapshot {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(
            forResource: name,
            withExtension: "json"
        ))
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(Snapshot.self, from: data)
    }

    func testMinimumVersionDecodesLiveRunAndUnknownFields() throws {
        // The captured v6 payload includes findings/proposals, which Snapshot
        // no longer reads. Supported old payloads must continue to decode.
        let snapshot = try decodeFixture("snapshot-v6")
        let run = try XCTUnwrap(snapshot.runs.first)
        XCTAssertEqual(snapshot.version, Snapshot.minimumVersion)
        XCTAssertEqual(snapshot.liveRuns, 1)
        XCTAssertTrue(run.live)
        XCTAssertFalse(run.isTerminal)
        XCTAssertEqual(run.workItem, "W-0141")
    }

    /// Version 7 was the first snapshot newer than the decoder's compatibility
    /// floor. An exact-equality gate rejected it after the payload merely grew
    /// fields, leaving the app unusable with no way forward.
    func testNewerSnapshotDecodesDisplayedFieldsAndIgnoresUnknownOnes() throws {
        let snapshot = try decodeFixture("snapshot-v7")
        let run = try XCTUnwrap(snapshot.runs.first)
        XCTAssertGreaterThan(snapshot.version, Snapshot.minimumVersion)
        XCTAssertTrue(run.isTerminal)
        XCTAssertFalse(run.live)
        XCTAssertEqual(run.isolation, "not_started")
        XCTAssertEqual(run.messages.first?.body, "run 31 finished: failed")
        XCTAssertEqual(run.messages.first?.undeliverableReason, "worker exited")
        XCTAssertEqual(run.messages.first?.pendingBoundary, false)
        XCTAssertEqual(snapshot.profiles.first?.model, "")
        XCTAssertEqual(snapshot.runway.first?.windows.first?.resetsIn, "in 4d")
        XCTAssertEqual(snapshot.runway.first?.readingAge, "read 3h ago")
        XCTAssertEqual(snapshot.runway.first?.creditsLabel, "1 banked reset")
        XCTAssertEqual(
            snapshot.daemon.observer,
            Observer(enabled: true, profile: "ds-flash", problem: nil,
                     firstLook: 300, interval: 1800)
        )
    }

    func testOlderSnapshotIsRefusedWithAReadableReason() throws {
        let data = Data(#"""
        {"version": 5, "generated_at": "x", "runs": [], "live_runs": 0}
        """#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(Snapshot.self, from: data)) { error in
            XCTAssertTrue("\(error)".contains("update the daemon"), "\(error)")
        }
    }

    /// The regression that mattered: the decoder only emits on an empty line,
    /// and `AsyncBytes.lines` drops those, so the trace stream yielded nothing
    /// at all while the daemon sent well-formed frames. This pins the split
    /// itself — every line, empty ones included, CRLF tolerated.
    func testFrameSplittingKeepsTraceEventsIntact() throws {
        let wire = ": keepalive\r\nevent: trace\r\n"
            + #"data: {"id":1,"kind":"tool_call","name":"shell","# + "\r\n"
            + #"data: "payload":"ls","payload_len":2,"truncated":false}"# + "\r\n\r\n"
        var lines: [String] = []
        var buffer = [UInt8]()
        for byte in Array(wire.utf8) {
            if byte == 0x0A {
                lines.append(String(decoding: buffer, as: UTF8.self))
                buffer.removeAll()
            } else if byte != 0x0D {
                buffer.append(byte)
            }
        }
        XCTAssertEqual(lines.last, "")

        var decoder = SSEDecoder()
        var messages: [SSEMessage] = []
        for line in lines {
            if let message = decoder.feed(line: line) { messages.append(message) }
        }
        let message = try XCTUnwrap(messages.first)
        XCTAssertEqual(message.event, "trace")
        let event = try JSONDecoder().decode(TraceEvent.self, from: Data(message.data.utf8))
        XCTAssertEqual(event.id, 1)
        XCTAssertEqual(event.kind, "tool_call")
        XCTAssertEqual(event.name, "shell")
        XCTAssertEqual(event.payload, "ls")
        XCTAssertEqual(event.payloadLength, 2)
        XCTAssertFalse(event.truncated)
    }

    /// Three response structs in a row declared fields the daemon never sends.
    /// Each had every field optional, so the decode SUCCEEDED and returned an
    /// all-nil object — a silent failure with nothing to report. These pin the
    /// wire's own key names, so the next rename fails loudly here instead.
    func testResponseStructsAreNamedOffTheWire() throws {
        let diff = try JSONDecoder().decode(DiffText.self, from: Data("""
        {"run": 30, "base": "abc1234", "head": "def5678",
         "text": "diff --git a/x b/x", "truncated": true, "message": null}
        """.utf8))
        XCTAssertEqual(diff.text, "diff --git a/x b/x")
        XCTAssertEqual(diff.truncated, true)
        XCTAssertEqual(diff.base, "abc1234")

        let project = try JSONDecoder().decode(ProjectDetail.self, from: Data("""
        {"project_id": "53efe3c3", "enabled_profiles": null,
         "statistics": {"runs_total": 4}, "generated_at": "now"}
        """.utf8))
        XCTAssertEqual(project.projectID, "53efe3c3")
        XCTAssertNil(project.enabledProfiles)          // null means ALL, not none
        XCTAssertEqual(project.statistics?.runsTotal, 4)

        let options = try JSONDecoder().decode([String: HarnessOptions].self, from: Data("""
        {"opencode": {"supports_effort": false, "effort_note": "no --effort flag",
          "free_model": false, "error": null,
          "models": [{"id": "xai/grok-4.6", "efforts": [], "default_effort": null}]}}
        """.utf8))
        XCTAssertEqual(options["opencode"]?.supportsEffort, false)
        XCTAssertEqual(options["opencode"]?.models.first?.id, "xai/grok-4.6")
        XCTAssertEqual(options["opencode"]?.effortNote, "no --effort flag")
    }

    /// The decision log (I-0081). The daemon answers with an OBJECT, like
    /// `/api/runway` — decoding the bare array here is the mistake that route
    /// already made once. The rows are run payloads, because a control turn IS
    /// a runs row and the log opens it in the run detail screen.
    func testTheDecisionLogDecodesAsAPageOfRuns() throws {
        let page = try JSONDecoder().decode(TurnsPage.self, from: Data("""
        {"turns": [
          {"id": 91, "status": "done", "profile": "cheap", "backend": "opencode",
           "layer": "observer", "project_id": "proj-a", "live": false,
           "summary": "ok: steady progress", "finished_at": "2026-08-19T20:45:39Z"},
          {"id": 88, "status": "failed", "profile": "cheap", "backend": "opencode",
           "layer": "router", "project_id": "proj-a", "live": false,
           "summary": "staffed big over stub"}],
         "project_id": "proj-a", "layer": null, "limit": 100,
         "generated_at": "2026-08-19T20:46:00Z"}
        """.utf8))
        XCTAssertEqual(page.turns.map(\.id), [91, 88])
        XCTAssertEqual(page.turns.first?.layer, "observer")
        XCTAssertEqual(page.turns.first?.summary, "ok: steady progress")
        XCTAssertEqual(page.generatedAt, "2026-08-19T20:46:00Z")
        // The row stamps the turn, so an unparseable date must not blank it.
        XCTAssertNotEqual("2026-08-19T20:45:39Z".relativeStamp,
                          "2026-08-19T20:45:39Z")
        XCTAssertEqual("not a date".relativeStamp, "not a date")
    }

    /// The upgrade path that must not lose anything: a phone that already had
    /// one server keeps it, pointed at the Keychain account its key is already
    /// under, so nobody is asked to retype a secret.
    func testTheFirstServerInheritsTheLegacyKeychainAccount() {
        let first = Server(label: "", url: "http://mac:3011/",
                           keyAccount: Keychain.legacyAccount)
        XCTAssertEqual(Keychain.legacyAccount, first.keyAccount)
        // A second server gets its own account, never the shared one.
        let second = Server(label: "windows", url: "http://win:3011/")
        XCTAssertEqual(second.id.uuidString, second.keyAccount)
        XCTAssertNotEqual(first.keyAccount, second.keyAccount)
    }

    func testAServerNamesItselfByHostWhenUnlabelled() {
        XCTAssertEqual("win.tailnet",
                       Server(label: "", url: "http://win.tailnet:3011/").displayName)
        XCTAssertEqual("windows box",
                       Server(label: "windows box", url: "http://win:3011/").displayName)
        // Whitespace is not a name.
        XCTAssertEqual("win", Server(label: "  ", url: "http://win:3011/").displayName)
    }

    func testServersRoundTripThroughJSON() throws {
        let servers = [Server(label: "mac", url: "http://mac:3011/",
                              keyAccount: Keychain.legacyAccount),
                       Server(label: "windows", url: "http://win:3011/")]
        let data = try JSONEncoder().encode(servers)
        let back = try JSONDecoder().decode([Server].self, from: data)
        XCTAssertEqual(servers, back)
        XCTAssertEqual(Keychain.legacyAccount, back[0].keyAccount,
                       "the account must survive persistence or the key is orphaned")
    }
}
