import json
import shutil
from dataclasses import replace

import pytest
import yaml

from evals import runner, shadow

THRESHOLDS = yaml.safe_load((runner.EVALS_DIR / "thresholds.yaml").read_text(encoding="utf-8"))
LIMITS = THRESHOLDS["shadow"]
CASES = runner.load_suite(runner.EVALS_DIR / "seeded-bugs")


def _report(reviewer_for_run, cases=CASES, repeats=3):
    """reviewer_for_run(run_index) -> reviewer. Builds the same JSON CI writes."""
    runs = [runner.run_suite("seeded-bugs", cases, reviewer_for_run(i), THRESHOLDS)
            for i in range(repeats)]
    return runner.report_payload(runner.RepeatedReport("seeded-bugs", runs), cases)


def _missing(case_ids, runs=None):
    """The oracle, except it misses the bugs of `case_ids` (in `runs`, default all)."""
    oracle = runner.make_oracle(CASES)
    diffs = {c.diff for c in CASES if c.id in case_ids}

    def for_run(i):
        def review(diff):
            if diff in diffs and (runs is None or i in runs):
                return runner.ReviewResult(findings=[])
            return oracle(diff)
        return review
    return for_run


def _with(oracle_for_run_cost=0.0):
    oracle = runner.make_oracle(CASES)

    def for_run(i):
        def review(diff):
            r = oracle(diff)
            r.cost_usd = oracle_for_run_cost
            return r
        return review
    return for_run


BUGGY = next(c.id for c in CASES if any(not e.optional for e in c.expected))
OTHER = [c.id for c in CASES if any(not e.optional for e in c.expected)][1]


def test_report_has_case_digests_that_track_content(tmp_path):
    report = _report(_missing(set()))
    assert set(report["case_digests"]) == {c.id for c in CASES}
    src = runner.EVALS_DIR / "seeded-bugs" / CASES[0].id
    shutil.copytree(src, tmp_path / CASES[0].id)
    (copy,) = runner.load_suite(tmp_path)
    assert copy.digest == CASES[0].digest != ""  # stable across loads
    spec = yaml.safe_load((src / "case.yaml").read_text(encoding="utf-8"))
    diff_file = tmp_path / CASES[0].id / spec["diff"]
    diff_file.write_text(diff_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert runner.load_suite(tmp_path)[0].digest != CASES[0].digest


def _latency(report, seconds):
    return {**report, "worst": {**report["worst"], "avg_latency_s": seconds}}


@pytest.mark.parametrize("before,after,fails", [
    (2.0, 6.0, True),          # x3 and +4 s
    (2.0, 4.5, False),         # x2.25 but only +2.5 s: jitter
    (10.0, 14.0, False),       # +4 s but only x1.4
    (0.000006, 0.000012, False),  # microseconds (fake reviewers): never gates
])
def test_latency_gate_needs_ratio_and_absolute_increase(before, after, fails):
    base = _report(_missing(set()))
    result = shadow.compare(_latency(base, before), _latency(base, after), LIMITS)
    assert bool(result["failures"]) is fails


def test_identical_reviewers_pass_with_nothing_moved():
    base = _report(_missing(set()))
    result = shadow.compare(base, _report(_missing(set())), LIMITS)
    assert result["failures"] == []
    assert all(c["status"] == "same" for c in result["cases"])
    assert "None: every case passed as often as before." in shadow.render(result)


def test_consistent_regression_fails_flaky_one_does_not():
    base = _report(_missing(set()))
    cand = _report(_missing({BUGGY}))
    result = shadow.compare(base, cand, LIMITS)
    statuses = {c["id"]: c["status"] for c in result["cases"]}
    assert statuses[BUGGY] == "regressed"
    assert any(BUGGY in f for f in result["failures"])

    # Missing it in one of three runs only: worse, reported, not a case regression.
    flaky = _report(_missing({BUGGY}, runs={1}))
    result = shadow.compare(base, flaky, {**LIMITS, "max_catch_rate_drop": 1.0})
    assert {c["id"]: c["status"] for c in result["cases"]}[BUGGY] == "worse"
    assert result["failures"] == []


def test_fixed_case_and_catch_rate_drop():
    base = _report(_missing({BUGGY, OTHER}))
    cand = _report(_missing(set()))
    result = shadow.compare(base, cand, LIMITS)
    assert {c["id"]: c["status"] for c in result["cases"]}[BUGGY] == "fixed"
    assert result["failures"] == []
    reverse = shadow.compare(cand, base, {**LIMITS, "max_catch_rate_drop": 0.0})
    assert any(f.startswith("catch_rate fell") for f in reverse["failures"])


def test_cost_ratio_gate():
    base, cand = _report(_with(0.01)), _report(_with(0.02))
    result = shadow.compare(base, cand, LIMITS)
    assert result["failures"] == ["avg_cost_usd 0.01 -> 0.02 is x2.00 (max x1.5)"]
    assert "| avg_cost_usd | 0.01 | 0.02 | worse |" in shadow.render(result)


def test_changed_new_and_removed_cases_are_not_compared():
    base = _report(_missing(set()), cases=CASES[1:])
    edited = [replace(CASES[1], digest="edited"), *CASES[2:-1], CASES[0]]
    cand = _report(_missing(set()), cases=edited)
    result = shadow.compare(base, cand, LIMITS)
    assert result["changed"] == [CASES[1].id]
    assert result["new"] == [CASES[0].id] and result["removed"] == [CASES[-1].id]
    assert CASES[1].id not in {c["id"] for c in result["cases"]}


def test_old_baseline_without_digests_compares_by_name():
    base = _report(_missing(set()))
    del base["case_digests"]
    result = shadow.compare(base, _report(_missing(set())), LIMITS)
    assert not result["digests_known"] and result["changed"] == []
    assert "compared by name" in shadow.render(result)


def test_incomparable_reports():
    base = _report(_missing(set()))
    with pytest.raises(shadow.ShadowError, match="different suites"):
        shadow.compare(base, {**base, "suite": "injection"}, LIMITS)
    with pytest.raises(shadow.ShadowError, match="no runs"):
        shadow.compare(base, {**base, "runs": []}, LIMITS)


def test_main_exit_codes_and_summary(tmp_path, capsys):
    ok, bad = tmp_path / "ok.json", tmp_path / "bad.json"
    ok.write_text(json.dumps(_report(_missing(set()))))
    bad.write_text(json.dumps(_report(_missing({BUGGY}))))
    summary = tmp_path / "summary.md"
    assert shadow.main([str(ok), str(ok), "--summary", str(summary)]) == 0
    assert shadow.main([str(ok), str(bad), "--summary", str(summary)]) == 1
    assert summary.read_text(encoding="utf-8").count("## Shadow mode") == 2  # appended
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert shadow.main([str(ok), str(broken)]) == 2
    assert "cannot compare" in capsys.readouterr().err
