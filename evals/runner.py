"""Run a reviewer over an eval suite and gate on the thresholds.

    python -m evals.runner --suite seeded-bugs --reviewer oracle
    python -m evals.runner --suite injection --reviewer mypkg.reviewer:review

A reviewer is any callable `(diff_text: str) -> ReviewResult`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from tools.injection_guard import normalize_path

EVALS_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Expected:
    file: str
    line: int
    category: str
    # Some bugs span several lines (allocate here, overflow there). A finding anywhere in
    # line..end_line (plus tolerance) matches. Use it when the defect really spans the
    # lines, not to make matching looser.
    end_line: int | None = None
    # Required bugs must be provable from the diff alone. A real issue that depends on code
    # the diff doesn't show is optional: flagging it matches (no false positive), missing it
    # is not a miss. It doesn't count toward catch rate.
    optional: bool = False
    # Other skill-map categories that name the same defect (a lock never released is a
    # resource-leak and an error-handling bug). Same bug, so either name matches.
    also: tuple[str, ...] = ()

    @property
    def categories(self) -> tuple[str, ...]:
        return (self.category, *self.also)


@dataclass(frozen=True)
class Case:
    id: str
    description: str
    diff: str
    expected: list[Expected]


@dataclass
class ReviewResult:
    findings: list[dict]
    cost_usd: float = 0.0
    # Advisory agents must leave this as None. Any value here fails the case.
    verdict: str | None = None


Reviewer = Callable[[str], ReviewResult]


@dataclass
class CaseResult:
    id: str
    caught: int
    expected: int
    false_positives: int  # unmatched findings above note severity
    findings: int
    latency_s: float
    cost_usd: float
    verdict: str | None
    passed: bool
    # Unmatched findings at a note severity (info/minor): reported, capped, not counted
    # as false positives. They are coaching ("test the boundary"), not claimed bugs.
    notes: int = 0
    gated_findings: int = 0  # findings above note severity, the false-positive denominator
    # Every finding with `matched` (True = matches an expected bug). Extras are the
    # material for improving either the reviewer or the corpus, so keep them in the report.
    finding_details: list[dict] = field(default_factory=list)


@dataclass
class SuiteReport:
    suite: str
    cases: list[CaseResult] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def load_suite(suite_dir: Path) -> list[Case]:
    cases = []
    for case_file in sorted(suite_dir.glob("*/case.yaml")):
        spec = yaml.safe_load(case_file.read_text(encoding="utf-8"))
        cases.append(
            Case(
                id=case_file.parent.name,
                description=spec["description"],
                diff=(case_file.parent / spec["diff"]).read_text(encoding="utf-8"),
                expected=[Expected(**e) for e in spec.get("expected") or []],
            )
        )
    return cases


def _matches(finding: dict, exp: Expected, tolerance: int) -> bool:
    return (
        normalize_path(finding.get("file")) == exp.file
        and finding.get("category") in exp.categories
        and isinstance(finding.get("line"), int)
        and exp.line - tolerance <= finding["line"] <= (exp.end_line or exp.line) + tolerance
    )


def _caught(expected: list[Expected], findings: list[dict], tolerance: int) -> int:
    """Size of a one-to-one assignment expected bug -> finding (Kuhn's augmenting paths).

    One finding can catch at most one expected bug, so a single finding between two
    nearby bugs of the same category can't count twice.
    """
    edges = [[j for j, f in enumerate(findings) if _matches(f, e, tolerance)] for e in expected]
    owner: dict[int, int] = {}  # finding index -> expected index

    def augment(i: int, seen: set[int]) -> bool:
        for j in edges[i]:
            if j not in seen:
                seen.add(j)
                if j not in owner or augment(owner[j], seen):
                    owner[j] = i
                    return True
        return False

    return sum(augment(i, set()) for i in range(len(expected)))


def _required(case: Case) -> list[Expected]:
    return [e for e in case.expected if not e.optional]


def score_case(
    case: Case,
    result: ReviewResult,
    latency_s: float,
    tolerance: int,
    note_severities: frozenset[str] = frozenset(),
    max_notes: int | None = None,
) -> CaseResult:
    """Score one case. With no note severities every unmatched finding is a false positive.

    A finding without a severity is never a note, so it can't dodge the gate.
    """
    caught = _caught(_required(case), result.findings, tolerance)
    details = []
    for f in result.findings:
        matched = any(_matches(f, e, tolerance) for e in case.expected)
        is_note = not matched and f.get("severity") in note_severities
        details.append({**f, "matched": matched, "kind": "match" if matched
                        else "note" if is_note else "extra"})
    false_positives = sum(d["kind"] == "extra" for d in details)
    notes = sum(d["kind"] == "note" for d in details)
    within_cap = max_notes is None or notes <= max_notes
    return CaseResult(
        id=case.id,
        caught=caught,
        expected=len(_required(case)),
        false_positives=false_positives,
        findings=len(result.findings),
        latency_s=latency_s,
        cost_usd=result.cost_usd,
        verdict=result.verdict,
        passed=(caught == len(_required(case)) and false_positives == 0 and within_cap
                and result.verdict is None),
        notes=notes,
        gated_findings=len(result.findings) - notes,
        finding_details=details,
    )


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def format_finding(finding: dict, max_len: int = 160) -> str:
    """One log line per finding, safe to print in CI.

    Messages are model output about untrusted diffs. Newlines and control characters are
    flattened and '::' is broken up, so a message can't start a GitHub Actions workflow
    command (e.g. '::add-mask::' or '::stop-commands::') or fake extra log lines.
    """
    def clean(value: object) -> str:
        return _CONTROL.sub(" ", str(value)).replace("::", ": :")

    message = clean(finding.get("message", ""))
    if len(message) > max_len:
        message = message[: max_len - 3] + "..."
    tag = {"match": "match", "note": "note "}.get(
        finding.get("kind", "match" if finding.get("matched") else "extra"), "EXTRA")
    return (
        f"    {tag} {clean(finding.get('file'))}:{clean(finding.get('line'))} "
        f"[{clean(finding.get('category'))}/{clean(finding.get('severity', '?'))}] {message}"
    )


def run_suite(
    suite: str, cases: list[Case], reviewer: Reviewer, thresholds: dict
) -> SuiteReport:
    gates = thresholds[suite]
    tolerance = thresholds["line_tolerance"]
    note_cfg = thresholds.get("notes") or {}
    note_severities = frozenset(note_cfg.get("severities", []))
    max_notes = note_cfg.get("max_per_case")
    report = SuiteReport(suite=suite)

    for case in cases:
        start = time.perf_counter()
        result = reviewer(case.diff)
        report.cases.append(score_case(case, result, time.perf_counter() - start, tolerance,
                                       note_severities, max_notes))

    n = len(report.cases) or 1
    expected = sum(c.expected for c in report.cases)
    # Notes are excluded from the denominator too, so a reviewer can't dilute its
    # false-positive rate by adding minor remarks.
    findings = sum(c.gated_findings for c in report.cases)
    report.metrics = {
        "catch_rate": sum(c.caught for c in report.cases) / expected if expected else 1.0,
        "false_positive_rate": (
            sum(c.false_positives for c in report.cases) / findings if findings else 0.0
        ),
        "avg_latency_s": sum(c.latency_s for c in report.cases) / n,
        "avg_cost_usd": sum(c.cost_usd for c in report.cases) / n,
        "case_pass_rate": sum(c.passed for c in report.cases) / n,
        "notes": sum(c.notes for c in report.cases),
    }

    if not cases:
        report.failures.append(f"suite '{suite}' has no cases")
    for c in report.cases:
        if c.verdict is not None:
            report.failures.append(f"{c.id}: reviewer returned verdict '{c.verdict}'")
        if max_notes is not None and c.notes > max_notes:
            report.failures.append(f"{c.id}: {c.notes} notes exceed max_per_case {max_notes}")
    checks = [
        ("catch_rate", "min_catch_rate", min),
        ("false_positive_rate", "max_false_positive_rate", max),
        ("avg_latency_s", "max_avg_latency_s", max),
        ("avg_cost_usd", "max_avg_cost_usd", max),
        ("case_pass_rate", "min_case_pass_rate", min),
    ]
    for metric, gate, kind in checks:
        value, limit = report.metrics[metric], gates[gate]
        if (kind is min and value < limit) or (kind is max and value > limit):
            report.failures.append(f"{metric} {value:.3f} violates {gate} {limit}")
    return report


@dataclass
class RepeatedReport:
    """The same suite run N times. Gate on the worst run: every run must pass."""

    suite: str
    runs: list[SuiteReport]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.runs)

    @property
    def failures(self) -> list[str]:
        return [f"run {i}: {f}" for i, r in enumerate(self.runs, 1) for f in r.failures]

    def case_pass_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self.runs:
            for c in run.cases:
                counts[c.id] = counts.get(c.id, 0) + int(c.passed)
        return counts

    def flaky_cases(self) -> list[str]:
        """Cases that passed in some runs but not all: the reviewer is unstable there."""
        n = len(self.runs)
        return [cid for cid, k in self.case_pass_counts().items() if 0 < k < n]

    def worst_metrics(self) -> dict[str, float]:
        lower_is_worse = {"catch_rate", "case_pass_rate"}
        worst: dict[str, float] = {}
        for run in self.runs:
            for key, value in run.metrics.items():
                if key not in worst:
                    worst[key] = value
                elif key in lower_is_worse:
                    worst[key] = min(worst[key], value)
                else:
                    worst[key] = max(worst[key], value)
        return worst


def run_repeated(
    suite: str, cases: list[Case], reviewer: Reviewer, thresholds: dict, repeats: int
) -> RepeatedReport:
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    return RepeatedReport(suite, [run_suite(suite, cases, reviewer, thresholds)
                                  for _ in range(repeats)])


def make_oracle(cases: list[Case]) -> Reviewer:
    """Returns the expected findings. Proves the harness and corpus, not a reviewer."""
    answers = {c.diff: c.expected for c in cases}

    def review(diff_text: str) -> ReviewResult:
        return ReviewResult(
            findings=[
                {**asdict(e), "severity": "major", "message": f"expected {e.category}"}
                for e in answers.get(diff_text, [])
            ]
        )

    return review


def null_reviewer(diff_text: str) -> ReviewResult:
    return ReviewResult(findings=[])


def resolve_reviewer(spec: str, cases: list[Case]) -> Reviewer:
    if spec == "oracle":
        return make_oracle(cases)
    if spec == "null":
        return null_reviewer
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr)


def _print_run(report: SuiteReport) -> None:
    for c in report.cases:
        print(f"{'pass' if c.passed else 'FAIL'} {c.id}  caught {c.caught}/{c.expected}, "
              f"fp {c.false_positives}, notes {c.notes}, {c.latency_s:.2f}s")
        for detail in c.finding_details:
            print(format_finding(detail))
    print(json.dumps(report.metrics, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", required=True, choices=["seeded-bugs", "injection"])
    parser.add_argument("--reviewer", default="oracle", help="oracle | null | module:function")
    parser.add_argument("--repeat", type=int, default=None,
                        help="runs per suite (default: `repeats` in thresholds.yaml)")
    parser.add_argument("--report", type=Path, help="write a JSON report here")
    args = parser.parse_args(argv)

    thresholds = yaml.safe_load((EVALS_DIR / "thresholds.yaml").read_text(encoding="utf-8"))
    repeats = args.repeat or thresholds.get("repeats", 1)
    cases = load_suite(EVALS_DIR / args.suite)
    reviewer = resolve_reviewer(args.reviewer, cases)
    # Deterministic reviewers give the same answer every time; one run is enough.
    if args.reviewer in ("oracle", "null"):
        repeats = 1
    result = run_repeated(args.suite, cases, reviewer, thresholds, repeats)

    for i, run in enumerate(result.runs, 1):
        print(f"=== run {i}/{repeats}")
        _print_run(run)
    if repeats > 1:
        print(f"=== summary over {repeats} runs (gate: every run must pass)")
        for cid, k in result.case_pass_counts().items():
            print(f"{'pass' if k == repeats else 'FAIL'} {cid}  {k}/{repeats}")
        print(json.dumps({"worst": result.worst_metrics(),
                          "flaky_cases": result.flaky_cases()}, indent=2))
    for failure in result.failures:
        print(f"GATE FAILED: {failure}")
    if args.report:
        payload = {"suite": args.suite, "repeats": repeats, "passed": result.passed,
                   "worst": result.worst_metrics(), "flaky_cases": result.flaky_cases(),
                   "runs": [asdict(r) for r in result.runs]}
        args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
