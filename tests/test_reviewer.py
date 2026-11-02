"""Offline tests for the Claude reviewer: everything except the model call itself."""

import pytest
import yaml

from evals.runner import EVALS_DIR, load_suite, run_suite
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
