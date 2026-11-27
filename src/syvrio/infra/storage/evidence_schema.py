"""创建正式记忆、推断与主题的证据历史及事务内写入触发器。"""

import sqlite3


def install(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE memory_versions(
        memory_id TEXT NOT NULL, commit_id INTEGER NOT NULL, topic TEXT NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL, recorded_at TEXT NOT NULL,
        context TEXT, supersedes TEXT, session_id TEXT, message_id TEXT,
        extensions TEXT NOT NULL, revision INTEGER NOT NULL,
        changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f+00:00','now')),
        PRIMARY KEY(memory_id,commit_id),
        FOREIGN KEY(session_id,message_id) REFERENCES session_events(session_id,message_id)
            DEFERRABLE INITIALLY DEFERRED) WITHOUT ROWID""")
    db.execute("CREATE INDEX version_origin ON memory_versions(session_id,message_id)")
    db.execute("""CREATE TABLE topic_versions(
        name TEXT NOT NULL, commit_id INTEGER NOT NULL, archived INTEGER NOT NULL,
        PRIMARY KEY(name,commit_id)) WITHOUT ROWID""")
    db.execute("""CREATE TABLE inference_versions(
        inference_id TEXT NOT NULL, commit_id INTEGER NOT NULL, text TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(inference_id,commit_id)) WITHOUT ROWID""")
    # 来源随推断正文保持不可变；人工更正推断时原有逻辑会新建 ID。
    fields = (
        "topic,text,status,recorded_at,context,supersedes,session_id,message_id,extensions,revision"
    )
    _track(db, "memories", "id", "memory_versions", fields)
    _track(db, "inferences", "id", "inference_versions", "text,kind,status,created_at,updated_at")
    _track(db, "topics", "name", "topic_versions", "archived")


def _track(db, table, key, history, fields):
    columns = fields.split(",")
    values = ",".join("NEW." + column for column in columns)
    changed = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
    history_key = {"memories": "memory_id", "inferences": "inference_id", "topics": "name"}[table]
    for operation in ("INSERT", "UPDATE"):
        event = operation if operation == "INSERT" else f"UPDATE OF {fields}"
        guard = "" if operation == "INSERT" else f"WHEN {changed}"
        # BEFORE INSERT 在 UPSERT 命中更新分支时也会执行。已有行交给 UPDATE 触发器记录。
        if operation == "INSERT":
            guard = f"WHEN NOT EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key})"
        db.execute(f"""CREATE TRIGGER history_{table}_{operation} BEFORE {event} ON {table}
            {guard} BEGIN
                UPDATE revisions SET version=coalesce(reserved,version+1) WHERE domain='evidence';
                INSERT INTO {history}({history_key},commit_id,{fields}) SELECT NEW.{key},version,{values} FROM revisions WHERE domain='evidence'
                ON CONFLICT DO UPDATE SET {",".join(f"{c}=excluded.{c}" for c in columns)};
            END""")
