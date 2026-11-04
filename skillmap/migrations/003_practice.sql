-- Coach: practice exercises built from findings, and a spaced-repetition schedule per area.

CREATE TABLE practice (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    skill_area      TEXT    NOT NULL REFERENCES skill_areas(name),
    source_findings TEXT    NOT NULL,  -- JSON list of findings.id the exercise is built from
    title           TEXT    NOT NULL,
    exercise        TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    score           INTEGER CHECK (score BETWEEN 0 AND 5),
    feedback        TEXT,
    answered_at     TEXT
);

CREATE TABLE skill_schedule (
    skill_area    TEXT PRIMARY KEY REFERENCES skill_areas(name),
    interval_days REAL NOT NULL DEFAULT 1.0,
    next_due      TEXT NOT NULL
);
