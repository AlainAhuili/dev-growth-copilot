"""Skill map storage: forward-only SQL migrations plus a small query API."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@dataclass(frozen=True)
class Finding:
    agent: str
    agent_version: str
    stage: str
    repo: str
    category: str
    severity: str
    message: str
    commit_sha: str | None = None
    file: str | None = None
    line: int | None = None
    created_at: str | None = None  # when it was found (ISO-8601 UTC); default: now


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every migration newer than the current version. Returns the versions applied."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]

    applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version <= current:
            continue
        with conn:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        applied.append(version)
    return applied


def record_finding(conn: sqlite3.Connection, finding: Finding) -> int:
    with conn:
        cur = conn.execute(
            """INSERT INTO findings
               (agent, agent_version, stage, repo, commit_sha, category, severity,
                file, line, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))""",
            (
                finding.agent,
                finding.agent_version,
                finding.stage,
                finding.repo,
                finding.commit_sha,
                finding.category,
                finding.severity,
                finding.file,
                finding.line,
                finding.message,
                finding.created_at,
            ),
        )
    return cur.lastrowid


def mistake_counts(conn: sqlite3.Connection, since: str | None = None) -> dict[str, int]:
    """Findings per skill area, most frequent first. `since` is an ISO-8601 timestamp."""
    rows = conn.execute(
        """SELECT c.skill_area, COUNT(*) AS n
           FROM findings f JOIN categories c ON c.name = f.category
           WHERE (? IS NULL OR f.created_at >= ?)
           GROUP BY c.skill_area
           ORDER BY n DESC, c.skill_area""",
        (since, since),
    ).fetchall()
    return dict(rows)
