from pathlib import Path

import pytest
import yaml

from evals.runner import (
    EVALS_DIR,
    Case,
    Expected,
    ReviewResult,
    format_finding,
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


def test_report_keeps_every_finding_with_matched_flag():
    case = Case(id="c", description="", diff="", expected=[Expected("main/a.c", 10, "off-by-one")])
    findings = [
        {"file": "main/a.c", "line": 10, "category": "off-by-one", "message": "bound"},
        {"file": "main/a.c", "line": 12, "category": "buffer-overflow", "message": "write"},
    ]
    result = score_case(case, ReviewResult(findings=findings), 0.0, tolerance=3)
    assert [d["matched"] for d in result.finding_details] == [True, False]
    assert result.false_positives == 1
    assert result.finding_details[1]["message"] == "write"


def test_format_finding_cannot_emit_workflow_commands():
    evil = {
        "file": "a.c", "line": 1, "category": "injection", "severity": "major",
        "matched": False, "message": "ok\n::add-mask::secret\r\n::stop-commands::x",
    }
    line = format_finding(evil)
    assert "\n" not in line and "\r" not in line
    assert "::" not in line
    assert line.startswith("    EXTRA a.c:1 [injection/major]")


def test_format_finding_truncates_long_messages():
    line = format_finding({"file": "a.c", "line": 1, "category": "x", "message": "m" * 500})
    assert line.endswith("...") and len(line) < 220


def test_cli_prints_findings(capsys):
    from evals.runner import main

    assert main(["--suite", "seeded-bugs", "--reviewer", "oracle"]) == 0
    out = capsys.readouterr().out
    assert "    match main/ring_buffer.c:15 [off-by-one/major]" in out
