CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
    title TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    last_message_at TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL DEFAULT 0,
    messages INTEGER NOT NULL DEFAULT 0,
    summaries INTEGER NOT NULL DEFAULT 0,
    undistilled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    history_revision INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX session_listing ON sessions(archived,last_message_at DESC,id ASC);

CREATE TABLE session_events (
    seq INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,
    kind TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    message_id TEXT,
    role TEXT,
    at TEXT NOT NULL,
    content TEXT,
    payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload)),
    distilled INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, message_id)
);
CREATE INDEX events_by_session ON session_events(session_id,seq);

CREATE TABLE topics (
    name TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1))
);

CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL REFERENCES topics,
    status TEXT NOT NULL CHECK(status IN ('active', 'deprecated')),
    text TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    context TEXT CHECK(json_valid(context) OR context IS NULL),
    supersedes TEXT REFERENCES memories DEFERRABLE INITIALLY DEFERRED,
    session_id TEXT,
    message_id TEXT,
    extensions TEXT NOT NULL CHECK(json_valid(extensions)),
    revision INTEGER NOT NULL DEFAULT 1,
    CHECK ((session_id IS NULL) = (message_id IS NULL)),
    FOREIGN KEY(session_id, message_id) REFERENCES session_events(session_id, message_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX memory_topic_status ON memories(topic,status);
CREATE INDEX memory_source ON memories(session_id,message_id);

CREATE TABLE candidates (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    text TEXT NOT NULL,
    question TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'rejected')),
    context TEXT,
    session_id TEXT,
    message_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    CHECK((session_id IS NULL)=(message_id IS NULL))
);
CREATE INDEX candidates_source ON candidates(session_id,state);

CREATE TABLE candidate_related (
    candidate_id TEXT NOT NULL REFERENCES candidates ON DELETE CASCADE,
    position INTEGER NOT NULL,
    memory_id TEXT NOT NULL,
    PRIMARY KEY(candidate_id, position),
    UNIQUE(candidate_id, memory_id)
);
CREATE INDEX related_memory ON candidate_related(memory_id);

CREATE TABLE inferences (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('hypothesis', 'confirmed', 'deprecated')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE inference_sources (
    inference_id TEXT NOT NULL REFERENCES inferences ON DELETE CASCADE,
    memory_id TEXT NOT NULL REFERENCES memories DEFERRABLE INITIALLY DEFERRED,
    position INTEGER NOT NULL,
    PRIMARY KEY(inference_id, memory_id),
    UNIQUE(inference_id, position)
);
CREATE INDEX inferred_from ON inference_sources(memory_id);

CREATE TABLE memory_changes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    change TEXT NOT NULL CHECK(json_valid(change))
);

CREATE TABLE audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL CHECK(json_valid(details))
);

CREATE TABLE revisions (
    domain TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);
INSERT INTO revisions VALUES ('memories',0),('inferences',0),('candidates',0);

CREATE TABLE jobs (
    kind TEXT NOT NULL CHECK(kind IN ('title', 'distillation', 'context', 'dream')),
    target TEXT NOT NULL,
    id TEXT,
    state TEXT,
    phase TEXT,
    retry_at REAL,
    generation INTEGER,
    failures INTEGER,
    version TEXT,
    through TEXT,
    partial TEXT,
    cursor TEXT CHECK(cursor IS NULL OR json_valid(cursor)),
    remaining_requests INTEGER,
    requests INTEGER,
    batch INTEGER,
    proposed INTEGER,
    updated_at TEXT,
    extra TEXT NOT NULL CHECK(json_valid(extra)),
    PRIMARY KEY(kind, target)
);

CREATE TABLE profile (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    body TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE undo_effects (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE,
    turn_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    before_row TEXT,
    after_row TEXT,
    prior_references TEXT NOT NULL,
    conflicted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, turn_id, entity, entity_id)
);

CREATE TABLE receipts (
    submission_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    instance TEXT NOT NULL,
    session_id TEXT,
    turn_id INTEGER,
    CHECK((session_id IS NULL)=(turn_id IS NULL))
);

CREATE TABLE attachment_objects (
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL
);

CREATE TABLE attachments (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    id TEXT NOT NULL,
    sha256 TEXT NOT NULL REFERENCES attachment_objects,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    PRIMARY KEY(session_id, message_id, id),
    FOREIGN KEY(session_id, message_id) REFERENCES session_events(session_id, message_id) ON DELETE CASCADE
);
CREATE INDEX object_refs ON attachments(sha256);

CREATE TABLE file_cleanup (
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    PRIMARY KEY(kind, object_id)
);

CREATE TABLE usage (
    provider TEXT NOT NULL,
    period TEXT NOT NULL,
    window TEXT NOT NULL,
    units INTEGER NOT NULL,
    PRIMARY KEY(provider, period)
);

CREATE TABLE quota_pauses (
    provider TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    resume_at REAL
);

CREATE TABLE git_sync (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    state TEXT NOT NULL
);

CREATE TABLE session_reasoning (
    event_id INTEGER PRIMARY KEY REFERENCES session_events ON DELETE CASCADE,
    body TEXT NOT NULL CHECK(json_valid(body))
);
CREATE TRIGGER attachment_removed AFTER DELETE ON attachments BEGIN INSERT OR IGNORE INTO file_cleanup VALUES('attachment',old.sha256); END;

CREATE TABLE attachment_reservations (
    token TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY(token, digest)
);

CREATE TRIGGER reasoning_changed AFTER UPDATE ON session_reasoning BEGIN
    UPDATE sessions SET revision=revision+1,history_revision=history_revision+1
    WHERE id=(SELECT session_id FROM session_events WHERE seq=new.event_id);
END;
CREATE TRIGGER reasoning_removed AFTER DELETE ON session_reasoning BEGIN
    UPDATE sessions SET revision=revision+1,history_revision=history_revision+1
    WHERE id=(SELECT session_id FROM session_events WHERE seq=old.event_id);
END;

CREATE TRIGGER topic_visibility AFTER UPDATE OF archived,title ON topics WHEN new.archived<>old.archived OR new.title<>old.title BEGIN
    UPDATE revisions SET version=version+1 WHERE domain='memories';
END;
