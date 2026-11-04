"""Review a real PR diff: per-file reviews, a findings JSON for the skill map, a PR comment.

    python -m reviewer.cli --diff pr.diff --repo firmware-edge-ai --sha abc123 \\
        --out findings.json --comment comment.md [--fail-on-injection]

Advisory by design: exit code 0 whatever the model says. The only thing that may fail
the job is the deterministic injection guard, and only with --fail-on-injection.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from evals.runner import ReviewResult

COMMENT_MARKER = "<!-- dev-growth-copilot-review -->"
REVIEWABLE_SUFFIXES = {
    ".c", ".h", ".cc", ".cpp", ".hpp", ".ino", ".py", ".yml", ".yaml", ".ini", ".cmake",
}
REVIEWABLE_NAMES = {"CMakeLists.txt", "Dockerfile", "Kconfig", "Kconfig.projbuild"}
MAX_MESSAGE_CHARS = 500
GATED = {"major", "critical"}
# The comment shows at most this many learning notes; the JSON keeps all of them.
MAX_COMMENT_NOTES = 8
# Other changed files given to each file review as read-only context.
CONTEXT_MAX_LINES = 600

# review(diff, context=None, reported=None) -> ReviewResult
Review = Callable[..., ReviewResult]


def split_by_file(diff_text: str) -> list[tuple[str | None, str]]:
    """[(new path or None if deleted, file diff)] in diff order."""
    chunks = re.split(r"(?m)^(?=diff --git )", diff_text)
    out = []
    for chunk in chunks:
        if not chunk.startswith("diff --git "):
            continue
        m = re.search(r"(?m)^\+\+\+ (.+)$", chunk)
        if m is None:  # binary file or pure rename: nothing to review
            header = chunk.splitlines()[0]
            out.append((header.rsplit(" b/", 1)[-1], ""))
            continue
        target = m.group(1).strip()
        out.append((None if target == "/dev/null" else target.removeprefix("b/"), chunk))
    return out


def is_reviewable(path: str) -> bool:
    p = PurePosixPath(path)
    return p.suffix.lower() in REVIEWABLE_SUFFIXES or p.name in REVIEWABLE_NAMES


def _related(path_a: str, diff_a: str, path_b: str, diff_b: str) -> bool:
    """True if either file mentions the other by name: an #include, an import, a workflow
    that names platformio.ini. Cheap, language-agnostic, and errs towards including."""
    return _mentions(diff_a, PurePosixPath(path_b).name) or _mentions(
        diff_b, PurePosixPath(path_a).name)


def _mentions(text: str, name: str) -> bool:
    """`name` as a whole file name: 'main.cpp' is not mentioned by 'test_main.cpp'."""
    return re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(name)}(?![A-Za-z0-9_-])", text) is not None


def build_context(files: list[tuple[str, str]], target: str, max_lines: int) -> str | None:
    """The PR files related to `target` (see _related), whole files only, within budget.

    Unrelated files are left out: they made v0.4.0 reviews three times as expensive
    without changing what the reviewer could answer.
    """
    target_diff = dict(files)[target]
    parts, used, omitted = [], 0, []
    for path, file_diff in files:
        if path == target or not _related(target, target_diff, path, file_diff):
            continue
        n = file_diff.count("\n")
        if used + n > max_lines:
            omitted.append(path)
            continue
        parts.append(file_diff)
        used += n
    if omitted:
        parts.append("(Not included, over the context budget: " + ", ".join(omitted) + ")\n")
    return "".join(parts) or None


def summarize_reported(findings: list[dict], limit: int = 40) -> str | None:
    """One line per finding so far, for the next file's review to avoid repeats."""
    lines = [
        f"- {f.get('file')}:{f.get('line')} [{f.get('category')}] "
        + " ".join(str(f.get("message", "")).split())[:120]
        for f in findings[:limit]
    ]
    return "\n".join(lines) or None


