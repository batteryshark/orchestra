import unittest
from pathlib import Path

from dromond import brief

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

    def test_spawn_enabled_brief_stays_inside_the_budget(self) -> None:
        text = brief.compose(
            run_id=1, slug="calm_otter",
            profile={"name": "lead", "spawn_profiles": ["worker", "cheap"]},
            mission="", requester="human", root=Path("/p"), workdir="/p")
        self.assertLessEqual(len(_without_writeback(text)), FIXED_CHAR_CEILING)
        card_lines = [l for l in brief._protocol_card(
            {"spawn_profiles": ["worker", "cheap"]}).splitlines() if l.strip()]
        self.assertLessEqual(len(card_lines), 10)

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

    def test_work_snapshot_seam_is_capped(self) -> None:
        # Phase-2 seam: the sweeper passes the frozen Work snapshot here.
        text = _compose(mission="m", work_snapshot="s" * 5000)
        self.assertIn("## Work item snapshot", text)
        self.assertNotIn("s" * (brief.WORK_SNAPSHOT_MAX_CHARS + 1), text)

    def test_spawn_mentioned_only_when_permitted(self) -> None:
        """D11: a worker is never taught a verb it is forbidden to use."""
        silent = _compose(mission="m")
        self.assertNotIn("delegate", silent.lower())
        allowed = brief.compose(
            run_id=1, slug="calm_otter",
            profile={"name": "lead", "spawn_profiles": ["worker", "cheap"]},
            mission="m", requester="human", root=Path("/p"), workdir="/p")
        self.assertIn("delegate", allowed.lower())
        self.assertIn("worker, cheap", allowed)
        empty_list = brief.compose(
            run_id=1, slug="calm_otter",
            profile={"name": "lead", "spawn_profiles": []},
            mission="m", requester="human", root=Path("/p"), workdir="/p")
        self.assertNotIn("delegate", empty_list.lower())

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
        self.assertIn("comments, resolution summaries, filed issues, "
                      "proposed tasks, and recommendation reasons", text)

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

    def test_two_profiles_both_carry_the_git_law(self) -> None:
        """W-0259: every harness gets the same write path — files, host commits."""
        for name in ("codex", "claude"):
            text = brief.compose(
                run_id=1, slug="calm_otter", profile={"name": name},
                mission="m", requester="human", root=Path("/p"), workdir="/p")
            self.assertIn("Never run git write commands", text, name)
            self.assertIn("host checkpoints", text, name)
            self.assertIn("EPERM", text, name)
            self.assertIn("worktree yes", text, name)
            self.assertIn(".git no", text, name)
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
