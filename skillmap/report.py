"""Show what the skill map says you should practise.

    python -m skillmap.report [--db ~/.dgc/skillmap.db] [--since 2026-11-01]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from skillmap.db import connect, migrate, mistake_counts
from skillmap.ingest import DEFAULT_DB


def top_categories(conn, since: str | None = None, limit: int = 5) -> list[tuple[str, str, int]]:
    """(category, skill area, count) for findings above note severity, most frequent first."""
    return conn.execute(
        """SELECT f.category, c.skill_area, COUNT(*) AS n
           FROM findings f JOIN categories c ON c.name = f.category
           WHERE f.severity IN ('major', 'critical') AND (? IS NULL OR f.created_at >= ?)
           GROUP BY f.category ORDER BY n DESC, f.category LIMIT ?""",
        (since, since, limit),
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--since", help="ISO date, e.g. 2026-11-01")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    migrate(conn)
    counts = mistake_counts(conn, args.since)
    if not counts:
        print("No findings recorded yet. Import some with `python -m skillmap.ingest`.")
        return 0
    print("Findings per skill area (all severities):")
    width = max(counts.values())
    for area, n in counts.items():
        print(f"  {area:<12} {'#' * round(20 * n / width):<20} {n}")
    top = top_categories(conn, args.since)
    if top:
        print("\nPractise next (major/critical, most frequent):")
        for category, area, n in top:
            print(f"  {category:<16} ({area}) x{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