def review_pr(diff_text: str, review: Review, max_lines: int, version: str,
              context_lines: int = CONTEXT_MAX_LINES) -> dict:
    """Review each source file on its own, with the rest of the PR as read-only context.

    One file's failure never loses the others.
    """
    findings, skipped, errors = [], [], []
    to_review: list[tuple[str, str]] = []
    cost = 0.0
    for path, file_diff in split_by_file(diff_text):
        if path is None:
            skipped.append({"file": "(deleted file)", "reason": "deleted"})
        elif not file_diff:
            skipped.append({"file": path, "reason": "empty, binary or rename only"})
        elif not is_reviewable(path):
            skipped.append({"file": path, "reason": "not a reviewed file type"})
        elif (lines := file_diff.count("\n")) > max_lines:
            skipped.append({"file": path, "reason": f"{lines} diff lines > {max_lines}"})
        else:
            to_review.append((path, file_diff))
    for path, file_diff in to_review:
        extra = {
            "context": build_context(to_review, path, context_lines),
            "reported": summarize_reported(findings),
        }
        try:
            result = review(file_diff, **{k: v for k, v in extra.items() if v})
        except Exception as exc:  # noqa: BLE001 - report and continue with the next file
            errors.append({"file": path, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        cost += result.cost_usd
        findings.extend(result.findings)
    return {
        "reviewer_version": version,
        "findings": findings,
        "skipped": skipped,
        "errors": errors,
        "cost_usd": round(cost, 6),
    }


def select_notes(notes: list[dict], limit: int) -> list[dict]:
    """At most `limit` notes: minor before info, spread across files round-robin, so one
    file can't fill the comment."""
    rank = {"minor": 0, "info": 1}
    by_file: dict[str, list[dict]] = {}
    for n in sorted(notes, key=lambda n: rank.get(n.get("severity"), 2)):
        by_file.setdefault(str(n.get("file")), []).append(n)
    picked: list[dict] = []
    queues = list(by_file.values())
    while len(picked) < limit and any(queues):
        for q in queues:
            if q and len(picked) < limit:
                picked.append(q.pop(0))
    return picked


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]()#|~!])")


def md_text(value: object, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Model text as inert Markdown: no links, images, HTML, mentions or layout breaks."""
    text = _CONTROL.sub(" ", str(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _MD_SPECIAL.sub(r"\\\1", text)
    return text.replace("@", "@​")  # never notify anyone


def md_code(value: object) -> str:
    """A path or category inside a code span; backticks can't close it early."""
    return "`" + _CONTROL.sub(" ", str(value)).replace("`", "'")[:200] + "`"


def _finding_line(f: dict) -> str:
    where = md_code(f"{f.get('file')}:{f.get('line')}")
    return (f"- **{md_text(f.get('severity', '?'), 20)}** {md_code(f.get('category'))} "
            f"at {where}: {md_text(f.get('message', ''))}")


def render_comment(report: dict) -> str:
    findings = report["findings"]
    injection = [f for f in findings if f.get("category") == "injection"]
    gated = [f for f in findings if f.get("severity") in GATED and f not in injection]
    notes = [f for f in findings if f.get("severity") not in GATED and f not in injection]
    out = [
        COMMENT_MARKER,
        "### Dev Growth Copilot review (advisory)",
        "",
        f"Reviewer {md_text(report['reviewer_version'], 20)} · {len(gated)} finding(s), "
        f"{len(notes)} learning note(s) · cost ${report['cost_usd']:.4f}",
        "",
        "> Advisory only: this comment never blocks the merge. Each item asks a question "
        "instead of giving the fix. Try to answer it before you look anything up.",
    ]
    if injection:
        out += ["", "#### ⚠️ Text addressed to an AI reviewer",
                *(_finding_line(f) for f in injection)]
    if gated:
        out += ["", "#### Findings", *(_finding_line(f) for f in gated)]
    if notes:
        shown = select_notes(notes, MAX_COMMENT_NOTES)
        out += ["", "#### Learning notes", *(_finding_line(f) for f in shown)]
        if len(notes) > len(shown):
            out.append(f"- …and {len(notes) - len(shown)} more in the `review-findings` artifact")
    if not findings:
        out += ["", "No findings. Which edge case did you test last?"]
    if report["errors"]:
        out += ["", "#### Not reviewed (errors)",
                *(f"- {md_code(e['file'])}: {md_text(e['error'], 200)}" for e in report["errors"])]
    if report["skipped"]:
        out += ["", "<details><summary>Skipped files</summary>", "",
                *(f"- {md_code(s['file'])}: {md_text(s['reason'], 100)}"
                  for s in report["skipped"]),
                "", "</details>"]
    out += ["", "<sub>Findings are saved as the `review-findings` artifact. Import them into "
                "your skill map with `python -m skillmap.ingest findings.json`.</sub>"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--diff", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--out", type=Path, required=True, help="findings JSON")
    parser.add_argument("--comment", type=Path, help="write the PR comment Markdown here")
    parser.add_argument("--fail-on-injection", action="store_true")
    args = parser.parse_args(argv)

    from reviewer import claude

    mode = claude.load_mode("fast")
    report = review_pr(args.diff.read_text(encoding="utf-8"), claude.review,
                       mode["max_diff_lines"] or 400, mode["version"])
    report.update(repo=args.repo, commit_sha=args.sha)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.comment:
        args.comment.write_text(render_comment(report), encoding="utf-8")

    print(f"{len(report['findings'])} finding(s), {len(report['skipped'])} skipped, "
          f"{len(report['errors'])} error(s), ${report['cost_usd']:.4f}")
    injected = [f for f in report["findings"]
                if f.get("category") == "injection" and f.get("source") == "deterministic"]
    if args.fail_on_injection and injected:
        print(f"{len(injected)} deterministic injection finding(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
