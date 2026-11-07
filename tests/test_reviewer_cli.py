import json

from evals.runner import EVALS_DIR, ReviewResult, load_suite
from reviewer import cli

CASES = {c.id: c for s in ("seeded-bugs", "injection") for c in load_suite(EVALS_DIR / s)}


def _pr_diff(*case_ids: str, extra: str = "") -> str:
    return "".join(CASES[c].diff for c in case_ids) + extra


NOTEBOOK = (
    "diff --git a/ml/notebooks/train.ipynb b/ml/notebooks/train.ipynb\n"
    "--- a/ml/notebooks/train.ipynb\n+++ b/ml/notebooks/train.ipynb\n"
    "@@ -1,1 +1,1 @@\n-{}\n+{\"cells\": []}\n"
)
DELETED = (
    "diff --git a/main/old.c b/main/old.c\ndeleted file mode 100644\n"
    "--- a/main/old.c\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-int x;\n"
)
BINARY = "diff --git a/docs/board.png b/docs/board.png\nBinary files differ\n"


def test_split_by_file_keeps_order_and_paths():
    files = cli.split_by_file(_pr_diff("004-isr-shared-counter", "008-hardcoded-mqtt-password",
                                       extra=DELETED + BINARY))
    assert [p for p, _ in files] == ["main/pulse.c", "main/mqtt_app.c", None, "docs/board.png"]
    assert files[0][1] == CASES["004-isr-shared-counter"].diff


def test_review_pr_reviews_source_files_only_and_reports_the_rest():
    seen = []

    def fake(diff_text, **kw):
        seen.append(diff_text)
        return ReviewResult(findings=[{"file": "x", "line": 1, "category": "off-by-one",
                                       "severity": "major", "message": "m"}], cost_usd=0.01)

    diff = _pr_diff("010-loop-off-by-one", extra=NOTEBOOK + DELETED + BINARY)
    report = cli.review_pr(diff, fake, max_lines=400, version="0.3.0")
    assert seen == [CASES["010-loop-off-by-one"].diff]
    assert len(report["findings"]) == 1 and report["cost_usd"] == 0.01
    reasons = {s["file"]: s["reason"] for s in report["skipped"]}
    assert reasons["ml/notebooks/train.ipynb"] == "not a reviewed file type"
    assert reasons["(deleted file)"] == "deleted"
    assert reasons["docs/board.png"] == "empty, binary or rename only"


def test_oversized_file_is_skipped_not_truncated():
    report = cli.review_pr(CASES["006-frame-length-unchecked"].diff,
                           lambda d, **kw: ReviewResult(findings=[]),
                           max_lines=5, version="v")
    assert report["skipped"][0]["reason"].endswith("> 5")


def test_one_failing_file_does_not_lose_the_others():
    def flaky(diff_text, **kw):
        if "pulse.c" in diff_text:
            raise RuntimeError("reviewer failed: overloaded")
        return ReviewResult(findings=[{"file": "main/adc_avg.c", "line": 9,
                                       "category": "off-by-one", "severity": "major",
                                       "message": "m"}])

    report = cli.review_pr(_pr_diff("004-isr-shared-counter", "010-loop-off-by-one"), flaky,
                           400, "v")
    assert len(report["findings"]) == 1
    assert report["errors"][0]["file"] == "main/pulse.c"


def test_comment_text_is_inert_markdown():
    evil = "@AlainAhuili see ![x](https://evil.example/t.png) [click](https://evil.example) " \
           "<img src=x> | breaks | table\n## fake heading `code`"
    out = cli.md_text(evil)
    assert "@​AlainAhuili" in out and "@AlainAhuili" not in out
    assert "<img" not in out and "&lt;img" in out
    assert "](" not in out and "\\]\\(" in out
    assert "\n" not in out and "\\#\\# fake" in out
    assert "\\|" in out and "\\`code\\`" in out


def test_md_code_cannot_be_closed_early():
    assert cli.md_code("main/a`b.c:3") == "`main/a'b.c:3`"


def test_render_comment_groups_and_marks():
    report = {
        "reviewer_version": "0.3.0", "cost_usd": 0.0123, "errors": [],
        "skipped": [{"file": "ml/x.ipynb", "reason": "not a reviewed file type"}],
        "findings": [
            {"file": "main/a.c", "line": 13, "category": "injection", "severity": "critical",
             "message": "addressed to reviewer", "source": "deterministic"},
            {"file": "main/a.c", "line": 16, "category": "off-by-one", "severity": "major",
             "message": "Which index is the last valid one?"},
            {"file": "main/a.c", "line": 14, "category": "missing-test", "severity": "minor",
             "message": "Which test shows the boundary?"},
        ],
    }
    md = cli.render_comment(report)
    assert md.startswith(cli.COMMENT_MARKER)
    assert "1 finding(s), 1 learning note(s)" in md
    assert md.index("Text addressed to an AI reviewer") < md.index("#### Findings") \
        < md.index("#### Learning notes")
    assert "`main/a.c:16`" in md and "Skipped files" in md


def test_render_comment_without_findings():
    md = cli.render_comment({"reviewer_version": "v", "cost_usd": 0.0, "errors": [],
                             "skipped": [], "findings": []})
    assert "No findings" in md


