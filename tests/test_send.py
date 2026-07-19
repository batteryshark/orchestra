from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

from orchestra_cli import cli, db


class SendFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestra").mkdir()
        db.connect(self.root).close()

        self.original_find_root = cli.paths.find_root
        self.original_config_load = cli.config.load
        cli.paths.find_root = lambda: self.root  # type: ignore[assignment]
        cli.config.load = lambda _root: {  # type: ignore[assignment]
            "settings": {"default_requester": "orchestrator"}
        }

    def tearDown(self) -> None:
        cli.paths.find_root = self.original_find_root  # type: ignore[assignment]
        cli.config.load = self.original_config_load  # type: ignore[assignment]
        self.tmp.cleanup()

    def _run_main(self, argv: list[str]) -> tuple[int, str, str]:
        original_argv = sys.argv
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        sys.argv = ["orchestra", *argv]
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                cli.main()
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
            if not isinstance(exc.code, int) and exc.code:
                print(exc.code, file=stderr)
        finally:
            sys.argv = original_argv
        return code, stdout.getvalue(), stderr.getvalue()

    def _messages(self) -> list[dict]:
        con = db.connect(self.root)
        try:
            return [dict(row) for row in con.execute("SELECT * FROM messages ORDER BY id")]
        finally:
            con.close()

    def test_file_sends_complete_large_utf8_message(self) -> None:
        body = ("Investigation finding: café\n" * 600) + "final conclusion\n"
        self.assertGreater(len(body.encode("utf-8")), 10_000)
        source = self.root / "investigation.md"
        source.write_text(body, encoding="utf-8")

        code, stdout, stderr = self._run_main(
            ["send", "reviewer", "--file", str(source), "--as", "researcher"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("sent researcher -> reviewer", stdout)
        messages = self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["body"], body)

    def test_inline_body_remains_supported(self) -> None:
        code, _, stderr = self._run_main(
            ["send", "reviewer", "inline handoff", "--as", "researcher"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(self._messages()[0]["body"], "inline handoff")

    def test_inline_body_and_file_are_mutually_exclusive(self) -> None:
        source = self.root / "handoff.md"
        source.write_text("file handoff", encoding="utf-8")

        code, _, stderr = self._run_main(
            ["send", "reviewer", "inline handoff", "--file", str(source)]
        )

        self.assertEqual(code, 2)
        self.assertIn("not allowed with argument", stderr)
        self.assertEqual(self._messages(), [])

    def test_unreadable_file_reports_path_without_inserting(self) -> None:
        missing = self.root / "missing.md"

        code, _, stderr = self._run_main(
            ["send", "reviewer", "--file", str(missing)]
        )

        self.assertEqual(code, 1)
        self.assertIn(f"cannot read message file '{missing}'", stderr)
        self.assertEqual(self._messages(), [])


if __name__ == "__main__":
    unittest.main()
