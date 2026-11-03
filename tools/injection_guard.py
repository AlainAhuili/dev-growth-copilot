"""Deterministic prompt-injection guard for diffs the reviewer reads.

Two layers in front of the model:
1. `scan(diff)` flags added lines that give instructions to an AI reviewer. These are
   `injection` findings no model output can remove, and the only reviewer findings
   allowed to block CI (they're deterministic).
2. `wrap_untrusted(diff)` puts the diff in a block with a random tag the diff author
   can't guess, and neutralises attempts inside the diff to close that block.

Precision matters more than recall here because the scan can block a pipeline: a
comment like "// Edge AI, runs on the ESP32" must never trigger it. Patterns require
an instruction aimed at a reviewer, not just an AI-related word. The model is the
second line of defence for subtler attempts.
"""

from __future__ import annotations

import re
import secrets
import sys
from collections.abc import Iterator
from pathlib import Path

# Reviewer roles only: "AI agent" or "LLM model" are normal words in an edge-AI codebase.
_REVIEWER = r"(?:ai|llm|automated|bot|code)[ _-]?(?:reviewers?|review\s+bot|scanners?)"
_PATTERNS = [
    # classic jailbreak phrasing
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|above|earlier|system)\s+(?:instructions?|rules|prompts?|guidelines)",
    r"\byou\s+are\s+now\b",
    # dictating the verdict
    r"\b(?:approve|lgtm|merge)\s+(?:this|the)\s+(?:pr|pull\s+request|change|diff|commit|mr)\b",
    r"\breport\s+(?:no|zero)\s+(?:findings|issues|problems|vulnerabilities|bugs)\b",
    r"\b(?:do\s+not|don'?t|never)\s+(?:report|flag|mention)\s+(?:this|these|any)\b",
    r"[\"']verdict[\"']\s*:",
    # addressing a reviewer, then giving it an order
    rf"\b{_REVIEWER}\b.{{0,60}}\b(?:must|should|shall|approve|ignore|skip|do\s+not|don'?t)\b",
    # fake prompt structure
    r"</?\s*(?:system|instructions?)\s*>",
]
_INJECTION = re.compile("|".join(_PATTERNS), re.IGNORECASE)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def normalize_path(path: str | None) -> str | None:
    """Make 'b/main/x.c', './main/x.c' and 'main\\x.c' comparable."""
    if path is None:
        return None
    path = path.strip().replace("\\", "/")
    for prefix in ("a/", "b/", "./"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path or None


def added_lines(diff_text: str) -> Iterator[tuple[str | None, int, str]]:
    """Yield (file, new_line_number, text) for every added line.

    Tracks hunk lengths, so an added line whose content starts with '++' (C++ `++i`)
    isn't mistaken for a '+++ file' header, and '\\ No newline' markers are skipped.
    """
    file: str | None = None
    old_left = new_left = line = 0
    for raw in diff_text.splitlines():
        if old_left <= 0 and new_left <= 0:
            if raw.startswith("+++ "):
                path = raw[4:].strip()
                file = None if path == "/dev/null" else normalize_path(path)
            elif m := _HUNK.match(raw):
                old_left = int(m.group(2) or 1)
                line = int(m.group(3))
                new_left = int(m.group(4) or 1)
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            yield file, line, raw[1:]
            line += 1
            new_left -= 1
        elif raw.startswith("-"):
            old_left -= 1
        else:  # context line (an empty line is a context line with its space stripped)
            line += 1
            new_left -= 1
            old_left -= 1


def scan(diff_text: str) -> list[dict]:
    """Injection findings in the runner's format. A multi-line comment is one finding."""
    findings: list[dict] = []
    prev: tuple[str | None, int] | None = None
    for file, line, text in added_lines(diff_text):
        if not _INJECTION.search(text):
            continue
        if prev and prev[0] == file and line == prev[1] + 1:
            prev = (file, line)
            continue
        prev = (file, line)
        findings.append({
            "file": file,
            "line": line,
            "category": "injection",
            "severity": "critical",
            "source": "deterministic",
            "message": (
                "This added text gives instructions to an automated reviewer. Who is it "
                "written for, and what would an agent with write access do if it obeyed?"
            ),
        })
    return findings


def wrap_untrusted(diff_text: str) -> tuple[str, str]:
    """Return (wrapped_text, tag). The random tag can't be guessed by the diff author."""
    tag = f"untrusted_diff_{secrets.token_hex(6)}"
    safe = re.sub(r"</?\s*untrusted_diff[^>]*>", "[removed-tag]", diff_text, flags=re.I)
    return f"<{tag}>\n{safe}\n</{tag}>", tag


def main(argv: list[str]) -> int:
    """`python -m tools.injection_guard pr.diff` exits 1 if an injection attempt is found."""
    text = Path(argv[0]).read_text(encoding="utf-8") if argv else sys.stdin.read()
    findings = scan(text)
    for f in findings:
        print(f"::error file={f['file']},line={f['line']}::possible prompt injection")
    print(f"{len(findings)} injection finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
