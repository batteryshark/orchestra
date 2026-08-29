"""Core-test seeding helpers: identity + history, no adapter anywhere."""
from orchestra import db


def seed_project(con, project_id: str, root, slug: str = "demo",
                 name: str = "Demo") -> None:
    """Register an identity and teach the run history that ``root`` is where
    it runs — the core's whole resolution story (schema v29)."""
    con.execute(
        "INSERT OR IGNORE INTO projects(project_id, slug, name, local, "
        "refreshed_at) VALUES(?,?,?,1,?)",
        (project_id, slug, name, db.now()))
    con.execute(
        "INSERT INTO runs(profile, backend, requested_by, workdir, "
        "project_id, repo, status, started_at, project_seq) "
        f"VALUES('seed','codex','seed',?,?,?,'done',?,{db.NEXT_PROJECT_SEQ})",
        (str(root), project_id, str(root), db.now(), project_id))
    con.commit()
