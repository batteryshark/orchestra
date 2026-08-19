import XCTest
@testable import Dromond

final class SnapshotDecoderTests: XCTestCase {
    func testCapturedSnapshotV6Decodes() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(
            forResource: "snapshot-v6",
            withExtension: "json"
        ))
        let data = try Data(contentsOf: url)
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(
            Set(json.keys),
            Set(["version", "generated_at", "home", "runs", "live_runs", "projects",
                 "dispatch", "profiles", "runway", "statistics", "findings", "proposals", "daemon"])
        )

        let snapshot = try JSONDecoder().decode(Snapshot.self, from: data)
        XCTAssertEqual(snapshot.version, Snapshot.minimumVersion)
        XCTAssertEqual(snapshot.liveRuns, 1)
        XCTAssertEqual(snapshot.runs.first?.workItem, "W-0141")
        XCTAssertEqual(snapshot.runs.first?.profile, "sol-medium")
    }

    /// The live daemon serves v7. An exact-equality version gate rejected it
    /// with "not in the correct format" on a snapshot that had merely grown
    /// fields, and the app was unusable with no way forward.
    func testNewerSnapshotDecodes() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(
            forResource: "snapshot-v7",
            withExtension: "json"
        ))
        let data = try Data(contentsOf: url)
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: data)
        XCTAssertGreaterThan(snapshot.version, Snapshot.minimumVersion)
        XCTAssertFalse(snapshot.runs.isEmpty)
        XCTAssertNotNil(snapshot.runs.first?.status)
    }

    func testOlderSnapshotIsRefusedWithAReadableReason() throws {
        let data = try XCTUnwrap(#"""
        {"version": 5, "generated_at": "x", "runs": [], "live_runs": 0}
        """#.data(using: .utf8))
        XCTAssertThrowsError(try JSONDecoder().decode(Snapshot.self, from: data)) { error in
            XCTAssertTrue("\(error)".contains("update the daemon"), "\(error)")
        }
    }

    /// The regression that mattered: the decoder only emits on an empty line,
    /// and `AsyncBytes.lines` drops those, so the trace stream yielded nothing
    /// at all while the daemon sent well-formed frames. This pins the split
    /// itself — every line, empty ones included, CRLF tolerated.
    func testFrameSplittingKeepsTheEmptyLinesSSEDependsOn() {
        let wire = "retry: 3000\n\nid: 2610\r\nevent: trace\r\ndata: {\"id\":1}\r\n\r\n"
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
        XCTAssertEqual(lines, ["retry: 3000", "", "id: 2610", "event: trace",
                               "data: {\"id\":1}", ""])

        var decoder = SSEDecoder()
        var messages: [SSEMessage] = []
        for line in lines {
            if let message = decoder.feed(line: line) { messages.append(message) }
        }
        XCTAssertEqual(messages, [SSEMessage(event: "trace", data: "{\"id\":1}")])
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

    func testSSEDecoderJoinsDataLinesAndIgnoresKeepalives() {
        var decoder = SSEDecoder()
        XCTAssertNil(decoder.feed(line: ": keepalive"))
        XCTAssertNil(decoder.feed(line: "event: trace"))
        XCTAssertNil(decoder.feed(line: "data: {\"payload\":"))
        XCTAssertNil(decoder.feed(line: "data: \"hello\"}"))
        XCTAssertEqual(
            decoder.feed(line: ""),
            SSEMessage(event: "trace", data: "{\"payload\":\n\"hello\"}")
        )
    }
}
