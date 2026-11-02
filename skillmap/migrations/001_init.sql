-- Skill map: every advisory finding becomes a data point about a skill to practise.

CREATE TABLE skill_areas (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE categories (
    name       TEXT PRIMARY KEY,
    skill_area TEXT NOT NULL REFERENCES skill_areas(name)
);

CREATE TABLE findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    agent         TEXT    NOT NULL CHECK (agent IN ('reviewer', 'coach', 'dojo')),
    agent_version TEXT    NOT NULL,
    stage         TEXT    NOT NULL CHECK (stage IN ('commit', 'acceptance', 'validation', 'production', 'metrics')),
    repo          TEXT    NOT NULL,
    commit_sha    TEXT,
    category      TEXT    NOT NULL REFERENCES categories(name),
    severity      TEXT    NOT NULL CHECK (severity IN ('info', 'minor', 'major', 'critical')),
    file          TEXT,
    line          INTEGER,
    message       TEXT    NOT NULL
);

CREATE INDEX idx_findings_category_time ON findings(category, created_at);

INSERT INTO skill_areas (name, description) VALUES
    ('correctness', 'Logic, boundaries and error handling'),
    ('memory',      'Memory safety and resource lifetimes (firmware)'),
    ('concurrency', 'Tasks, ISRs, locks and shared state'),
    ('security',    'Untrusted input, secrets and supply chain'),
    ('testing',     'Executable specs and test design'),
    ('design',      'Architecture, coupling and readability'),
    ('operations',  'Debugging, rollouts and incident response');

INSERT INTO categories (name, skill_area) VALUES
    ('off-by-one',         'correctness'),
    ('error-handling',     'correctness'),
    ('null-deref',         'memory'),
    ('buffer-overflow',    'memory'),
    ('resource-leak',      'memory'),
    ('race-condition',     'concurrency'),
    ('injection',          'security'),
    ('secret-exposure',    'security'),
    ('missing-test',       'testing'),
    ('spec-gap',           'testing'),
    ('coupling',           'design'),
    ('rollout-failure',    'operations');
