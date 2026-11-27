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
    distilled INTEGER NOT NULL DEFAULT 0,
    tool_length INTEGER,
    tool_preview TEXT,
    payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload)),
    reasoning TEXT CHECK(reasoning IS NULL OR json_valid(reasoning)),
    content TEXT,
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
    memory_version INTEGER,
    FOREIGN KEY(memory_id,memory_version) REFERENCES memory_versions(memory_id,commit_id) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(inference_id, memory_id),
    UNIQUE(inference_id, position)
);
CREATE INDEX inferred_from ON inference_sources(memory_id);

CREATE TABLE audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL CHECK(json_valid(details)),
    reverts INTEGER REFERENCES audit(seq),
    memory_change TEXT CHECK(memory_change IS NULL OR json_valid(memory_change))
);
CREATE UNIQUE INDEX audit_reversal ON audit(reverts) WHERE reverts IS NOT NULL;

CREATE TABLE revisions (
    domain TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    reserved INTEGER CHECK(reserved IS NULL OR domain='evidence')
);
INSERT INTO revisions(domain,version) VALUES ('memories',0),('inferences',0),('candidates',0),('evidence',0);

CREATE TABLE jobs (
    kind TEXT NOT NULL CHECK(kind IN ('title', 'distillation', 'context', 'dream')),
    target TEXT NOT NULL,
    state TEXT,
    retry_at REAL,
    evidence_commit INTEGER,
    checkpoint TEXT NOT NULL CHECK(json_valid(checkpoint) AND json_type(checkpoint)='object'),
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

CREATE TABLE attachments (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size>0),
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

CREATE TABLE provider_pauses (
    provider TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    resume_at REAL
);

CREATE TABLE git_sync (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    state TEXT NOT NULL
);

CREATE TRIGGER attachment_removed AFTER DELETE ON attachments BEGIN INSERT OR IGNORE INTO file_cleanup VALUES('attachment',old.sha256); END;

CREATE TRIGGER reasoning_changed AFTER UPDATE OF reasoning ON session_events
WHEN new.reasoning IS NOT old.reasoning BEGIN
    UPDATE sessions SET revision=revision+1,history_revision=history_revision+1 WHERE id=new.session_id;
END;

CREATE TRIGGER topic_visibility AFTER UPDATE OF archived,title ON topics WHEN new.archived<>old.archived OR new.title<>old.title BEGIN
    UPDATE revisions SET version=version+1 WHERE domain='memories';
END;
