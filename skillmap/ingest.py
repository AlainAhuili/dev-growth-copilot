"""Import a review-findings JSON (from reviewer.cli) into the local skill map.

    python -m skillmap.ingest findings.json [--db ~/.dgc/skillmap.db]

Idempotent: importing the same file twice records each finding once.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from skillmap.db import Finding, connect, migrate, record_finding

DEFAULT_DB = Path.home() / ".dgc" / "skillmap.db"


def ingest(conn, report: dict, across_commits: bool = False) -> tuple[int, int, int]:
    """Returns (recorded, already_present, rejected).

    By default a finding is a duplicate only within the same commit. With
    `across_commits`, the same file, line and category in the same repo is one finding
    whatever the commit: re-reviews of a PR repeat notes on unchanged code, and counting
    each repeat would inflate that skill area.
    """
    known = {r[0] for r in conn.execute("SELECT name FROM categories")}
    recorded = present = rejected = 0
    for f in report.get("findings", []):
        if f.get("category") not in known or not f.get("message"):
            rejected += 1
            continue
        if across_commits:
            exists = conn.execute(
                """SELECT 1 FROM findings WHERE repo = ? AND file IS ? AND line IS ?
                   AND category = ? LIMIT 1""",
                (str(report.get("repo", "unknown")), f.get("file"), f.get("line"),
                 f["category"]),
            ).fetchone()
        else:
            exists = conn.execute(
                """SELECT 1 FROM findings WHERE commit_sha IS ? AND file IS ? AND line IS ?
                   AND category = ? LIMIT 1""",
                (report.get("commit_sha"), f.get("file"), f.get("line"), f["category"]),
            ).fetchone()
        if exists:
            present += 1
            continue
        try:
            record_finding(conn, Finding(
                agent="reviewer",
                agent_version=str(report.get("reviewer_version", "unknown")),
                stage="commit",
                repo=str(report.get("repo", "unknown")),
                commit_sha=report.get("commit_sha"),
                category=f["category"],
                severity=f.get("severity", "info"),
                file=f.get("file"),
                line=f.get("line"),
                message=str(f["message"])[:2000],
            ))
            recorded += 1
        except Exception:  # noqa: BLE001 - a bad row (e.g. unknown severity) must not stop the rest
            rejected += 1
    return recorded, present, rejected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    migrate(conn)
    for path in args.files:
        recorded, present, rejected = ingest(conn, json.loads(path.read_text(encoding="utf-8")))
        print(f"{path}: {recorded} recorded, {present} already present, {rejected} rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
