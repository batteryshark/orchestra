"""The source boundary became structure: the adapter LEFT the repository.

The old rule allowed five adapter modules to know the one source.
The eviction moved them to the sibling work-bridge project — a consumer of
Orchestra's library and API — so the rule is now absolute: nothing in
``orchestra/`` names a source, imports a source client, or reads a
``[work]`` config table. Orchestra is a runner; every caller is a caller.
"""
import ast
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "orchestra"

# The modules the eviction removed. Their return, under any of these names,
# is the coupling growing back.
EVICTED = {"work_client", "sweeper", "conductor", "verify", "findings",
           "router", "refine"}

# The Nod client's importers, frozen. This is a RATCHET: Nod is a specific
# external notification product, three of these are core modules (merge,
# messaging, observer), and nothing else may join the list — shrink it,
# never grow it. Every caller already degrades to None through
# ``nod.from_cfg``; the next step is fewer importers, not more.
NOD_IMPORTERS = {"cli", "daemon", "merge", "messaging", "observer",
                 "profile_edit"}


def _imports(path: Path, names: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(), str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.rsplit(".", 1)[-1] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").rsplit(".", 1)[-1]
            found.add(mod)
            if (node.module or "").startswith("orchestra") or node.level:
                found |= {a.name for a in node.names}
    return found & names


class BoundaryTests(unittest.TestCase):
    def test_the_evicted_modules_stay_evicted(self):
        present = {p.stem for p in CORE.glob("*.py")} & EVICTED
        self.assertEqual(present, set(),
                         f"{sorted(present)} came back: the source automation "
                         "lives in work-bridge, not here")

    def test_nothing_imports_an_evicted_name(self):
        found = {p.stem: sorted(_imports(p, EVICTED))
                 for p in CORE.glob("*.py")}
        offending = {stem: names for stem, names in found.items() if names}
        self.assertEqual(offending, {},
                         "the core reaches no source and hosts no adapter; "
                         "a caller integrates through the library and API")

    def test_no_module_reads_a_work_config_table(self):
        offending = [p.name for p in CORE.glob("*.py")
                     if 'get("work")' in p.read_text()
                     or "['work']" in p.read_text()
                     or '["work"]' in p.read_text()]
        self.assertEqual(offending, [],
                         "a [work] table is the bridge's to read, not the "
                         "runner's")

    def test_the_nod_client_gains_no_new_importers(self):
        found = {p.stem for p in CORE.glob("*.py")
                 if p.stem != "nod" and _imports(p, {"nod"})}
        self.assertEqual(found - NOD_IMPORTERS, set(),
                         f"new Nod coupling in {sorted(found - NOD_IMPORTERS)}: "
                         "the human-loop client is a specific external "
                         "product; route through an existing importer, or "
                         "shrink this set — never grow it")


if __name__ == "__main__":
    unittest.main()
