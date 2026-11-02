"""Read-only git helpers. The agents only ever see diffs, never write to the repo."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def diff(base: str, head: str = "HEAD", repo: str | Path = ".") -> str:
    """Unified diff of `head` against its merge base with `base`."""
    return subprocess.run(
        ["git", "diff", "--unified=3", f"{base}...{head}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def changed_files(diff_text: str) -> list[str]:
    """Files added or modified in a unified diff (deleted files are skipped)."""
    return _DIFF_FILE.findall(diff_text)
