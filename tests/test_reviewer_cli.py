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

    def fake(diff_text):
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
                           lambda d: ReviewResult(findings=[]), max_lines=5, version="v")
    assert report["skipped"][0]["reason"].endswith("> 5")


def test_one_failing_file_does_not_lose_the_others():
    def flaky(diff_text):
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
    assert "Text addressed to an AI reviewer" in comment.read_text()
    # Only the deterministic guard may fail the job.
    assert cli.main([*args, "--fail-on-injection"]) == 1
