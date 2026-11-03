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
    assert {e.category for e in case.expected} <= known
