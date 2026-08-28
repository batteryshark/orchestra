import unittest
from pathlib import Path

from orchestra import brief

FIXED_CHAR_CEILING = 1200  # DESIGN D6: <= 300 fixed tokens per dispatch
CONTINUATION_CHAR_CEILING = 600  # DESIGN D6: continuation wrapper ~130 tokens


def _compose(mission: str = "", **kwargs) -> str:
    return brief.compose(
        run_id=1, slug="calm_otter", profile={"name": "codex"}, mission=mission,
        requester="human", root=Path("/p"), workdir="/p", **kwargs)


def _without_writeback(text: str) -> str:
    # D6 measures the protocol card and header, not the path-loaded style doc.
    return text.replace(brief.writeback_section(), "")


class BriefBudgetTests(unittest.TestCase):
    def test_fixed_portion_stays_inside_d6_budget(self) -> None:
        """Fails when the fixed injection grows past the D6 ceiling."""
        self.assertLessEqual(len(_without_writeback(_compose(mission=""))),
                             FIXED_CHAR_CEILING)

    def test_protocol_card_renders_within_ten_lines(self) -> None:
        lines = [l for l in brief.PROTOCOL_CARD.splitlines() if l.strip()]
        self.assertLessEqual(len(lines), 10)

    def test_continuation_wrapper_budget(self) -> None:
        text = brief.compose_continuation(run_id=2, parent_run=1, instructions="")
        self.assertLessEqual(len(text), CONTINUATION_CHAR_CEILING)


class BriefContentTests(unittest.TestCase):
    def test_mission_and_context_present(self) -> None:
        text = _compose(mission="fix the bug", extra_context="see file X")
        self.assertIn("fix the bug", text)
        self.assertIn("see file X", text)
        self.assertIn("## Protocol", text)
        self.assertIn("calm_otter", text)

    def test_snapshot_seam_is_capped(self) -> None:
        # Phase-2 seam: the source adapter passes the frozen snapshot here.
        text = _compose(mission="m", snapshot="s" * 5000)
        self.assertIn("## Item snapshot", text)
        self.assertNotIn("s" * (brief.SNAPSHOT_MAX_CHARS + 1), text)

    def test_snapshot_protocol_is_injected_verbatim(self) -> None:
        # The checklist card is the ADAPTER's rendering (CONTRACT §7); the
        # core injects it beside the snapshot and knows nothing about it.
        text = _compose(mission="m", snapshot="W-1 · t [ready]",
                        snapshot_protocol="account for every criterion")
        self.assertIn("account for every criterion", text)
        # No snapshot, no protocol: a brief never teaches a verb it cannot use.
        bare = _compose(mission="m", snapshot_protocol="account for it")
        self.assertNotIn("account for it", bare)

    def test_spawn_is_never_advertised(self) -> None:
        text = _compose(mission="m")
        self.assertNotIn("delegate", text.lower())
        self.assertNotIn("child run", text.lower())

    def test_continuation_carries_instructions(self) -> None:
        text = brief.compose_continuation(run_id=5, parent_run=3,
                                          instructions="also update docs")
        self.assertIn("continuation of run 3", text)
        self.assertIn("also update docs", text)
        # Nothing landed: the section is absent rather than empty.
        self.assertNotIn("Landed on the base branch", text)

    def test_recent_commits_tell_a_fresh_run_what_it_need_not_rebuild(self) -> None:
        text = _compose(mission="m", recent_commits=[
            "71aac22 a re-dispatch never assumes the previous world exists",
            "9e6512d observer status carries its own interval"])
        self.assertIn("## Recently landed here", text)
        self.assertIn("- 71aac22 a re-dispatch never assumes", text)
        self.assertIn("Work already done is not your mission", text)
        # A repository with no commits, or none at all, adds no empty heading.
        self.assertNotIn("Recently landed", _compose(mission="m", recent_commits=[]))

    def test_the_recent_commit_block_is_capped(self) -> None:
        text = _compose(mission="m", recent_commits=["c" * 200] * 40)
        self.assertNotIn("c" * (brief.RECENT_COMMITS_MAX_CHARS + 1), text)

    def test_a_fresh_dispatch_brief_contains_the_writeback_style(self) -> None:
        text = _compose(mission="m")
        style = brief.WRITEBACK_STYLE.read_text(encoding="utf-8")
        self.assertIn("## Writeback", text)
        self.assertIn(style.strip(), text)
        self.assertIn("human operator", text)
        self.assertIn("every run handoff and any optional adapter writeback",
                      text)

    def test_style_doc_is_referenced_by_path(self) -> None:
        """An edit to the doc is an edit to the brief. brief.py holds no copy."""
        src = Path(brief.__file__).read_text(encoding="utf-8")
        self.assertIn("docs/WRITEBACK-STYLE.md", src)
        self.assertEqual(brief.WRITEBACK_STYLE.name, "WRITEBACK-STYLE.md")
        self.assertTrue(brief.WRITEBACK_STYLE.is_file())
        # The STE body lives in the doc, not as a string in brief.py.
        self.assertNotIn("One idea per sentence", src)
        self.assertIn("One idea per sentence",
                      brief.WRITEBACK_STYLE.read_text(encoding="utf-8"))

    def test_two_profiles_both_carry_the_git_boundary(self) -> None:
        """Every harness reserves git writes for Orchestra."""
        for name in ("codex", "claude"):
            text = brief.compose(
                run_id=1, slug="calm_otter", profile={"name": name},
                mission="m", requester="human", root=Path("/p"), workdir="/p")
            self.assertIn("Never run git write commands", text, name)
            self.assertIn("Orchestra checkpoints isolated runs", text, name)
            self.assertIn("working directory yes", text, name)
            self.assertIn(".git host-owned", text, name)
            self.assertIn("/tmp yes", text, name)
            self.assertNotIn("Commit your git changes", text, name)

    def test_continuation_repeats_the_git_law(self) -> None:
        text = brief.compose_continuation(run_id=5, parent_run=3,
                                          instructions="carry on")
        self.assertIn("Never run git write commands", text)
        self.assertNotIn("Commit your git changes", text)

    def test_a_resumed_run_is_told_what_landed_while_it_worked(self) -> None:
        # The case that matters most: its worktree branched before these
        # commits, so it cannot see them and will rebuild them given the chance.
        text = brief.compose_continuation(
            run_id=5, parent_run=3, instructions="carry on",
            landed=["6f8bd1f a run may not land what the base does not track"])
        self.assertIn("Landed on the base branch since you started", text)
        self.assertIn("6f8bd1f", text)
        self.assertIn("Do not rebuild them", text)


if __name__ == "__main__":
    unittest.main()
