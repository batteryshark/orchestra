import Foundation
import Testing
@testable import Orchestra

struct OrchestraTests {
    @Test func appTransportPolicyDoesNotOverrideUserConfiguredHTTPException() {
        let transportPolicy = Bundle.main.object(forInfoDictionaryKey: "NSAppTransportSecurity")
            as? [String: Any]

        #expect(transportPolicy?["NSAllowsArbitraryLoads"] as? Bool == true)
        // On iOS 10+, this key's presence makes iOS ignore NSAllowsArbitraryLoads.
        #expect(transportPolicy?["NSAllowsLocalNetworking"] == nil)
    }

    @Test func serverURLRequiresExplicitHTTPTransport() throws {
        #expect(throws: OrchestraAPIError.invalidServerURL) {
            try OrchestraAPIClient.validatedURL(from: "macbook:4764")
        }
        #expect(throws: OrchestraAPIError.unsupportedScheme) {
            try OrchestraAPIClient.validatedURL(from: "ftp://macbook/file")
        }
        #expect(try OrchestraAPIClient.validatedURL(from: " http://macbook:4764 ").absoluteString
                == "http://macbook:4764")
    }

    @Test func dashboardPayloadDecodesExistingWireShape() throws {
        let payload = #"""
        {
          "root":"/tmp/project",
          "project_id":"project-1",
          "runs":[{
            "id":17,"agent":"claude","backend":"claude","model":"claude-sonnet",
            "title":"Polish the release","work_item":"W-0126","team":null,
            "requested_by":"codex","branch":null,"parent_run":null,"lead_run":null,
            "child_depth":0,"session_ref":null,"status":"running","exit_code":null,
            "summary":null,"started_at":"2026-07-19T10:00:00Z","finished_at":null,
            "slug":"lively_fox"
          }],
          "messages":[],"feed":[],"teams":[],
          "ensemble":[{
            "id":"team-1","name":"review","status":"active","lead_session":"lead-session",
            "members":[{"name":"scout","model":"provider/model","status":"busy",
              "execution_status":"running","session_id":"session-1"}],
            "tasks":[{"content":"inspect","status":"in_progress","priority":"high","assignee":"scout"}]
          }]
        }
        """#.data(using: .utf8)!
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let state = try decoder.decode(DashboardState.self, from: payload)

        #expect(state.projectId == "project-1")
        #expect(state.runs.first?.displayName == "lively fox")
        #expect(state.runs.first?.isTerminal == false)
        #expect(state.ensemble.first?.tasks.first?.priority == "high")
    }

    @Test func usagePayloadAcceptsNullHeadroom() throws {
        let payload = #"""
        {
          "generated_at":"2026-07-19T10:00:00Z","status":"partial",
          "providers":[{
            "id":"claude","name":"Claude","status":"auth_required","plan":null,
            "windows":[],"message":"Login needed","source":null,"rate_limit_resets":null,
            "fetched_at":"2026-07-19T10:00:00Z","headroom_percent":null
          }]
        }
        """#.data(using: .utf8)!
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let snapshot = try decoder.decode(UsageSnapshot.self, from: payload)

        #expect(snapshot.providers.first?.headroomPercent == nil)
        #expect(snapshot.providers.first?.message == "Login needed")
    }
}