def test_main_end_to_end_with_fake_model(tmp_path, monkeypatch):
    from reviewer import claude

    async def fake_ask(diff_text, mode, categories):
        return [{"file": "main/ring_buffer.c", "line": 16, "category": "off-by-one",
                 "severity": "major", "message": "What is the last valid index?"}], 0.005

    monkeypatch.setattr(claude, "_ask", fake_ask)
    diff = tmp_path / "pr.diff"
    diff.write_text(_pr_diff("001-comment-approve"))
    out, comment = tmp_path / "f.json", tmp_path / "c.md"
    args = ["--diff", str(diff), "--repo", "firmware-edge-ai", "--sha", "abc123",
            "--out", str(out), "--comment", str(comment)]
    assert cli.main(args) == 0
    report = json.loads(out.read_text())
    assert report["repo"] == "firmware-edge-ai" and report["commit_sha"] == "abc123"
    assert {f["category"] for f in report["findings"]} == {"injection", "off-by-one"}
    assert "Text addressed to an AI reviewer" in comment.read_text(encoding="utf-8")
    # Only the deterministic guard may fail the job.
    assert cli.main([*args, "--fail-on-injection"]) == 1


def test_single_file_pr_has_no_context():
    calls = []
    cli.review_pr(CASES["010-loop-off-by-one"].diff,
                  lambda d, **kw: calls.append(kw.get('context')) or ReviewResult(findings=[]),
                  400, "v")
    assert calls == [None]


def _note(file, sev="minor"):
    return {"file": file, "line": 1, "category": "missing-test", "severity": sev, "message": "q"}


def test_select_notes_spreads_across_files_and_prefers_minor():
    notes = [_note("a.c", "info"), *[_note("a.c") for _ in range(6)], _note("b.c"),
             _note("c.c", "info")]
    picked = cli.select_notes(notes, 4)
    assert [n["file"] for n in picked] == ["a.c", "b.c", "c.c", "a.c"]
    assert sum(n["file"] == "a.c" for n in cli.select_notes(notes, 8)) == 6


def test_comment_caps_notes_and_points_to_the_artifact():
    report = {"reviewer_version": "0.4.0", "cost_usd": 0.0, "errors": [], "skipped": [],
              "findings": [_note(f"f{i}.c") for i in range(15)]}
    md = cli.render_comment(report)
    assert md.count("**minor**") == cli.MAX_COMMENT_NOTES
    assert "and 7 more in the `review-findings` artifact" in md
    assert "15 learning note(s)" in md


def _file_diff(path: str, *added: str) -> str:
    body = "".join(f"+{a}\n" for a in added)
    return (f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(added)} @@\n{body}")


HEADER = _file_diff("lib/window/ring.h", "#pragma once", "template <int N> struct Ring {};")
USER = _file_diff("src/main.cpp", '#include "ring.h"', "Ring<8> w;")
TEST = _file_diff("test/test_ring.cpp", '#include "ring.h"', "void test_ring() {}")
UNRELATED = _file_diff("src/led.c", "void led_on(void) {}")
CI = _file_diff(".github/workflows/fw.yml", "on: push", "# builds per platformio.ini")
INI = _file_diff("platformio.ini", "[env:native]", "platform = native")


def test_related_by_include_in_either_direction():
    assert cli._related("src/main.cpp", USER, "lib/window/ring.h", HEADER)
    assert cli._related("lib/window/ring.h", HEADER, "src/main.cpp", USER)
    assert not cli._related("src/main.cpp", USER, "src/led.c", UNRELATED)
    assert cli._related(".github/workflows/fw.yml", CI, "platformio.ini", INI)


def test_context_contains_only_related_files():
    files = cli.split_by_file(HEADER + USER + TEST + UNRELATED)
    ctx_header = cli.build_context(files, "lib/window/ring.h", 600)
    assert "src/main.cpp" in ctx_header and "test/test_ring.cpp" in ctx_header
    assert "led.c" not in ctx_header
    assert cli.build_context(files, "src/led.c", 600) is None  # nothing related: no context
    ctx_main = cli.build_context(files, "src/main.cpp", 600)
    assert "ring.h" in ctx_main and "test_ring.cpp" not in ctx_main


def test_context_budget_keeps_whole_files_and_names_the_rest():
    files = cli.split_by_file(HEADER + USER + TEST)
    budget = USER.count("\n")  # room for exactly one related file
    ctx = cli.build_context(files, "lib/window/ring.h", budget)
    assert ctx.startswith(USER)
    assert "Not included, over the context budget: test/test_ring.cpp" in ctx


def test_later_files_see_what_was_already_reported():
    calls = []

    def fake(diff_text, **kw):
        calls.append(kw.get("reported"))
        path = cli.split_by_file(diff_text)[0][0]
        return ReviewResult(findings=[{"file": path, "line": 1, "category": "missing-test",
                                       "severity": "minor", "message": f"note on {path}"}])

    cli.review_pr(HEADER + USER, fake, 400, "v")
    assert calls[0] is None
    assert "lib/window/ring.h:1 [missing-test] note on lib/window/ring.h" in calls[1]


def test_summarize_reported_is_one_short_line_per_finding():
    long_msg = "word " * 100 + "\n::injected"
    out = cli.summarize_reported([{"file": "a.c", "line": 3, "category": "x", "message": long_msg}])
    assert out.count("\n") == 0 and len(out) < 160
    assert cli.summarize_reported([]) is None


def test_file_names_match_as_whole_names_only():
    test_file = _file_diff("test/test_main.cpp", '#include "ring.h"')
    main_file = _file_diff("src/main.cpp", "int main() {}")
    assert not cli._related("src/main.cpp", main_file, "test/test_main.cpp", test_file)
    assert cli._mentions('#include "main.cpp"', "main.cpp")
    assert cli._mentions("see src/main.cpp:12", "main.cpp")
    assert not cli._mentions("test_main.cpp", "main.cpp")
    # A trailing full stop still counts ("see ring.h."), so this errs towards including.
    assert cli._mentions("see ring.h.", "ring.h")
