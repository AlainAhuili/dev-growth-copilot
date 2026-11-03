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

Review = Callable[[str], ReviewResult]


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


def review_pr(diff_text: str, review: Review, max_lines: int, version: str) -> dict:
    """Review each source file on its own. One file's failure never loses the others."""
    findings, skipped, errors = [], [], []
    cost = 0.0
    for path, file_diff in split_by_file(diff_text):
        if path is None:
            skipped.append({"file": "(deleted file)", "reason": "deleted"})
            continue
        if not file_diff:
            skipped.append({"file": path, "reason": "empty, binary or rename only"})
            continue
        if not is_reviewable(path):
            skipped.append({"file": path, "reason": "not a reviewed file type"})
            continue
        lines = file_diff.count("\n")
        if lines > max_lines:
            skipped.append({"file": path, "reason": f"{lines} diff lines > {max_lines}"})
            continue
        try:
            result = review(file_diff)
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
        out += ["", "#### Learning notes", *(_finding_line(f) for f in notes)]
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
