import io
import json
import urllib.request
import zipfile

import pytest

from skillmap import connect, migrate
from skillmap import sync as sync_mod
from skillmap.sync import SyncError, findings_from_zip, make_fetch, sync

REPO = "AlainAhuili/firmware-edge-ai"
REPORT = {
    "reviewer_version": "0.5.0", "repo": "firmware-edge-ai", "commit_sha": "abc",
    "findings": [
        {"file": "firmware/src/main.cpp", "line": 26, "category": "spec-gap",
         "severity": "minor", "message": "Does the 100 Hz period drift?"},
        {"file": "firmware/lib/x.h", "line": 35, "category": "off-by-one",
         "severity": "major", "message": "What does at(i) return for i >= size()?"},
    ],
}


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, artifacts: list[dict], zips: dict[int, bytes]):
        self.artifacts, self.zips, self.calls = artifacts, zips, []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if "/actions/artifacts?" in url:
            return json.dumps({"artifacts": self.artifacts}).encode()
        art_id = int(url.rsplit("/", 2)[-2])
        return self.zips[art_id]


def _db():
    conn = connect()
    migrate(conn)
    return conn


def _artifact(i, expired=False, name="review-findings"):
    return {"id": i, "name": name, "expired": expired}


def test_sync_imports_new_artifacts_once():
    gh = FakeGitHub([_artifact(1), _artifact(2)],
                    {1: _zip({"findings.json": json.dumps(REPORT).encode(),
                              "comment.md": b"x"}),
                     2: _zip({"findings.json": json.dumps({**REPORT, "commit_sha": "def"})
                              .encode()})})
    conn = _db()
    first = sync(conn, REPO, gh)
    # Artifact 2 is a re-review (other commit) with the same findings: counted once.
    assert (first["artifacts"], first["recorded"], first["present"]) == (2, 2, 2)
    downloads = [u for u in gh.calls if u.endswith("/zip")]
    second = sync(conn, REPO, gh)
    assert (second["artifacts"], second["skipped"], second["recorded"]) == (0, 2, 0)
    assert [u for u in gh.calls if u.endswith("/zip")] == downloads  # nothing re-downloaded


def test_expired_and_other_artifacts_are_ignored():
    gh = FakeGitHub([_artifact(1, expired=True), _artifact(2, name="firmware-esp32-s3")], {})
    assert sync(_db(), REPO, gh)["artifacts"] == 0


def test_bad_artifact_is_reported_not_fatal_and_retried_next_time():
    gh = FakeGitHub([_artifact(1), _artifact(2)],
                    {1: b"not a zip", 2: _zip({"findings.json": json.dumps(REPORT).encode()})})
    conn = _db()
    s = sync(conn, REPO, gh)
    assert (s["failed"], s["artifacts"]) == (1, 1)
    synced = {r[0] for r in conn.execute("SELECT artifact_id FROM synced_artifacts")}
    assert synced == {2}  # the failed one stays unsynced, so it's tried again


def test_zip_without_findings_or_too_large_is_rejected(monkeypatch):
    with pytest.raises(SyncError, match="no findings.json"):
        findings_from_zip(_zip({"comment.md": b"x"}))
    monkeypatch.setattr(sync_mod, "MAX_FINDINGS_BYTES", 10)
    with pytest.raises(SyncError, match="too large"):
        findings_from_zip(_zip({"findings.json": json.dumps(REPORT).encode()}))


def test_zip_entries_are_never_written_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    evil = _zip({"findings.json": json.dumps(REPORT).encode(), "../escape.txt": b"x"})
    assert findings_from_zip(evil)["repo"] == "firmware-edge-ai"
    assert list(tmp_path.iterdir()) == []


def test_repo_name_is_validated():
    with pytest.raises(SyncError):
        sync(_db(), "../../etc", FakeGitHub([], {}))


def test_fetch_refuses_non_api_urls_and_keeps_token_off_redirects(monkeypatch):
    fetch = make_fetch("t0ken")
    with pytest.raises(SyncError, match="only the GitHub API"):
        fetch("https://evil.example/steal")

    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["redirected"] = dict(req.headers)  # what a redirect would carry
        seen["unredirected"] = dict(req.unredirected_hdrs)
        return Resp(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetch(f"{sync_mod.API}/repos/{REPO}/actions/artifacts?name=x")
    assert seen["unredirected"]["Authorization"] == "Bearer t0ken"
    assert "Authorization" not in seen["redirected"]


def test_fetch_enforces_size_limit(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Resp(b"x" * 11))
    with pytest.raises(SyncError, match="larger than 10 bytes"):
        make_fetch("t", max_bytes=10)(f"{sync_mod.API}/x")


def test_token_from_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    assert sync_mod.resolve_token() == "from-env"


def test_rereview_of_a_pr_counts_each_issue_once_but_new_issues_count():
    later = {**REPORT, "commit_sha": "later", "findings": [
        *REPORT["findings"],
        {"file": "firmware/src/main.cpp", "line": 40, "category": "error-handling",
         "severity": "major", "message": "new issue in a later push"}]}
    gh = FakeGitHub([{**_artifact(2), "created_at": "2026-11-04T22:00:00Z"},
                     {**_artifact(1), "created_at": "2026-11-04T21:00:00Z"}],
                    {1: _zip({"findings.json": json.dumps(REPORT).encode()}),
                     2: _zip({"findings.json": json.dumps(later).encode()})})
    conn = _db()
    sync(conn, REPO, gh)
    rows = conn.execute("SELECT commit_sha, category FROM findings ORDER BY id").fetchall()
    assert rows == [("abc", "spec-gap"), ("abc", "off-by-one"), ("later", "error-handling")]
    # Dated by the review (artifact), not by the import, so weekly trends are right.
    dates = [r[0] for r in conn.execute("SELECT created_at FROM findings ORDER BY id")]
    assert dates == ["2026-11-04T21:00:00Z"] * 2 + ["2026-11-04T22:00:00Z"]


@pytest.mark.parametrize("value", [None, "yesterday", "2026-11-04 21:00:00", 12345])
def test_bad_or_missing_review_time_falls_back_to_now(value):
    conn = _db()
    sync_mod.ingest(conn, REPORT, reviewed_at=value)
    (when,) = conn.execute("SELECT DISTINCT created_at FROM findings").fetchone()
    assert when.startswith("20") and when.endswith("Z") and when != value
