"""CONTRACT §7 Enforcement: the import graph is the test.

Nothing in Orchestra outside a source's ADAPTER may know that source's
routes, schema, or storage. The rule is as old as the contract; nothing
enforced it, so ten modules ended up importing the Work client and the git
landing path grew a line that posts a Work fact.

This was a RATCHET while the repair ran: ``LEAKING`` held every module that
still imported the client, and could only shrink. It emptied at schema v25,
when the daemon stopped building a source client to hand to its adapter
passes, and the ratchet deleted itself as its own docstring instructed.
What is left is the permanent rule, asserted once.
"""
import ast
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "orchestra"

# The Work adapter. These five are allowed to name a source; being the
# adapter is their whole job (CONTRACT §7).
ADAPTER = {"sweeper", "conductor", "verify", "refine", "findings"}


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
    def test_only_the_adapter_imports_the_work_client(self):
        found = {p.stem for p in CORE.glob("*.py")
                 if p.stem != "work_client" and _imports_work_client(p)}
        self.assertEqual(found - ADAPTER, set(),
                         f"new Work coupling in {sorted(found - ADAPTER)}: the "
                         "core reaches a source only through its adapter "
                         "(CONTRACT §7 Enforcement)")


if __name__ == "__main__":
    unittest.main()
