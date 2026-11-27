"""把选定的源文件冻结在 SQLite 中，业务解析和归档读取同一份内容。"""

import hashlib
import os
from pathlib import Path

from syvrio.infra.storage.database import encode


def fingerprint(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def members(vault, runtime):
    for base, prefix, selected in (
        (vault, "vault/", None),
        (runtime, "runtime/", ("session-reasoning", "cache/profile")),
    ):
        if base is None:
            continue
        roots = [base] if selected is None else [base / n for n in selected if (base / n).exists()]
        for root in roots:
            for folder, dirs, names in os.walk(root, onerror=raise_error):
                here = Path(folder)
                if here.resolve() != here:
                    raise ValueError(f"源目录重定向: {here}")
                dirs[:] = sorted(d for d in dirs if d not in (".git", ".incoming", ".locks"))
                for name in sorted(names):
                    if name == ".git" or name.endswith(".lock"):
                        continue
                    path = here / name
                    if path.resolve() != path or not path.is_file():
                        raise ValueError(f"源文件重定向或不是普通文件: {path}")
                    yield prefix + path.relative_to(base).as_posix(), path


def raise_error(exc):
    raise exc


class Snapshot:
    def __init__(self, db):
        self.db = db

    def capture(self, sources):
        self.db.executescript("""
            CREATE TABLE objects(id INTEGER PRIMARY KEY, digest TEXT UNIQUE, content BLOB NOT NULL);
            CREATE TABLE files(source INTEGER, path TEXT, digest TEXT REFERENCES objects(digest),
                               PRIMARY KEY(source,path));
        """)
        before = {}
        for index, (vault, runtime) in enumerate(sources):
            for name, path in members(vault, runtime):
                stat = path.stat()
                before[index, name] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                with path.open("rb") as f:
                    digest = hashlib.file_digest(f, "sha256").hexdigest()
                    if not self.db.execute(
                        "SELECT 1 FROM objects WHERE digest=?", (digest,)
                    ).fetchone():
                        row = self.db.execute(
                            "INSERT INTO objects(digest,content) VALUES(?,zeroblob(?))",
                            (digest, stat.st_size),
                        )
                        with self.db.blobopen("objects", "content", row.lastrowid) as blob:
                            check = hashlib.sha256()
                            f.seek(0)
                            while data := f.read(1024 * 1024):
                                blob.write(data)
                                check.update(data)
                        if check.hexdigest() != digest:
                            raise ValueError(f"读取期间文件发生变化: {name}")
                self.db.execute("INSERT INTO files VALUES(?,?,?)", (index, name, digest))
        after = {}
        for index, (vault, runtime) in enumerate(sources):
            for name, path in members(vault, runtime):
                st = path.stat()
                after[index, name] = st.st_size, st.st_mtime_ns, st.st_ino
        if before != after:
            raise ValueError("采集期间源文件有变动，请在源部署空闲时重试")
        self.db.commit()
        self.paths = {
            (i, p): d for i, p, d in self.db.execute("SELECT * FROM files ORDER BY source,path")
        }

    def text(self, index, path):
        digest = self.paths[index, path]
        (content,) = self.db.execute(
            "SELECT content FROM objects WHERE digest=?", (digest,)
        ).fetchone()
        return content.decode("utf-8-sig")

    def blob(self, digest, size):
        row = self.db.execute(
            "SELECT id,length(content) FROM objects WHERE digest=?", (digest,)
        ).fetchone()
        if not row or row[1] != size:
            raise ValueError(f"附件原件缺失或大小不同: {digest}")
        return self.db.blobopen("objects", "content", row[0], readonly=True)
