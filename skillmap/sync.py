"""Import every `review-findings` artifact of a repo from GitHub into the skill map.

    python -m skillmap.sync AlainAhuili/firmware-edge-ai [--db ~/.dgc/skillmap.db]

Token: GITHUB_TOKEN or GH_TOKEN, else `gh auth token`. Only artifacts not synced before are
downloaded. Each one is read in memory: only findings.json is opened, with a size limit,
and nothing from the zip is written to disk.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from skillmap.db import connect, migrate
from skillmap.ingest import DEFAULT_DB, ingest

API = "https://api.github.com"
ARTIFACT_NAME = "review-findings"
MAX_ZIP_BYTES = 10 * 1024 * 1024
MAX_FINDINGS_BYTES = 5 * 1024 * 1024
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# fetch(url) -> bytes. Injected so tests never touch the network.
Fetch = Callable[[str], bytes]


class SyncError(RuntimeError):
    pass


def resolve_token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,  # noqa: S603,S607
                             check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    raise SyncError("No GitHub token: set GITHUB_TOKEN, or log in with `gh auth login`.")


def make_fetch(token: str, max_bytes: int = MAX_ZIP_BYTES) -> Fetch:
    def fetch(url: str) -> bytes:
        if not url.startswith(API + "/"):
            raise SyncError(f"refusing to call {url!r}: only the GitHub API is used")
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dev-growth-copilot-skillmap-sync",
        })
        # Unredirected: the token goes to api.github.com only. Artifact downloads redirect
        # to storage with a signed URL, which must not receive the token.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - https API only
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SyncError(f"response from {url} is larger than {max_bytes} bytes")
        return data

    return fetch


def list_artifacts(repo: str, fetch: Fetch) -> list[dict]:
    """All unexpired review-findings artifacts, newest first."""
    out, page = [], 1
    while True:
        data = json.loads(fetch(f"{API}/repos/{repo}/actions/artifacts"
                                f"?name={ARTIFACT_NAME}&per_page=100&page={page}"))
        batch = data.get("artifacts", [])
        out += [a for a in batch if a.get("name") == ARTIFACT_NAME and not a.get("expired")]
        if len(batch) < 100:
            return out
        page += 1


def findings_from_zip(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        try:
            info = zf.getinfo("findings.json")
        except KeyError as exc:
            raise SyncError("artifact has no findings.json") from exc
        if info.file_size > MAX_FINDINGS_BYTES:
            raise SyncError("findings.json is too large")
        return json.loads(zf.read(info))


def sync(conn, repo: str, fetch: Fetch) -> dict[str, int]:
    if not _REPO_RE.match(repo):
        raise SyncError(f"not an owner/name repository: {repo!r}")
    done = {r[0] for r in conn.execute(
        "SELECT artifact_id FROM synced_artifacts WHERE repo = ?", (repo,))}
    stats = {"artifacts": 0, "skipped": 0, "recorded": 0, "present": 0, "rejected": 0,
             "failed": 0}
    # Oldest first, so the first time an issue appears is the one recorded.
    for art in sorted(list_artifacts(repo, fetch), key=lambda a: a.get("created_at", "")):
        if art["id"] in done:
            stats["skipped"] += 1
            continue
        try:
            report = findings_from_zip(
                fetch(f"{API}/repos/{repo}/actions/artifacts/{int(art['id'])}/zip"))
        except (SyncError, zipfile.BadZipFile, json.JSONDecodeError, OSError) as exc:
            print(f"  artifact {art['id']}: skipped ({exc})", file=sys.stderr)
            stats["failed"] += 1
            continue
        recorded, present, rejected = ingest(conn, report, across_commits=True)
        with conn:
            conn.execute(
                "INSERT INTO synced_artifacts (repo, artifact_id, recorded) VALUES (?, ?, ?)",
                (repo, int(art["id"]), recorded))
        stats["artifacts"] += 1
        stats["recorded"] += recorded
        stats["present"] += present
        stats["rejected"] += rejected
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repos", nargs="+", help="owner/name")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)

    try:
        fetch = make_fetch(resolve_token())
    except SyncError as exc:
        print(exc, file=sys.stderr)
        return 2
    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    migrate(conn)
    for repo in args.repos:
        try:
            s = sync(conn, repo, fetch)
        except SyncError as exc:
            print(f"{repo}: {exc}", file=sys.stderr)
            return 2
        print(f"{repo}: {s['artifacts']} new artifact(s), {s['recorded']} finding(s) recorded, "
              f"{s['skipped']} already synced, {s['failed']} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
