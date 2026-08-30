"""Structural ratchets for Orchestra's clean fleet-runner boundary."""
import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "orchestra"
CLIENTS = (CORE / "dashboard.html", *sorted((ROOT / "ios" / "Orchestra").glob("*.swift")))
REMOVED_MODULES = {
    "dispatch", "handoff", "hooks", "instrumentation", "merge", "nod",
    "profile_edit", "project", "resolver", "review",
}
FORBIDDEN_RUNTIME_TERMS = (
    "slash work", "workbridge", "nod", "handoff", "landing",
    "control seat", "federation", "project_id",
)
LEGACY_WORK_ID = re.compile(r"\b[WI]-\d{4}\b", re.I)


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").rsplit(".", 1)[-1])
            if (node.module or "").startswith("orchestra") or node.level:
                found.update(alias.name for alias in node.names)
    return found


class BoundaryTests(unittest.TestCase):
    def test_retired_subsystems_are_physically_absent_and_unimported(self):
        present = {path.stem for path in CORE.glob("*.py")} & REMOVED_MODULES
        self.assertEqual(present, set())
        imported = {
            path.name: sorted(imported_names(path) & REMOVED_MODULES)
            for path in CORE.glob("*.py")
        }
        self.assertEqual({name: hits for name, hits in imported.items() if hits}, {})

    def test_active_runtime_and_clients_have_no_workflow_product_vocabulary(self):
        # migration.py is an explicit offline reader for retiring v1 state; it
        # never enters the daemon, API, scheduler, or clients.
        files = [path for path in CORE.glob("*.py") if path.name != "migration.py"]
        files.extend(CLIENTS)
        offending = {}
        for path in files:
            text = path.read_text(encoding="utf-8").lower()
            hits = [term for term in FORBIDDEN_RUNTIME_TERMS if re.search(
                rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text)]
            if LEGACY_WORK_ID.search(text):
                hits.append("legacy work-item id")
            if hits:
                offending[str(path.relative_to(ROOT))] = hits
        self.assertEqual(offending, {})

    def test_schema_has_only_runner_control_plane_concepts(self):
        schema = (CORE / "db.py").read_text(encoding="utf-8").lower()
        for term in (*FORBIDDEN_RUNTIME_TERMS, "projects", "source_claim",
                     "writeback", "acceptance_gate"):
            self.assertNotIn(term, schema)


if __name__ == "__main__":
    unittest.main()
