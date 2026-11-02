from pathlib import Path

import pytest
import yaml

from evals.runner import (
    EVALS_DIR,
    Case,
    Expected,
    ReviewResult,
    load_suite,
    make_oracle,
    null_reviewer,
    run_suite,
    score_case,
)

THRESHOLDS = yaml.safe_load((EVALS_DIR / "thresholds.yaml").read_text())
SUITES = ["seeded-bugs", "injection"]


@pytest.mark.parametrize("suite", SUITES)
def test_oracle_passes_every_gate(suite):
    cases = load_suite(EVALS_DIR / suite)
    report = run_suite(suite, cases, make_oracle(cases), THRESHOLDS)
    assert report.passed, report.failures
    assert report.metrics["catch_rate"] == 1.0


@pytest.mark.parametrize("suite", SUITES)
def test_reviewer_that_finds_nothing_fails(suite):
    cases = load_suite(EVALS_DIR / suite)
    report = run_suite(suite, cases, null_reviewer, THRESHOLDS)
    assert not report.passed
    assert report.metrics["catch_rate"] == 0.0


def test_injected_verdict_fails_even_with_perfect_findings():
    cases = load_suite(EVALS_DIR / "injection")
    oracle = make_oracle(cases)

    def obeys_the_injection(diff_text):
        return ReviewResult(findings=oracle(diff_text).findings, verdict="approve")

    report = run_suite("injection", cases, obeys_the_injection, THRESHOLDS)
    assert any("verdict 'approve'" in f for f in report.failures)


def test_noise_on_clean_case_counts_as_false_positive():
    cases = load_suite(EVALS_DIR / "seeded-bugs")
    oracle = make_oracle(cases)
    clean = next(c for c in cases if not c.expected)

    def noisy(diff_text):
        findings = oracle(diff_text).findings
        if diff_text == clean.diff:
            findings.append({"file": "main/led.c", "line": 6, "category": "coupling"})
        return ReviewResult(findings=findings)

    report = run_suite("seeded-bugs", cases, noisy, THRESHOLDS)
    assert report.metrics["false_positive_rate"] == pytest.approx(1 / 3)
    assert not report.passed


def _two_bug_case():
    # Two off-by-one bugs four lines apart: one finding at line 12 is within tolerance of both.
    return Case(
        id="two-bugs", description="", diff="",
        expected=[Expected("main/a.c", 10, "off-by-one"), Expected("main/a.c", 14, "off-by-one")],
    )


def test_one_finding_cannot_catch_two_bugs():
    one = [{"file": "main/a.c", "line": 12, "category": "off-by-one"}]
    result = score_case(_two_bug_case(), ReviewResult(findings=one), 0.0, tolerance=3)
    assert result.caught == 1
    assert not result.passed


def test_assignment_finds_what_greedy_would_miss():
    # Line 12 fits both bugs, line 16 only the second. A greedy matcher giving 12 to the
    # second bug would leave the first uncaught.
    findings = [
        {"file": "main/a.c", "line": 12, "category": "off-by-one"},
        {"file": "main/a.c", "line": 16, "category": "off-by-one"},
    ]
    result = score_case(_two_bug_case(), ReviewResult(findings=findings), 0.0, tolerance=3)
    assert result.caught == 2 and result.passed


def test_model_path_prefix_is_normalised():
    finding = [{"file": "b/main/a.c", "line": 10, "category": "off-by-one"}]
    case = Case(id="c", description="", diff="", expected=[Expected("main/a.c", 10, "off-by-one")])
    assert score_case(case, ReviewResult(findings=finding), 0.0, tolerance=3).passed


def test_empty_suite_fails(tmp_path: Path):
    report = run_suite("seeded-bugs", load_suite(tmp_path), null_reviewer, THRESHOLDS)
    assert "suite 'seeded-bugs' has no cases" in report.failures
