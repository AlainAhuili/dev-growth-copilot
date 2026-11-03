"""Offline tests for the Claude reviewer: everything except the model call itself."""

import pytest
import yaml

from evals.runner import EVALS_DIR, ReviewResult, load_suite, run_suite
from reviewer import claude
from skillmap import connect, migrate

THRESHOLDS = yaml.safe_load((EVALS_DIR / "thresholds.yaml").read_text())


def test_schema_categories_are_exactly_the_skill_map():
    conn = connect()
    migrate(conn)
    db = {r[0] for r in conn.execute("SELECT name FROM categories")}
    schema = claude.output_schema(claude.skillmap_categories())
    item = schema["properties"]["findings"]["items"]
    assert set(item["properties"]["category"]["enum"]) == db


def test_schema_has_no_verdict_field():
    schema = claude.output_schema(["off-by-one"])
    assert "verdict" not in schema["properties"]
    assert schema["additionalProperties"] is False


def test_fast_mode_config_loads():
    mode = claude.load_mode("fast")
    assert mode["model"] and "untrusted" in mode["prompt"].lower()
    assert mode["max_diff_lines"] == 400


def test_clean_findings_drops_invalid_and_normalises():
    raw = [
        {"file": "b/main/x.c", "line": "5", "category": "off-by-one", "severity": "major",
         "message": "m"},
        {"file": "main/x.c", "line": 1, "category": "made-up", "severity": "major",
         "message": "m"},
        {"file": "main/x.c", "line": 1, "category": "off-by-one", "severity": "blocker",
         "message": "m"},
        {"line": 1},
        "not a dict",
    ]
    out = claude.clean_findings(raw, {"off-by-one"})
    assert [(f["file"], f["line"]) for f in out] == [("main/x.c", 5)]


def test_merge_keeps_guard_finding_and_drops_model_duplicate():
    guard = [{"file": "a.c", "line": 13, "category": "injection"}]
    llm = [
        {"file": "a.c", "line": 14, "category": "injection"},
        {"file": "a.c", "line": 16, "category": "off-by-one"},
    ]
    merged = claude.merge(guard, llm)
    assert merged == [guard[0], llm[1]]


def test_oversized_diff_is_rejected_before_any_model_call(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("model must not be called")

    monkeypatch.setattr(claude, "_ask", fail)
    with pytest.raises(ValueError, match="exceeds"):
        claude.review("+x\n" * 401)


def test_guard_plus_model_passes_injection_suite(monkeypatch):
    """With a model that finds the real bugs and also repeats the injection finding,
    the merged result passes the injection gates (no duplicates, no verdict)."""
    cases = load_suite(EVALS_DIR / "injection")
    answers = {c.diff: c.expected for c in cases}

    async def fake_ask(diff_text, mode, categories):
        return [
            {"file": e.file, "line": e.line, "category": e.category, "severity": "major",
             "message": "why? what would you check?"}
            for e in answers[diff_text]
        ], 0.01

    monkeypatch.setattr(claude, "_ask", fake_ask)
    report = run_suite("injection", cases, claude.review, THRESHOLDS)
    assert report.passed, report.failures


def _numbered_added(diff_text):
    """(line number, text) for every '+' line in number_diff output."""
    out = []
    for row in claude.number_diff(diff_text).splitlines():
        num, sep, rest = row.partition(" | ")
        if sep and rest.startswith("+") and num.strip():
            out.append((int(num), rest[1:]))
    return out


def test_number_diff_matches_the_guard_line_tracking_on_every_case():
    from tools.injection_guard import added_lines

    for suite in ("seeded-bugs", "injection"):
        for case in load_suite(EVALS_DIR / suite):
            expected = [(line, text) for _, line, text in added_lines(case.diff)]
            assert _numbered_added(case.diff) == expected, case.id


def test_number_diff_puts_expected_bugs_on_their_numbered_lines():
    case = next(c for c in load_suite(EVALS_DIR / "seeded-bugs") if c.id.startswith("007"))
    numbered = dict(_numbered_added(case.diff))
    assert "esp_ota_end(handle);" in numbered[case.expected[0].line]


def test_number_diff_leaves_removed_lines_and_headers_unnumbered():
    diff = (
        "diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -4,2 +4,2 @@\n ctx\n-old\n+new\n"
        "\\ No newline at end of file\n"
    )
    rows = claude.number_diff(diff).splitlines()
    assert rows[:4] == ["diff --git a/x.c b/x.c", "--- a/x.c", "+++ b/x.c", "@@ -4,2 +4,2 @@"]
    assert rows[4] == "    4 |  ctx"
    assert rows[5] == "      | -old"
    assert rows[6] == "    5 | +new"
    # The marker follows the hunk's last counted line, so it passes through unnumbered.
    assert rows[7] == "\\ No newline at end of file"


def test_injection_guard_still_scans_the_raw_diff(monkeypatch):
    """Numbering is for the model only; the deterministic guard sees the original."""
    case = next(c for c in load_suite(EVALS_DIR / "injection") if c.id.startswith("001"))
    seen = {}

    async def fake_ask(diff_text, mode, categories):
        seen["diff"] = diff_text
        return [], 0.0

    monkeypatch.setattr(claude, "_ask", fake_ask)
    result = claude.review(case.diff)
    assert seen["diff"] == case.diff
    assert any(f["category"] == "injection" and f["line"] == 13 for f in result.findings)


def test_iteration_2_run_replays():
    """Replays of the three failures in the first 3x17 run (reviewer 0.2.0)."""
    from evals.runner import score_case

    tol = THRESHOLDS["line_tolerance"]
    inj = next(c for c in load_suite(EVALS_DIR / "injection") if c.id.startswith("001"))
    found = [
        {"file": "main/ring_buffer.c", "line": 13, "category": "injection", "severity": "critical"},
        {"file": "main/ring_buffer.c", "line": 16, "category": "buffer-overflow",
         "severity": "major"},
    ]
    assert score_case(inj, ReviewResult(found), 0.0, tol).passed  # fixed by `also`

    ota = next(c for c in load_suite(EVALS_DIR / "seeded-bugs") if c.id.startswith("007"))
    wrong_line = [{"file": "main/ota.c", "line": 22, "category": "error-handling",
                   "severity": "major"}]
    assert not score_case(ota, ReviewResult(wrong_line), 0.0, tol).passed  # still a miss:
    right_line = [{**wrong_line[0], "line": 8}]  # line 22 doesn't exist; numbering fixes it
    assert score_case(ota, ReviewResult(right_line), 0.0, tol).passed
