import json

from skillmap import connect, migrate
from skillmap.ingest import ingest
from skillmap.ingest import main as ingest_main
from skillmap.report import main as report_main
from skillmap.report import top_categories

REPORT = {
    "reviewer_version": "0.3.0",
    "repo": "firmware-edge-ai",
    "commit_sha": "abc123",
    "findings": [
        {"file": "main/a.c", "line": 16, "category": "off-by-one", "severity": "major",
         "message": "Which index is the last valid one?"},
        {"file": "main/a.c", "line": 14, "category": "missing-test", "severity": "minor",
         "message": "Which test shows the boundary?"},
        {"file": "main/a.c", "line": 1, "category": "made-up", "severity": "major",
         "message": "x"},
        {"file": "main/a.c", "line": 2, "category": "coupling", "severity": "blocker",
         "message": "unknown severity"},
    ],
}


def _db():
    conn = connect()
    migrate(conn)
    return conn


def test_ingest_records_known_and_rejects_bad_rows():
    conn = _db()
    assert ingest(conn, REPORT) == (2, 0, 2)
    rows = conn.execute("SELECT agent, stage, repo, commit_sha, category FROM findings").fetchall()
    assert ("reviewer", "commit", "firmware-edge-ai", "abc123", "off-by-one") in rows


def test_ingest_is_idempotent():
    conn = _db()
    ingest(conn, REPORT)
    assert ingest(conn, REPORT) == (0, 2, 2)
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 2


def test_top_categories_ignores_notes():
    conn = _db()
    ingest(conn, REPORT)
    assert top_categories(conn) == [("off-by-one", "correctness", 1)]


def test_cli_roundtrip(tmp_path, capsys):
    f = tmp_path / "findings.json"
    f.write_text(json.dumps(REPORT))
    db = tmp_path / "skill.db"
    assert ingest_main([str(f), "--db", str(db)]) == 0
    assert "2 recorded" in capsys.readouterr().out
    assert report_main(["--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "correctness" in out and "off-by-one" in out


def test_report_on_empty_db(tmp_path, capsys):
    assert report_main(["--db", str(tmp_path / "empty.db")]) == 0
    assert "No findings recorded yet" in capsys.readouterr().out
