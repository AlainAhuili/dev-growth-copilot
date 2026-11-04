-- Which CI artifacts skillmap.sync has already imported, so a sync only downloads new ones.

CREATE TABLE synced_artifacts (
    repo        TEXT    NOT NULL,
    artifact_id INTEGER NOT NULL,
    synced_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    recorded    INTEGER NOT NULL,
    PRIMARY KEY (repo, artifact_id)
);
