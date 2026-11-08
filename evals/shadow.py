"""Shadow mode: compare a candidate reviewer with the released one on the same corpus.

    python -m evals.shadow baseline.json candidate.json [--summary shadow.md]

Both files are `evals.runner --report` outputs for the same suite. Exit 1 when the
candidate regresses (see `shadow:` in thresholds.yaml), 2 when the reports can't be
compared. Deterministic: it only reads the two reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from evals.runner import EVALS_DIR

# Worst-run metrics, and which direction is better.
HIGHER_IS_BETTER = {"catch_rate", "case_pass_rate"}
SHOWN = ["catch_rate", "false_positive_rate", "case_pass_rate", "avg_latency_s",
         "avg_cost_usd", "notes"]


class ShadowError(ValueError):
    pass


def _pass_counts(report: dict) -> dict[str, tuple[int, int]]:
    """case id -> (runs passed, runs)."""
    runs = report.get("runs") or []
    counts: dict[str, int] = {}
    for run in runs:
        for c in run.get("cases", []):
            counts[c["id"]] = counts.get(c["id"], 0) + int(bool(c.get("passed")))
    return {cid: (k, len(runs)) for cid, k in counts.items()}


def _case_status(base: tuple[int, int], cand: tuple[int, int]) -> str:
    (bk, bn), (ck, cn) = base, cand
    if bk == bn and ck == 0:
        return "regressed"
    if bk == 0 and ck == cn:
        return "fixed"
    b, c = bk / bn, ck / cn
    return "worse" if c < b else "better" if c > b else "same"


def compare(base: dict, cand: dict, limits: dict) -> dict:
    if base.get("suite") != cand.get("suite"):
        raise ShadowError(f"different suites: {base.get('suite')} vs {cand.get('suite')}")
    if not base.get("runs") or not cand.get("runs"):
        raise ShadowError("a report has no runs")
    bd, cd = base.get("case_digests"), cand.get("case_digests")
    b_counts, c_counts = _pass_counts(base), _pass_counts(cand)
    cases, changed = [], []
    for cid in sorted(set(b_counts) & set(c_counts)):
        if bd and cd and bd.get(cid) != cd.get(cid):
            changed.append(cid)
            continue
        cases.append({"id": cid, "baseline": b_counts[cid], "candidate": c_counts[cid],
                      "status": _case_status(b_counts[cid], c_counts[cid])})

    bw, cw = base.get("worst", {}), cand.get("worst", {})
    metrics = {m: (bw[m], cw[m]) for m in SHOWN if m in bw and m in cw}

    failures = [f"{c['id']}: passed {c['baseline'][0]}/{c['baseline'][1]} runs before, "
                f"{c['candidate'][0]}/{c['candidate'][1]} now" for c in cases
                if c["status"] == "regressed"]
    if "catch_rate" in metrics:
        b, c = metrics["catch_rate"]
        if b - c > limits["max_catch_rate_drop"]:
            failures.append(f"catch_rate fell {b:.3f} -> {c:.3f} "
                            f"(max drop {limits['max_catch_rate_drop']})")
    if "false_positive_rate" in metrics:
        b, c = metrics["false_positive_rate"]
        if c - b > limits["max_false_positive_rate_rise"]:
            failures.append(f"false_positive_rate rose {b:.3f} -> {c:.3f} "
                            f"(max rise {limits['max_false_positive_rate_rise']})")
    for metric, key, floor in (("avg_cost_usd", "max_cost_ratio", "min_cost_increase_usd"),
                               ("avg_latency_s", "max_latency_ratio",
                                "min_latency_increase_s")):
        if metric in metrics:
            b, c = metrics[metric]
            if b > 0 and c / b > limits[key] and c - b >= limits[floor]:
                failures.append(f"{metric} {b:.4g} -> {c:.4g} is x{c / b:.2f} "
                                f"(max x{limits[key]})")

    return {
        "suite": cand["suite"],
        "repeats": (len(base["runs"]), len(cand["runs"])),
        "metrics": metrics,
        "cases": cases,
        "changed": changed,
        "new": sorted(set(c_counts) - set(b_counts)),
        "removed": sorted(set(b_counts) - set(c_counts)),
        "digests_known": bool(bd and cd),
        "failures": failures,
    }


def _better(metric: str, b: float, c: float) -> str:
    if b == c:
        return "="
    up = c > b
    return "better" if up == (metric in HIGHER_IS_BETTER) else "worse"


def render(result: dict) -> str:
    """Markdown for the job summary. Case ids are corpus directory names (repo content)."""
    br, cr = result["repeats"]
    md = [f"## Shadow mode: `{result['suite']}`",
          f"Released reviewer ({br} runs) vs candidate ({cr} runs), worst run of each.", "",
          "| Metric | Released | Candidate | |", "|---|---|---|---|"]
    md += [f"| {m} | {b:.4g} | {c:.4g} | {_better(m, b, c)} |"
           for m, (b, c) in result["metrics"].items()]
    moved = [c for c in result["cases"] if c["status"] != "same"]
    md += ["", "### Cases that moved"]
    md += [f"- `{c['id']}`: {c['baseline'][0]}/{c['baseline'][1]} -> "
           f"{c['candidate'][0]}/{c['candidate'][1]} **{c['status']}**" for c in moved] \
        or ["- None: every case passed as often as before."]
    for key, label in (("changed", "Changed in this PR (not compared)"),
                       ("new", "New cases (no baseline)"), ("removed", "Removed cases")):
        if result[key]:
            md += ["", f"### {label}", ", ".join(f"`{c}`" for c in result[key])]
    if not result["digests_known"]:
        md += ["", "_The released runner wrote no case digests: cases are compared by name, "
               "so a case edited in this PR may be compared unfairly._"]
    md += ["", "### Gate"]
    md += [f"- FAILED: {f}" for f in result["failures"]] or ["- No regression."]
    return "\n".join(md) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--summary", type=Path, help="append the Markdown report here")
    args = parser.parse_args(argv)

    limits = yaml.safe_load((EVALS_DIR / "thresholds.yaml").read_text(encoding="utf-8"))
    try:
        base, cand = (json.loads(p.read_text(encoding="utf-8"))
                      for p in (args.baseline, args.candidate))
        result = compare(base, cand, limits["shadow"])
    except (ShadowError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2
    text = render(result)
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(text)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
