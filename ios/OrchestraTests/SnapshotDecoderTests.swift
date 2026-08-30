import XCTest
@testable import Orchestra

final class SnapshotDecoderTests: XCTestCase {
    private func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(
            forResource: name, withExtension: "json"))
        return try Data(contentsOf: url)
    }

    func testV2SnapshotEnvelopePinsIdentityAndIgnoresAdditiveFields() throws {
        let response: APIValue<FleetSnapshot> = try OrchestraAPI.decodeEnvelope(
            fixture("snapshot-v2"))
        XCTAssertEqual(response.instanceID, "7b9049b4-9097-4b8c-bfed-819129ab273e")
        XCTAssertEqual(response.value.instance.name, "Studio Mac")
        XCTAssertEqual(response.value.scheduler.maxActive, 8)
        XCTAssertEqual(response.value.counts.runsTotal, 31)
        XCTAssertEqual(response.value.messages.undeliverable, 1)
        XCTAssertEqual(response.value.observer?.minimumEvents, 5)
        XCTAssertEqual(response.value.observer?.authority, "correct_then_stop")
        XCTAssertEqual(response.value.storage?.artifactBytes, 4096)
    }

    func testAnythingOtherThanAPIV2IsRejected() throws {
        let data = Data(#"{"api_version":1,"instance_id":"old","data":{}}"#.utf8)
        XCTAssertThrowsError(try OrchestraAPI.decodeEnvelope(data) as APIValue<FleetSnapshot>) {
            XCTAssertEqual($0 as? APIError, .apiVersion(1))
        }
    }

    func testEveryLifecycleAndWaitShapeDecodes() throws {
        let statuses = ["queued", "starting", "running", "waiting", "completed",
                        "failed", "timed_out", "stopped", "skipped"]
        for (index, status) in statuses.enumerated() {
            let waiting = status == "waiting" ? #", "waiting_kind":"children""# : ""
            let hold = status == "queued" ? #", "hold":{"kind":"runway","detail":"fresh zero"}"# : ""
            let data = Data("""
            {"id":\(index + 1),"display":"Research #\(index + 1)",
             "group_id":"group-research","group_number":\(index + 1),
             "profile_id":"profile-fast","context":"Investigate the topic",
             "cwd_source":"group","status":"\(status)"\(waiting)\(hold)}
            """.utf8)
            let run = try JSONDecoder().decode(Run.self, from: data)
            XCTAssertEqual(run.isTerminal, Run.terminalStates.contains(status))
            XCTAssertEqual(run.cwdSource, "group")
            if status == "waiting" { XCTAssertEqual(run.waitingKind, "children") }
            if status == "queued" { XCTAssertEqual(run.hold?.kind, "runway") }
        }
    }

    func testRunCanOmitEveryOptionalEvidenceField() throws {
        let data = Data(#"{"id":42,"group_id":"general","group_number":7,"profile_id":"fast","context":"Organize mail","cwd_source":"managed","status":"running"}"#.utf8)
        let run = try JSONDecoder().decode(Run.self, from: data)
        XCTAssertNil(run.profileSnapshot)
        XCTAssertNil(run.runtimeSnapshot)
        XCTAssertNil(run.usage)
        XCTAssertEqual(run.groupNumber, 7)
        XCTAssertEqual(run.context, "Organize mail")
        XCTAssertEqual(run.cwdSource, "managed")
        XCTAssertTrue(run.isLive)
    }

    func testProfileObserverCompatibilityProjectionDecodesFailClosed() throws {
        let compatible = try JSONDecoder().decode(Profile.self, from: Data(#"{"id":"safe","name":"Safe","runtime_id":"claude","tier":1,"priority":0,"env_configured":true,"config_configured":false,"observer_compatible":true,"observer_incompatibility":null,"enabled":true}"#.utf8))
        XCTAssertTrue(compatible.observerReady)
        XCTAssertNil(compatible.observerIssue)
        XCTAssertEqual(compatible.envConfigured, true)
        XCTAssertEqual(compatible.configConfigured, false)

        let workerOnly = try JSONDecoder().decode(Profile.self, from: Data(#"{"id":"worker","name":"Worker","runtime_id":"codex","tier":2,"priority":0,"observer_compatible":false,"observer_incompatibility":"codex runtime cannot provide a tool-free Observer","enabled":true}"#.utf8))
        XCTAssertFalse(workerOnly.observerReady)
        XCTAssertEqual(workerOnly.observerIssue,
                       "codex runtime cannot provide a tool-free Observer")
        XCTAssertEqual(compatible.tierName, "Workhorse")
        XCTAssertEqual(workerOnly.tierName, "Core")
    }

    func testGroupProjectsOnlyWhetherItsPrivateCWDIsConfigured() throws {
        let group = try JSONDecoder().decode(RunGroup.self, from: Data(#"{"id":"research","name":"Research","slug":"research","archived":false,"next_number":9,"runs_count":8,"cwd_configured":true}"#.utf8))
        XCTAssertEqual(group.cwdConfigured, true)
        XCTAssertEqual(group.nextNumber, 9)
    }

    func testWriteOnlyConfigurationProjectsPresenceAndParsesReplacements() throws {
        let runtime = try JSONDecoder().decode(RuntimeConfig.self, from: Data(#"{"id":"r","name":"Runtime","kind":"claude","argv":[],"config_configured":true,"enabled":true}"#.utf8))
        XCTAssertEqual(runtime.configConfigured, true)
        XCTAssertNil(try Orchestra.replacementObject("  ", label: "Configuration"))
        XCTAssertEqual(try Orchestra.replacementObject("{}", label: "Configuration"), [:])
        XCTAssertEqual(try Orchestra.replacementStringMap(#"{"MODE":"fast"}"#,
                                                          label: "Environment")?["MODE"], "fast")
        XCTAssertThrowsError(try Orchestra.replacementStringMap(#"{"COUNT":2}"#,
                                                                label: "Environment"))
    }

    func testPagedRunsAndAttentionUseItemsAndOpaqueCursor() throws {
        let data = Data(#"{"api_version":2,"instance_id":"x","data":{"items":[{"id":1,"group_id":"general","group_number":1,"profile_id":"p","context":"Do the thing","cwd_source":"managed","status":"queued"}],"next_cursor":"opaque-older","resume_cursor":"opaque-newest","has_more":true}}"#.utf8)
        let page: APIValue<APIPage<Run>> = try OrchestraAPI.decodeEnvelope(data)
        XCTAssertEqual(page.value.items.first?.groupNumber, 1)
        XCTAssertEqual(page.value.nextCursor, "opaque-older")
        XCTAssertEqual(page.value.resumeCursor, "opaque-newest")
        XCTAssertTrue(page.value.hasMore)
    }

    func testArtifactAndObserverRecordsDecodeIndependentlyOfRuns() throws {
        let artifact = try JSONDecoder().decode(Artifact.self, from: Data(#"{"id":"artifact-1","run_id":42,"name":"brief.md","media_type":"text/markdown","byte_size":123,"sha256":"abcdef","created_at":"now"}"#.utf8))
        XCTAssertEqual(artifact.mediaType, "text/markdown")
        let detail = try JSONDecoder().decode(ObserverRunDetail.self, from: Data(#"{"checks":[{"id":8,"action":"tell","judgment":"drifting","rationale":"request expanded","evidence_from":10,"evidence_to":14,"usage":{"total_tokens":90,"cost_usd":0.02}}],"usage":{"total_tokens":90,"cost_usd":0.02}}"#.utf8))
        XCTAssertEqual(detail.checks.first?.action, "tell")
        XCTAssertEqual(detail.checks.first?.evidenceTo, 14)
        XCTAssertEqual(detail.usage?.totalTokens, 90)
    }

    func testCanonicalEvidenceShapesDecodeWithoutInternalAliases() throws {
        let changes = try JSONDecoder().decode(RunChanges.self, from: Data(#"{"branch":"run/42","base":"abc","head":"def","checkpoints":[{"id":"fed","commit":"fed"}],"patch":"diff"}"#.utf8))
        XCTAssertEqual(changes.base, "abc")
        XCTAssertEqual(changes.checkpoints.first?.commit, "fed")

        let lineage = try JSONDecoder().decode(RunLineage.self, from: Data(#"{"root_run_id":1,"items":[{"id":1,"group_id":"general","group_number":1,"profile_id":"p","context":"Done","cwd_source":"inherited","status":"completed"}]}"#.utf8))
        XCTAssertEqual(lineage.rootRunID, 1)
        XCTAssertEqual(lineage.items.first?.id, 1)

        let attention = try JSONDecoder().decode(AttentionItem.self, from: Data(#"{"id":"7","state":"open","kind":"question","blocking":true,"prompt":"Choose","message":"Which source?","detail":null,"choices":[],"fallback":{"mode":"read_only"},"opened_at":"now"}"#.utf8))
        XCTAssertEqual(attention.id, "7")
        XCTAssertEqual(attention.message, "Which source?")
        XCTAssertEqual(attention.fallback?.description, "mode: read_only")
    }

    func testUnknownRunwayWindowRemainsVisible() throws {
        let source = try JSONDecoder().decode(RunwaySource.self, from: Data(#"{"id":"source-1","name":"Primary","provider":"codex","account":"personal","lane":null,"adapter":"command","enabled":true,"archived":false,"argv_configured":true,"config_configured":false,"status":"unknown","fresh":false,"observed_at":null,"burn_rate":null,"windows":[{"id":"weekly","name":"Weekly","remaining_percent":null,"resets_at":null}],"linked_profile_ids":[],"history":[{"observed_at":null,"burn_rate":null,"windows":[]}]}"#.utf8))
        XCTAssertNil(source.windows.first?.remainingPercent)
        XCTAssertNil(source.history?.first?.observedAt)
        XCTAssertTrue(source.argvConfigured)
    }

    func testStatisticsAndMessageReceiptsUseCanonicalShapes() throws {
        let statistics = try JSONDecoder().decode(RunStatistics.self, from: Data(#"{"runs":12,"agent_seconds":3661,"by_status":{"running":2},"worker_usage":{"total_tokens":100,"cost_usd":0.1},"observer_usage":{"total_tokens":20,"cost_usd":0.02},"combined_usage":{"input_tokens":80,"output_tokens":40,"total_tokens":120,"cost_usd":0.12}}"#.utf8))
        XCTAssertEqual(statistics.byStatus["running"], 2)
        XCTAssertEqual(statistics.agentSeconds, 3661)
        XCTAssertEqual(statistics.combinedUsage?.inputTokens, 80)
        XCTAssertEqual(statistics.combinedUsage?.totalTokens, 120)

        let message = try JSONDecoder().decode(RunMessage.self, from: Data(#"{"id":9,"run_id":42,"display":"Research #12","direction":"outbound","sender":"worker","kind":"result","status":"undeliverable","body":"Finished","correlation_id":"reply-7","reply_to":3,"created_at":"now","delivered_at":null,"undeliverable_at":"later","delivery_error":"operator offline"}"#.utf8))
        XCTAssertEqual(message.direction, "outbound")
        XCTAssertEqual(message.status, "undeliverable")
        XCTAssertEqual(message.deliveryError, "operator offline")
        XCTAssertEqual(message.replyTo, 3)
        XCTAssertEqual(message.display, "Research #12")
    }

    func testPairingAcceptsCodeOrURIWithoutPersistingSecretsInTheClaim() throws {
        XCTAssertEqual(try PairingClaim.parse("ABCD-1234"),
                       PairingClaim(endpoint: nil, code: "ABCD-1234"))
        XCTAssertEqual(try PairingClaim.parse(
            "orchestra://pair?endpoint=https%3A%2F%2Fstudio.example%2F&pairing_id=pair-7&code=ABCD-1234"),
            PairingClaim(endpoint: "https://studio.example/", pairingID: "pair-7",
                         code: "ABCD-1234"))
        XCTAssertThrowsError(try PairingClaim.parse("orchestra://pair"))
    }

    func testProfileDiscoverySeparatesHarnessAndHostLocalModels() throws {
        let data = Data(#"{"runtimes":{"opencode":{"data":{"openai":["gpt-5"]},"error":null},"codex":{"data":[{"model":"gpt-5.6-sol","efforts":["low","high"],"default_effort":"high"}],"error":null},"reasonix":{"data":[{"provider":"openai","models":["gpt-5"],"efforts":[],"default_effort":null}],"error":null},"claude":{"data":null,"error":"unavailable"}},"local_requested":true,"local_models":[{"id":"qwen-local","source":"lm-studio"}]}"#.utf8)
        let discovery = try JSONDecoder().decode(ProfileDiscovery.self, from: data)
        XCTAssertEqual(discovery.runtimes.codex.data?.first?.model, "gpt-5.6-sol")
        XCTAssertEqual(discovery.runtimes.opencode.data?["openai"], ["gpt-5"])
        XCTAssertEqual(discovery.runtimes.claude.error, "unavailable")
        XCTAssertEqual(discovery.localModels.first?.id, "qwen-local")
    }

    func testSSEDecoderKeepsBlankFrameBoundaryAndMultilineData() throws {
        let wire = ["id: 14", "event: run.event", "data: {\"id\":14,", "data: \"kind\":\"output\"}", ""]
        var decoder = SSEDecoder()
        let messages = wire.compactMap { decoder.feed(line: $0) }
        let message = try XCTUnwrap(messages.first)
        XCTAssertEqual(message.id, "14")
        XCTAssertEqual(message.event, "run.event")
        XCTAssertEqual(message.data, "{\"id\":14,\n\"kind\":\"output\"}")
    }

    func testInstanceReplacementHasAnExplicitRepairMessage() {
        let error = APIError.instanceChanged(expected: "old-instance", received: "new-instance")
        XCTAssertTrue(error.localizedDescription.contains("Pair it again"))
    }
}
