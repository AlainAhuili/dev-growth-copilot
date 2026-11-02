import sqlite3

import pytest

from skillmap import Finding, connect, migrate, mistake_counts, record_finding


@pytest.fixture
def conn():
    c = connect()
    migrate(c)
    return c


def _finding(category="off-by-one", **kw):
    base = dict(
        agent="reviewer",
        agent_version="0.1.0",
        stage="commit",
        repo="edge-ai",
        category=category,
        severity="major",
        message="Is head ever equal to RB_SIZE here?",
    )
    return Finding(**{**base, **kw})


def test_migrate_is_idempotent():
    c = connect()
    assert migrate(c) == [1]
    assert migrate(c) == []


def test_mistake_counts_group_by_skill_area(conn):
    record_finding(conn, _finding("off-by-one"))
    record_finding(conn, _finding("error-handling"))
    record_finding(conn, _finding("resource-leak"))
    assert mistake_counts(conn) == {"correctness": 2, "memory": 1}


def test_mistake_counts_since_filters_old_findings(conn):
    record_finding(conn, _finding())
    assert mistake_counts(conn, since="2999-01-01T00:00:00Z") == {}


def test_unknown_category_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        record_finding(conn, _finding("not-a-category"))


def test_unknown_stage_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        record_finding(conn, _finding(stage="nightly"))
