"""CONTRACT §7 Enforcement: the import graph is the test.

Nothing in Orchestra outside a source's ADAPTER may know that source's
routes, schema, or storage. The rule is as old as the contract; nothing
enforced it, so ten modules ended up importing the Work client and the git
landing path grew a line that posts a Work fact.

This is a RATCHET, not a snapshot. ``ADAPTER`` is the set allowed to know
Work forever. ``LEAKING`` is the set that still does and must not grow: every
name removed from it is a boundary repaired, and a name added back fails the
suite. When ``LEAKING`` is empty, delete it and the assertion that reads it.
"""
import ast
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "orchestra"

# The Work adapter. These five are allowed to name a source; being the
# adapter is their whole job (CONTRACT §7).
ADAPTER = {"sweeper", "conductor", "verify", "refine", "findings"}

# Every other module that imports the Work client today. Shrink only.
LEAKING = {"daemon", "merge", "messaging", "profile_edit", "project"}


def _imports_work_client(path: Path) -> bool:
    tree = ast.parse(path.read_text(), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.endswith("work_client") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith("work_client"):
                return True
            if (node.module or "") == "orchestra" and any(
                    a.name == "work_client" for a in node.names):
                return True
    return False


class BoundaryTests(unittest.TestCase):
    def test_only_the_adapter_and_known_leaks_import_the_work_client(self):
        found = {p.stem for p in CORE.glob("*.py")
                 if p.stem != "work_client" and _imports_work_client(p)}
        unexpected = found - ADAPTER - LEAKING
        self.assertEqual(unexpected, set(),
                         f"new Work coupling in {sorted(unexpected)}: the core "
                         "reaches a source only through its adapter "
                         "(CONTRACT §7 Enforcement)")

    def test_the_leak_list_does_not_go_stale(self):
        """A repaired module must leave LEAKING, or the ratchet stops biting."""
        found = {p.stem for p in CORE.glob("*.py")
                 if p.stem != "work_client" and _imports_work_client(p)}
        self.assertEqual(LEAKING - found, set(),
                         "these no longer import the Work client — delete them "
                         "from LEAKING so it cannot creep back")


if __name__ == "__main__":
    unittest.main()
