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
    findings = sum(len(c.expected) for c in cases) + 1  # oracle answers + the one noisy finding
    assert report.metrics["false_positive_rate"] == pytest.approx(1 / findings)
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


NOTES = frozenset({"info", "minor"})


def _one_bug_case():
    return Case(id="c", description="", diff="", expected=[Expected("main/a.c", 10, "off-by-one")])


def _bug_found():
    return {"file": "main/a.c", "line": 10, "category": "off-by-one", "severity": "major"}


def test_minor_unmatched_finding_is_a_note_not_a_false_positive():
    note = {"file": "main/a.c", "line": 5, "category": "missing-test", "severity": "minor"}
    r = score_case(_one_bug_case(), ReviewResult([_bug_found(), note]), 0.0, 3, NOTES, 3)
    assert (r.false_positives, r.notes, r.gated_findings) == (0, 1, 1)
    assert r.passed
    assert r.finding_details[1]["kind"] == "note"


def test_major_unmatched_finding_is_still_a_false_positive():
    wrong = {"file": "main/a.c", "line": 5, "category": "null-deref", "severity": "major"}
    r = score_case(_one_bug_case(), ReviewResult([_bug_found(), wrong]), 0.0, 3, NOTES, 3)
    assert r.false_positives == 1 and not r.passed


def test_finding_without_severity_cannot_dodge_the_gate():
    no_sev = {"file": "main/a.c", "line": 5, "category": "null-deref"}
    r = score_case(_one_bug_case(), ReviewResult([_bug_found(), no_sev]), 0.0, 3, NOTES, 3)
    assert r.false_positives == 1


def test_too_many_notes_fail_the_case_and_the_gate():
    notes = [{"file": "main/a.c", "line": i, "category": "missing-test", "severity": "minor"}
             for i in range(20, 24)]
    r = score_case(_one_bug_case(), ReviewResult([_bug_found(), *notes]), 0.0, 3, NOTES, 3)
    assert r.notes == 4 and not r.passed

    cases = load_suite(EVALS_DIR / "seeded-bugs")
    oracle = make_oracle(cases)

    def chatty(diff_text):
        extra = [{"file": "x.c", "line": i, "category": "missing-test", "severity": "info"}
                 for i in range(4)]
        return ReviewResult(findings=oracle(diff_text).findings + extra)

    report = run_suite("seeded-bugs", cases, chatty, THRESHOLDS)
    assert any("notes exceed max_per_case" in f for f in report.failures)


def test_notes_do_not_dilute_false_positive_rate():
    cases = load_suite(EVALS_DIR / "seeded-bugs")
    oracle = make_oracle(cases)
    bugged = next(c for c in cases if c.expected)

    def wrong_plus_notes(diff_text):
        findings = oracle(diff_text).findings
        if diff_text == bugged.diff:
            findings.append({"file": "x.c", "line": 1, "category": "coupling", "severity": "major"})
            findings += [{"file": "x.c", "line": i, "category": "missing-test",
                          "severity": "minor"} for i in range(2, 5)]
        return ReviewResult(findings=findings)

    report = run_suite("seeded-bugs", cases, wrong_plus_notes, THRESHOLDS)
    gated = sum(len(c.expected) for c in cases) + 1
    assert report.metrics["false_positive_rate"] == pytest.approx(1 / gated)
    assert report.metrics["notes"] == 3


def test_first_real_run_findings_now_pass():
    """Replay of the 2026-11-03 Claude run on case 002 (5 findings), after the corpus fix."""
    case = next(c for c in load_suite(EVALS_DIR / "seeded-bugs") if c.id == "002-wifi-cfg-leak")
    real = [
        {"file": "main/wifi_cfg.c", "line": 30, "category": "resource-leak", "severity": "major"},
        {"file": "main/wifi_cfg.c", "line": 32, "category": "buffer-overflow", "severity": "major"},
        {"file": "main/wifi_cfg.c", "line": 24, "category": "null-deref", "severity": "minor"},
        {"file": "main/wifi_cfg.c", "line": 23, "category": "missing-test", "severity": "minor"},
        {"file": "main/wifi_cfg.c", "line": 30, "category": "error-handling", "severity": "minor"},
    ]
    r = score_case(case, ReviewResult(real), 0.0, THRESHOLDS["line_tolerance"], NOTES,
                   THRESHOLDS["notes"]["max_per_case"])
    assert (r.caught, r.false_positives, r.notes) == (2, 0, 3)
    assert r.passed


def test_finding_anywhere_in_a_bug_span_matches():
    span = Case(id="s", description="", diff="",
                expected=[Expected("main/a.c", 25, "buffer-overflow", end_line=32)])
    for line in (22, 25, 28, 32, 35):  # span plus tolerance 3 on both sides
        f = [{"file": "main/a.c", "line": line, "category": "buffer-overflow", "severity": "major"}]
        assert score_case(span, ReviewResult(f), 0.0, 3).passed, line
    for line in (21, 36):
        f = [{"file": "main/a.c", "line": line, "category": "buffer-overflow", "severity": "major"}]
        assert not score_case(span, ReviewResult(f), 0.0, 3).passed, line


def test_reviewer_0_2_0_run_on_case_002_now_passes():
    """Replay of the reviewer 0.2.0 run: the NUL-terminator bug anchored on the malloc line."""
    case = next(c for c in load_suite(EVALS_DIR / "seeded-bugs") if c.id == "002-wifi-cfg-leak")
    real = [
        {"file": "main/wifi_cfg.c", "line": 29, "category": "resource-leak", "severity": "major"},
        {"file": "main/wifi_cfg.c", "line": 28, "category": "buffer-overflow", "severity": "major"},
        {"file": "main/wifi_cfg.c", "line": 23, "category": "null-deref", "severity": "minor"},
        {"file": "main/wifi_cfg.c", "line": 23, "category": "missing-test", "severity": "minor"},
    ]
    r = score_case(case, ReviewResult(real), 0.0, THRESHOLDS["line_tolerance"], NOTES,
                   THRESHOLDS["notes"]["max_per_case"])
    assert (r.caught, r.false_positives, r.notes) == (2, 0, 2)
