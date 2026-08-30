import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ClientContractTests(unittest.TestCase):
    def test_browser_is_v2_only_and_exposes_operator_surfaces(self):
        text = (ROOT / "orchestra" / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"/api/(?!v2(?:/|'))")
        for label in ("Runs", "Inbox", "Groups", "Profiles", "Runway", "Fleet", "Settings"):
            self.assertIn(f"'{label}'", text)
        for pane in ("thread", "artifacts", "changes", "usage", "observer",
                     "logs", "lineage"):
            self.assertIn(f"'{pane}'", text)
        self.assertIn("new EventSource('/api/v2/stream'", text)
        for event in ("fleet.changed", "inbox.changed", "run.event"):
            self.assertIn(f"'{event}'", text)
        self.assertIn("/api/v2/outbox", text)
        self.assertIn("/api/v2/profile-discovery?local=", text)
        self.assertIn("setInterval(()=>refresh({quiet:true}),45000)", text)
        self.assertIn("browser:true", text)
        self.assertIn("bootstrap-pair", text)
        self.assertIn("linked_profile_ids", text)
        self.assertIn("Range:'bytes=-262144'", text)
        self.assertIn("Download full log", text)
        self.assertIn("direction=older", text)
        self.assertIn("Load older activity", text)
        self.assertIn("function scheduleRunRefresh(id)", text)
        self.assertIn("runRefreshTimer=setTimeout(()=>", text)
        self.assertIn("p.observer_compatible", text)
        self.assertIn("function observerIssue(id)", text)
        self.assertIn("Configured Observer unavailable", text)
        self.assertIn("env_configured", text)
        self.assertIn("config_configured", text)
        self.assertIn("function replacementJSON(raw,label)", text)
        self.assertIn("Existing values are never returned or prefilled", text)
        self.assertNotIn('id="new-observer"', text)
        self.assertIn('<select id="new-profile">${filterOptions(state.profiles.filter', text)
        self.assertIn('id="new-context" required', text)
        self.assertIn('id="new-cwd"', text)
        self.assertIn("cwd_configured", text)
        self.assertIn("cwd_source", text)
        self.assertIn("Show harness and lifecycle events", text)
        self.assertIn("Return to live", text)
        self.assertIn("/api/v2/service-log", text)
        self.assertIn('value="cancel" formnovalidate', text)
        self.assertIn("data-new-runway", text)
        self.assertIn("argv_configured", text)
        self.assertIn("/api/v2/settings", text)
        self.assertNotIn("resident", text)
        self.assertNotIn("/api/v2/scopes", text)
        self.assertNotIn('id="new-mission"', text)
        self.assertNotIn('id="new-isolation"', text)
        for internal_alias in ("artifact_id", "mime_type", "size_bytes", "base_commit",
                               "head_commit", "checkpoint_commit", "linked_profiles"):
            self.assertNotIn(internal_alias, text)
        self.assertNotRegex(text.lower(), r"\b(nod|landing|control turn|project_id)\b")

    def test_apple_sources_are_v2_only_and_have_a_native_mac_target(self):
        sources = "\n".join(path.read_text(encoding="utf-8")
                            for path in (ROOT / "ios" / "Orchestra").glob("*.swift"))
        self.assertIn('"api/v2/snapshot"', sources)
        self.assertIn('"api/v2/outbox"', sources)
        self.assertIn('"api/v2/profile-discovery?local=', sources)
        self.assertIn('"Bearer \\(token)"', sources)
        self.assertIn('"bytes=-262144"', sources)
        self.assertIn('direction: String = "older"', sources)
        self.assertIn('"q": q', sources)
        self.assertIn("Prepare full log", sources)
        self.assertIn("observerCompatible", sources)
        self.assertIn("observerSelectionIssue", sources)
        self.assertIn("ForEach(workerProfiles)", sources)
        self.assertIn("state.profiles.filter(\\.observerReady)", sources)
        self.assertIn("Observer unavailable", sources)
        self.assertIn("envConfigured", sources)
        self.assertIn("configConfigured", sources)
        self.assertIn("replacementObject", sources)
        self.assertIn("Existing values are never returned or prefilled", sources)
        self.assertIn("RunwaySourceEditor", sources)
        self.assertNotIn("resident", sources)
        self.assertIn("cwdConfigured", sources)
        self.assertIn("cwdSource", sources)
        self.assertNotIn('case scope', sources)
        self.assertNotIn('case mission', sources)
        self.assertNotRegex(sources, r'"api/(?!v2/)')
        for internal_alias in ("artifact_id", "mime_type", "size_bytes", "base_commit",
                               "head_commit", "checkpoint_commit", "linked_profiles"):
            self.assertNotIn(internal_alias, sources)
        self.assertNotRegex(sources.lower(), r"\b(nod|landing|control turn|projectid)\b")
        project = (ROOT / "ios" / "Orchestra.xcodeproj" / "project.pbxproj").read_text()
        self.assertIn("Orchestra macOS", project)
        self.assertIn("SDKROOT = macosx", project)

    def test_snapshot_fixture_is_a_v2_envelope(self):
        fixture = (ROOT / "ios" / "OrchestraTests" / "Fixtures" / "snapshot-v2.json")
        self.assertTrue(fixture.exists())
        self.assertIn('"api_version": 2', fixture.read_text())


if __name__ == "__main__":
    unittest.main()
