"""会话文本检索和沉淀游标的 SQLite 投影。"""

from __future__ import annotations

import sqlite3
from itertools import pairwise

PENDING_USER = "active=1 AND kind='message' AND role='user' AND distilled=0"
# SQLite 整数有符号。每组使用两个 63 位字，避免截断符号位。
_BITS = 126
_FULL = (1 << _BITS) - 1
_WORD = (1 << 63) - 1


def search_signature(text: str) -> tuple[int, int, int, int]:
    """字符和相邻字符各占 126 位，碰撞只会多查候选，最终仍核对原文。"""
    # Python 正则忽略大小写也把带点和不带点的土耳其语 i 视为等价。
    if len(text) > 32768:
        # 超长输出只建字符签名，约束发送时的 CPU 成本；相邻字符仍由原文核对。
        chars = 0
        if text.isascii():
            for code in range(128):
                if chr(code) in text:
                    chars |= 1 << (ord(chr(code).lower()) % _BITS)
        else:
            for pos in range(0, len(text), 8192):
                block = text[pos : pos + 8192].replace("İ", "i").replace("ı", "i").casefold()
                for char in set(block):
                    chars |= 1 << (ord(char) % _BITS)
                if chars == _FULL:
                    break
        return chars & _WORD, chars >> 63, _WORD, _WORD
    folded = text.replace("İ", "i").replace("ı", "i").casefold()
    chars = pairs = 0
    # 分段限制长消息的暂存内存；相邻分段多保留一个字符以覆盖边界。
    for pos in range(0, len(folded), 2048):
        block = folded[max(0, pos - 1) : pos + 2048]
        if chars != _FULL:
            for char in set(block):
                chars |= 1 << (ord(char) % _BITS)
        if pairs != _FULL:
            for left, right in set(pairwise(block)):
                pairs |= 1 << (((ord(left) * 257) ^ (ord(right) * 65599)) % _BITS)
        if chars == pairs == _FULL:
            break
    return chars & _WORD, chars >> 63, pairs & _WORD, pairs >> 63


def index_message(db: sqlite3.Connection, event_id: int, text: str) -> None:
    """消息与它的搜索签名在同一次写入中生效。"""
    db.execute(
        "UPDATE search_signatures SET c0=?,c1=?,p0=?,p1=?,ready=1 WHERE event_id=?",
        (*search_signature(text), event_id),
    )


def install(db: sqlite3.Connection) -> None:
    db.execute(f"CREATE INDEX pending_users ON session_events(session_id,seq) WHERE {PENDING_USER}")
    db.execute("""CREATE INDEX visible_history ON session_events(session_id,seq)
        WHERE active=1 AND kind IN ('message','assistant','tool','summary','turn_failed')""")
    db.execute(
        "CREATE INDEX latest_summary ON session_events(session_id,seq) WHERE active=1 AND kind='summary'"
    )
    db.execute("CREATE INDEX chat_search_order ON sessions(archived,started_at DESC,id DESC)")
    # 默认全位匹配可在同一事务中立即搜索刚追加或修改的正文。所有触发器仅使用标准 SQL，
    # 直接操作数据库的工具同样可写，且不依赖可选分词器或连接专属函数。
    db.execute("""CREATE TABLE search_signatures (
        started_at TEXT NOT NULL, session_id TEXT NOT NULL, event_id INTEGER NOT NULL,
        c0 INTEGER NOT NULL DEFAULT -1, c1 INTEGER NOT NULL DEFAULT -1,
        p0 INTEGER NOT NULL DEFAULT -1, p1 INTEGER NOT NULL DEFAULT -1,
        ready INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(started_at,session_id,event_id)
    ) WITHOUT ROWID""")
    db.execute("CREATE UNIQUE INDEX search_event ON search_signatures(event_id)")
    db.execute("CREATE INDEX search_pending ON search_signatures(event_id) WHERE ready=0")
    insert_new = """INSERT INTO search_signatures(started_at,session_id,event_id)
        SELECT started_at,new.session_id,new.seq FROM sessions
        WHERE id=new.session_id AND new.kind IN ('message','assistant') AND new.content IS NOT NULL;"""
    delete_old = "DELETE FROM search_signatures WHERE event_id=old.seq;"
    db.execute("""CREATE TRIGGER search_reorder AFTER UPDATE OF started_at ON sessions
        WHEN new.started_at!=old.started_at BEGIN UPDATE search_signatures SET started_at=new.started_at
        WHERE started_at=old.started_at AND session_id=old.id; END""")
    db.execute(
        f"CREATE TRIGGER search_insert AFTER INSERT ON session_events BEGIN {insert_new} END"
    )
    db.execute(
        f"CREATE TRIGGER search_delete AFTER DELETE ON session_events BEGIN {delete_old} END"
    )
    db.execute(f"""CREATE TRIGGER search_update AFTER UPDATE OF content,kind,session_id,seq
        ON session_events BEGIN {delete_old} {insert_new} END""")
