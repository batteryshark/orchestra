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

# The Nod client's importers, frozen. This is a RATCHET, like the Work one
# was: Nod is a specific external notification product, three of these are
# core modules (merge, messaging, observer), and nothing else may join the
# list — shrink it, never grow it. Every caller already degrades to None
# through ``nod.from_cfg``; the next step is fewer importers, not more.
NOD_IMPORTERS = {"cli", "conductor", "daemon", "merge", "messaging",
                 "observer", "profile_edit", "sweeper"}


def _imports(path: Path, module: str) -> bool:
    tree = ast.parse(path.read_text(), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == module or a.name.endswith(f".{module}")
                   for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == module or mod.endswith(f".{module}"):
                return True
            if mod == "orchestra" and any(a.name == module
                                          for a in node.names):
                return True
    return False


def _importers(module: str) -> set[str]:
    return {p.stem for p in CORE.glob("*.py")
            if p.stem != module and _imports(p, module)}


class BoundaryTests(unittest.TestCase):
    def test_only_the_adapter_imports_the_work_client(self):
        found = _importers("work_client")
        self.assertEqual(found - ADAPTER, set(),
                         f"new Work coupling in {sorted(found - ADAPTER)}: the "
                         "core reaches a source only through its adapter "
                         "(CONTRACT §7 Enforcement)")

    def test_the_nod_client_gains_no_new_importers(self):
        found = _importers("nod")
        self.assertEqual(found - NOD_IMPORTERS, set(),
                         f"new Nod coupling in {sorted(found - NOD_IMPORTERS)}: "
                         "the human-loop client is a specific external "
                         "product; route through an existing importer, or "
                         "shrink this set — never grow it")


if __name__ == "__main__":
    unittest.main()
