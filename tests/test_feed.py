from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestra_cli import cli, db


class FeedCommandTests(unittest.TestCase):
    def test_feed_prints_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestra").mkdir()
            con = db.connect(root)
            con.executemany(
                "INSERT INTO feed(author, body, tags, created_at) VALUES(?,?,?,?)",
                [
                    ("worker", "oldest", "test", "2026-07-18T22:00:00Z"),
                    ("worker", "middle", "test", "2026-07-18T22:01:00Z"),
                    ("orchestra", "newest", "run", "2026-07-18T22:02:00Z"),
                ],
            )
            con.commit()
            con.close()

            output = io.StringIO()
            args = argparse.Namespace(tag=None, limit=25)
            with mock.patch.object(cli.paths, "find_root", return_value=root), \
                    contextlib.redirect_stdout(output):
                cli.cmd_feed(args)

            self.assertEqual(
                [line.split(": ", 1)[1].split(" [", 1)[0]
                 for line in output.getvalue().splitlines()],
                ["newest", "middle", "oldest"],
            )


if __name__ == "__main__":
    unittest.main()
