"""Deterministic part of the weekly report: this week vs last week per skill area, practice
results, optional DORA metrics, and which area to focus on next week. The model only
explains the numbers; it never picks the area."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from coach import plan

# DORA metrics as the pipeline (or you) reports them. Lower is better except deploys.
DORA_KEYS = {
    "deployments_per_week": "Deployment frequency (per week)",
    "lead_time_hours": "Lead time for changes (hours)",
    "change_failure_rate": "Change failure rate (0-1)",
    "time_to_restore_hours": "Time to restore (hours)",
}


def _window_weights(conn, start: str, end: str) -> dict[str, tuple[float, int]]:
    rows = conn.execute(
        """SELECT c.skill_area, f.severity, COUNT(*)
           FROM findings f JOIN categories c ON c.name = f.category
           WHERE f.created_at >= ? AND f.created_at < ?
           GROUP BY c.skill_area, f.severity""", (start, end)).fetchall()
    out: dict[str, tuple[float, int]] = {}
    for area, severity, n in rows:
        w, count = out.get(area, (0.0, 0))
        out[area] = (w + plan.SEVERITY_WEIGHT.get(severity, 0.5) * n, count + n)
    return out


def week_stats(conn, now: datetime) -> dict:
    """Findings and practice for the 7 days before `now`, compared with the 7 before."""
    t, w1, w2 = plan.now_iso(now), plan.now_iso(now - timedelta(days=7)), \
        plan.now_iso(now - timedelta(days=14))
    this, last = _window_weights(conn, w1, t), _window_weights(conn, w2, w1)
    areas = []
    for a in sorted(set(this) | set(last)):
        tw, tn = this.get(a, (0.0, 0))
        lw, ln = last.get(a, (0.0, 0))
        areas.append({"area": a, "weight": tw, "findings": tn,
                      "last_weight": lw, "last_findings": ln, "change": tw - lw})
    areas.sort(key=lambda r: (-r["weight"], -r["change"], r["area"]))
    practice = [
        {"area": a, "sessions": n, "avg_score": round(avg, 1)}
        for a, n, avg in conn.execute(
            """SELECT skill_area, COUNT(*), AVG(score) FROM practice
               WHERE status = 'done' AND answered_at >= ? AND answered_at < ?
               GROUP BY skill_area ORDER BY skill_area""", (w1, t)).fetchall()]
    return {"from": w1, "to": t, "areas": areas, "practice": practice}


def pick_focus(conn, stats: dict, now: datetime) -> tuple[str | None, str]:
    """(area, reason). Most weight this week (ties: biggest rise, then name); with no
    findings this week, the area the practice schedule would pick."""
    current = [r for r in stats["areas"] if r["weight"] > 0]
    if current:
        top = current[0]
        if top["change"] > 0:
            return top["area"], (f"most findings this week ({top['findings']}, weight "
                                 f"{top['weight']:.1f}), up from {top['last_weight']:.1f}")
        return top["area"], (f"most findings this week ({top['findings']}, weight "
                             f"{top['weight']:.1f})")
    area = plan.pick_area(conn, now)
    if area:
        return area, "no findings this week; weakest area that is due for practice"
    return None, "no findings this week and no area is due for practice"


def load_dora(path: Path) -> dict[str, float]:
    """A JSON object with any of DORA_KEYS; numbers >= 0. Raises ValueError otherwise."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("DORA file must be a JSON object")
    unknown = sorted(set(data) - set(DORA_KEYS))
    if unknown:
        raise ValueError(f"unknown DORA keys: {', '.join(unknown)} "
                         f"(expected {', '.join(DORA_KEYS)})")
    out = {}
    for k, v in data.items():
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) \
                or v < 0:
            raise ValueError(f"{k} must be a number >= 0")
        if k == "change_failure_rate" and v > 1:
            raise ValueError("change_failure_rate is a fraction between 0 and 1")
        out[k] = float(v)
    return out


def trend(change: float) -> str:
    return "up" if change > 0 else "down" if change < 0 else "same"
