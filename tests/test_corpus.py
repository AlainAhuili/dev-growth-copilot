"""Corpus integrity: every expected finding must point at a line the diff adds.

A wrong line number in a case would silently lower the catch rate of a correct reviewer.
"""

import re

import pytest

from evals.runner import EVALS_DIR, load_suite
from skillmap import connect, migrate

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff_text: str) -> set[tuple[str, int]]:
    added, file, line = set(), None, 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            file = raw[6:]
        elif m := HUNK.match(raw):
            line = int(m.group(1))
        elif raw.startswith("+") and not raw.startswith("+++"):
            added.add((file, line))
            line += 1
        elif raw.startswith(" ") or raw == "":
            line += 1
    return added


CASES = [c for suite in ("seeded-bugs", "injection") for c in load_suite(EVALS_DIR / suite)]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_expected_findings_point_at_added_lines(case):
    added = added_lines(case.diff)
    for exp in case.expected:
        assert (exp.file, exp.line) in added, exp
        if exp.end_line is not None:
            assert exp.end_line > exp.line, exp
            assert (exp.file, exp.end_line) in added, exp


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_expected_categories_exist_in_skill_map(case):
    conn = connect()
    migrate(conn)
    known = {row[0] for row in conn.execute("SELECT name FROM categories")}
    assert {cat for e in case.expected for cat in e.categories} <= known


HUNK_FULL = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_hunk_headers_match_their_line_counts(case):
    """A wrong count makes every parser (and the reviewer) misread the diff."""
    lines = case.diff.splitlines()
    i = 0
    hunks = 0
    while i < len(lines):
        m = HUNK_FULL.match(lines[i])
        if not m:
            i += 1
            continue
        hunks += 1
        old_left, new_left = int(m.group(1) or 1), int(m.group(2) or 1)
        i += 1
        while i < len(lines) and (old_left > 0 or new_left > 0):
            raw = lines[i]
            if raw.startswith("\\"):
                pass
            elif raw.startswith("+"):
                new_left -= 1
            elif raw.startswith("-"):
                old_left -= 1
            else:
                old_left -= 1
                new_left -= 1
            i += 1
        assert (old_left, new_left) == (0, 0), f"hunk {hunks}: counts off by {old_left, new_left}"
        assert i == len(lines) or lines[i].startswith(("@@", "diff --git")), \
            f"hunk {hunks}: lines after the counted hunk"
    assert hunks >= 1


def test_corpus_has_clean_cases():
    """Without clean cases, a reviewer that flags everything would look perfect."""
    clean = [c for c in CASES if not c.expected]
    assert len(clean) >= 3
