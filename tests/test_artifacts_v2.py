import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestra import artifacts


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "report.md").write_text("# Report\n", encoding="utf-8")
        self.home = patch.dict(os.environ, {"ORCHESTRA_HOME": str(self.root / "state")})
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.temp.cleanup()

    def test_publication_is_a_content_addressed_immutable_copy(self):
        item = artifacts.stage(4, self.work, "report.md", artifact_id="a1")
        self.assertEqual(item.media_type, "text/markdown")
        self.assertEqual(item.size, 9)
        self.assertEqual(Path(item.stored_path).read_text(), "# Report\n")
        (self.work / "report.md").write_text("changed", encoding="utf-8")
        self.assertEqual(Path(item.stored_path).read_text(), "# Report\n")

    def test_symlink_and_parent_escape_are_rejected(self):
        (self.work / "link").symlink_to(self.work / "report.md")
        for value in ("link", "../work/report.md"):
            with self.subTest(value=value), self.assertRaises(artifacts.ArtifactError):
                artifacts.stage(1, self.work, value)

    def test_intermediate_symlink_swap_cannot_redirect_open_file(self):
        nested = self.work / "nested"
        nested.mkdir()
        (nested / "report.md").write_text("trusted\n", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "report.md").write_text("redirected\n", encoding="utf-8")
        displaced = self.work / "displaced"
        real_open = os.open
        swapped = False

        def swap_after_open(path, flags, *args, **kwargs):
            nonlocal swapped
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == "nested" and kwargs.get("dir_fd") is not None and not swapped:
                nested.rename(displaced)
                nested.symlink_to(outside, target_is_directory=True)
                swapped = True
            return descriptor

        with patch.object(artifacts.os, "open", side_effect=swap_after_open):
            item = artifacts.stage(
                1, self.work, "nested/report.md", artifact_id="swap-after")
        self.assertEqual(Path(item.stored_path).read_text(), "trusted\n")

    def test_intermediate_symlink_installed_before_open_is_rejected(self):
        nested = self.work / "nested"
        nested.mkdir()
        (nested / "report.md").write_text("trusted\n", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "report.md").write_text("redirected\n", encoding="utf-8")
        displaced = self.work / "displaced"
        real_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "nested" and kwargs.get("dir_fd") is not None and not swapped:
                nested.rename(displaced)
                nested.symlink_to(outside, target_is_directory=True)
                swapped = True
            return real_open(path, flags, *args, **kwargs)

        with patch.object(artifacts.os, "open", side_effect=swap_before_open), \
                self.assertRaises(artifacts.ArtifactError):
            artifacts.stage(1, self.work, "nested/report.md", artifact_id="swap-before")

    def test_ranges_cover_full_prefix_and_suffix_forms(self):
        self.assertIsNone(artifacts.byte_range(10, None))
        self.assertEqual(artifacts.byte_range(10, "bytes=2-5"), (2, 5))
        self.assertEqual(artifacts.byte_range(10, "bytes=7-"), (7, 9))
        self.assertEqual(artifacts.byte_range(10, "bytes=-3"), (7, 9))


if __name__ == "__main__":
    unittest.main()
