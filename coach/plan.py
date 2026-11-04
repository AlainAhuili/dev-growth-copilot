"""Deterministic part of the Coach: which area to practise, from which findings, and when
to come back. The model only writes exercises and feedback; it never decides any of this."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

# How much a finding says about a weakness. Real defects weigh more than learning notes.
SEVERITY_WEIGHT = {"critical": 3.0, "major": 2.0, "minor": 1.0, "info": 0.5}
ISO = "%Y-%m-%dT%H:%M:%SZ"


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime(ISO)


def area_weights(conn) -> dict[str, float]:
    rows = conn.execute(
        """SELECT c.skill_area, f.severity, COUNT(*)
           FROM findings f JOIN categories c ON c.name = f.category
           GROUP BY c.skill_area, f.severity"""
    ).fetchall()
    weights: dict[str, float] = {}
    for area, severity, n in rows:
        weights[area] = weights.get(area, 0.0) + SEVERITY_WEIGHT.get(severity, 0.5) * n
    return weights


def due_areas(conn, now: datetime | None = None) -> dict[str, float]:
    """Areas with findings that are due (never practised, or next_due reached)."""
    t = now_iso(now)
    scheduled = dict(conn.execute("SELECT skill_area, next_due FROM skill_schedule").fetchall())
    return {a: w for a, w in area_weights(conn).items()
            if a not in scheduled or scheduled[a] <= t}


def pick_area(conn, now: datetime | None = None) -> str | None:
    """Weakest due area; ties broken by name so the choice is reproducible."""
    due = due_areas(conn, now)
    if not due:
        return None
    return sorted(due.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def source_findings(conn, area: str, limit: int = 3) -> list[dict]:
    """The most serious, then most recent, findings of an area: the exercise material."""
    rows = conn.execute(
        """SELECT f.id, f.repo, f.file, f.line, f.category, f.severity, f.message
           FROM findings f JOIN categories c ON c.name = f.category
           WHERE c.skill_area = ?
           ORDER BY CASE f.severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1
                                    WHEN 'minor' THEN 2 ELSE 3 END, f.id DESC
           LIMIT ?""",
        (area, limit),
    ).fetchall()
    keys = ("id", "repo", "file", "line", "category", "severity", "message")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def open_practice(conn) -> dict | None:
    row = conn.execute(
        "SELECT id, skill_area, title, exercise FROM practice WHERE status = 'open' "
        "ORDER BY id LIMIT 1").fetchone()
    return dict(zip(("id", "skill_area", "title", "exercise"), row, strict=True)) if row else None


def start_practice(conn, area: str, findings: list[dict], title: str, exercise: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO practice (skill_area, source_findings, title, exercise) "
            "VALUES (?, ?, ?, ?)",
            (area, json.dumps([f["id"] for f in findings]), title, exercise))
    return int(cur.lastrowid)


def next_interval(previous_days: float, score: int) -> float:
    """SM-2-lite: a good answer pushes the area out, a weak one brings it back tomorrow."""
    if score < 3:
        return 1.0
    return round(previous_days * (1.5 + 0.5 * (score - 3)), 2)


def finish_practice(conn, practice_id: int, score: int, feedback: str,
                    now: datetime | None = None) -> datetime:
    """Store the result and reschedule the area. Returns when the area is due again."""
    score = max(0, min(5, int(score)))
    row = conn.execute("SELECT skill_area, status FROM practice WHERE id = ?",
                       (practice_id,)).fetchone()
    if row is None:
        raise ValueError(f"no practice {practice_id}")
    area, status = row
    if status != "open":
        raise ValueError(f"practice {practice_id} is already done")
    t = now or datetime.now(UTC)
    prev = conn.execute("SELECT interval_days FROM skill_schedule WHERE skill_area = ?",
                        (area,)).fetchone()
    interval = next_interval(prev[0] if prev else 1.0, score)
    due = t + timedelta(days=interval)
    with conn:
        conn.execute(
            "UPDATE practice SET status = 'done', score = ?, feedback = ?, answered_at = ? "
            "WHERE id = ?", (score, feedback, now_iso(t), practice_id))
        conn.execute(
            """INSERT INTO skill_schedule (skill_area, interval_days, next_due) VALUES (?, ?, ?)
               ON CONFLICT(skill_area) DO UPDATE SET interval_days = excluded.interval_days,
                                                     next_due = excluded.next_due""",
            (area, interval, now_iso(due)))
    return due
