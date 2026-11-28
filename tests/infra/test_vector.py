from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from threading import Barrier, Event

import numpy as np
import pytest

from syvrio.infra.config import Config, EmbeddingConfig, RetrievalConfig
from syvrio.infra.retrieval import (
    HybridRetriever,
    KeywordRetriever,
    SearchHit,
    VectorRetriever,
)
from syvrio.infra.storage.cache_database import cache_transaction
from syvrio.infra.storage.database import database_path, transaction
from syvrio.infra.vault.files import initialize_vault
from syvrio.infra.vault.sync import GitSyncService
from syvrio.memory import service as service_module
from syvrio.memory.store import MemoryStore

pytestmark = pytest.mark.retrieval

# 同义词组：组内任一词命中即贡献该维度方向，模拟语义相近。
GROUPS = {
    "housing": ["房", "住", "租"],
    "coffee": ["咖啡", "清淡", "茶"],
    "work": ["工作", "后端"],
}


class FakeEmbedder:
    """确定性假 Embedder；记录条目/查询侧调用，fail=True 模拟后端不可用。"""

    def __init__(self, groups: dict[str, list[str]], model_id: str = "fake", fail: bool = False):
        self.model_id = model_id
        self.groups = groups
        self.fail = fail
        self.passages: list[str] = []
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        if self.fail:
            raise RuntimeError("embedding 不可用")
        vector = np.zeros(len(self.groups), dtype=np.float32)
        for index, words in enumerate(self.groups.values()):
            if any(word in text for word in words):
                vector[index] = 1.0
        return vector.tolist()

    def embed(self, texts) -> list[list[float]]:
        self.passages.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)


def persisted_vector(store, embedder, **kwargs):
    return VectorRetriever(
        store, embedder, paths=store.paths, current_revision=store.revision, **kwargs
    )


def vector_rows(paths):
    with cache_transaction(paths, "vectors", write=False) as db:
        return db.execute("SELECT * FROM vectors ORDER BY memory_id,model").fetchall()


def seeded_store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path)
    store.append("housing", "在找市区的房子，预算 4000 以内")
    store.append("preferences", "咖啡偏好清淡、像茶的风味")
    return store


def test_vector_search_finds_paraphrase(tmp_path):
    store = seeded_store(tmp_path)
    retriever = persisted_vector(store, FakeEmbedder(GROUPS))

    hits = retriever.search("住的地方", limit=3)

    assert hits and hits[0].topic == "general/housing"


def test_incremental_sync_embeds_only_new_entries(tmp_path):
    store = seeded_store(tmp_path)
    embedder = FakeEmbedder(GROUPS)
    retriever = persisted_vector(store, embedder)

    retriever.search("房子", 3)
    assert len(embedder.passages) == 2
    retriever.search("房子", 3)
    assert len(embedder.passages) == 2

    store.append("career", "在找后端工作")
    retriever.search("工作", 3)
    assert len(embedder.passages) == 3


def test_vector_cache_writes_only_changed_rows(tmp_path):
    store = seeded_store(tmp_path)
    retriever = persisted_vector(store, FakeEmbedder(GROUPS))
    retriever.sync()
    with cache_transaction(store.paths, "vectors") as db:
        db.execute("CREATE TABLE updated_vectors(id TEXT)")
        db.execute("""CREATE TRIGGER count_vector_updates AFTER UPDATE ON vectors BEGIN
                      INSERT INTO updated_vectors VALUES(new.memory_id); END""")
    with transaction(store.paths) as db:
        changed = store.entries()[0]
        db.execute("UPDATE memories SET text=text||' updated' WHERE id=?", (changed.id,))
    retriever.sync()
    with cache_transaction(store.paths, "vectors", write=False) as db:
        assert [r[0] for r in db.execute("SELECT id FROM updated_vectors")] == [changed.id]


def test_vector_cache_reuses_each_input_digest_when_persisting(tmp_path, monkeypatch):
    from syvrio.infra.retrieval import vector

    store = seeded_store(tmp_path)
    original = vector.text_hash
    inputs = []

    def counted(text):
        inputs.append(text)
        return original(text)

    monkeypatch.setattr(vector, "text_hash", counted)
    persisted_vector(store, FakeEmbedder(GROUPS)).sync()
    assert len(inputs) == len(set(inputs)) == 2
    assert {row["input_digest"] for row in vector_rows(store.paths)} == {
        original(text) for text in inputs
    }


def test_query_embedding_does_not_hold_index_lock(tmp_path):
    store = seeded_store(tmp_path)
    simultaneous = Barrier(3)

    class ParallelEmbedder(FakeEmbedder):
        def embed_query(self, text):
            simultaneous.wait(timeout=5)
            return super().embed_query(text)

    retriever = persisted_vector(store, ParallelEmbedder(GROUPS))
    retriever.sync()
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(retriever.search, ["咖啡"] * 3))
    assert all(hits and hits[0].topic == "general/preferences" for hits in results)


def test_rolled_back_recall_cannot_publish_keyword_or_vector_indexes(tmp_path):
    store = seeded_store(tmp_path)
    keyword = KeywordRetriever(store)
    vector = persisted_vector(store, FakeEmbedder(GROUPS))
    for retriever in (keyword, vector):
        assert retriever.search("工作") == []
    with pytest.raises(RuntimeError, match="rollback"), transaction(store.paths):
        temporary = store.append("career", "后端工作")
        for retriever in (keyword, vector):
            assert retriever.search("工作")[0].id == temporary.id
        raise RuntimeError("rollback")
    committed = store.append("career", "新的工作安排")
    for retriever in (keyword, vector):
        assert retriever.search("工作")[0].id == committed.id
    assert temporary.id not in {r["memory_id"] for r in vector_rows(store.paths)}


def test_concurrent_queries_share_one_complete_vector_index(tmp_path):
    store = seeded_store(tmp_path)
    embedder = FakeEmbedder(GROUPS)
    retriever = persisted_vector(store, embedder)
    start = Barrier(4)

    def search(query):
        start.wait(timeout=10)
        return retriever.search(query)[0].topic

    with ThreadPoolExecutor(max_workers=4) as pool:
        topics = list(pool.map(search, ["住房", "咖啡"] * 2))
    assert topics == ["general/housing", "general/preferences"] * 2
    assert len(embedder.passages) == 2


@pytest.mark.parametrize("mode", ["keyword", "vector", "hybrid"])
def test_concurrent_correction_does_not_drop_a_recalled_fact(service, mode, monkeypatch):
    from syvrio.memory.models import MemoryDraft

    old = service.note("常喝拿铁咖啡", topic="preferences")
    keyword = KeywordRetriever(service.store)
    vector = persisted_vector(service.store, FakeEmbedder(GROUPS))
    service.retriever = {
        "keyword": keyword,
        "vector": vector,
        "hybrid": HybridRetriever(keyword, vector),
    }[mode]
    searching, resume = Event(), Event()
    first_branch = vector if mode == "vector" else keyword
    search = first_branch.search

    def pause_search(*args, **kwargs):
        if not searching.is_set():
            searching.set()
            assert resume.wait(10)
        return search(*args, **kwargs)

    monkeypatch.setattr(first_branch, "search", pause_search)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(service.recall, "咖啡", original_query="常喝哪种咖啡")
        try:
            assert searching.wait(10)
            new = pool.submit(
                service.correct, old.id, MemoryDraft(text="常喝美式咖啡", topic="preferences")
            ).result(timeout=10)
            assert [hit.id for hit in service.recall("咖啡")] == [new.id]
        finally:
            resume.set()
        assert [hit.id for hit in read.result(timeout=10)] == [old.id]

    if mode != "keyword":
        assert [r["memory_id"] for r in vector_rows(service.paths)] == [new.id]
    assert [hit.id for hit in service.recall("咖啡")] == [new.id]


def test_hybrid_recall_reads_each_topic_once_for_both_queries(service, monkeypatch):

    coffee = service.note("常喝拿铁咖啡", topic="preferences")
    service.note("使用键盘工作", topic="devices")
    service.retriever = HybridRetriever(
        KeywordRetriever(service.store), persisted_vector(service.store, FakeEmbedder(GROUPS))
    )
    original = service.store.snapshot
    seen = []

    def counted():
        data = original()
        seen.append(data.revision)
        return data

    monkeypatch.setattr(service.store, "snapshot", counted)
    for _ in range(2):
        seen.clear()
        assert service.recall(coffee.text, original_query=coffee.text)[0].id == coffee.id
        assert len(seen) == 1


def test_cache_persists_across_instances(tmp_path):
    store = seeded_store(tmp_path)
    persisted_vector(store, FakeEmbedder(GROUPS)).search("房子", 3)

    embedder = FakeEmbedder(GROUPS)
    retriever = persisted_vector(store, embedder)
    assert retriever.search("房子", 3)
    assert embedder.passages == []


def test_repeated_embedding_input_keeps_distinct_facts_and_reuses_vectors(tmp_path):
    store = MemoryStore(tmp_path)
    first = store.append("preferences", "咖啡")
    second = store.append("drinks", "咖啡")
    embedder = FakeEmbedder(GROUPS)
    vector = persisted_vector(store, embedder)
    assert {hit.id for hit in vector.search("咖啡")} == {first.id, second.id}
    assert embedder.passages == ["咖啡"]

    third = store.append("notes", "咖啡")
    fresh = FakeEmbedder(GROUPS)
    assert {hit.id for hit in persisted_vector(store, fresh).search("咖啡")} == {
        first.id,
        second.id,
        third.id,
    }
    assert fresh.passages == []
    cached = vector_rows(store.paths)
    assert {row["memory_id"] for row in cached} == {first.id, second.id, third.id}
    assert len({row["input_digest"] for row in cached}) == 1


def test_vector_cache_removal_rebuilds_from_authoritative_memories(tmp_path):
    store = seeded_store(tmp_path)
    before = persisted_vector(store, FakeEmbedder(GROUPS)).search("咖啡")
    path = store.paths.cache_dir / "vectors.sqlite3"
    assert path.is_file()
    with transaction(store.paths, write=False) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name='vectors'").fetchone() is None
    path.unlink()
    embedder = FakeEmbedder(GROUPS)
    assert persisted_vector(store, embedder).search("咖啡") == before
    assert len(embedder.passages) == len(store.entries()) == len(vector_rows(store.paths))


@pytest.fixture
def cache_diagnostics(monkeypatch):
    from syvrio.infra.observability import diagnostics

    events = []
    monkeypatch.setattr(diagnostics, "_RECORDED_ONCE", set())

    def record(event, error):
        events.append(event)
        return True

    monkeypatch.setattr(diagnostics, "record_event", record)
    return events


def test_hybrid_recall_with_damaged_vector_file_uses_memory_then_keywords(
    service, cache_diagnostics
):
    first = service.note("偏爱清淡的茶", topic="preferences")
    path = service.paths.cache_dir / "vectors.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    unknown_bytes = b"unreadable cache; preserve this file"
    path.write_bytes(unknown_bytes)
    embedder = FakeEmbedder(GROUPS)
    service.retriever = HybridRetriever(
        KeywordRetriever(service.store), persisted_vector(service.store, embedder)
    )
    assert service.recall("咖啡")[0].id == first.id

    second = service.note("住房预算有限", topic="housing")
    assert service.recall("房子")[0].id == second.id
    assert len(embedder.passages) == 2
    assert service.recall("咖啡")[0].id == first.id

    service.retriever = HybridRetriever(
        KeywordRetriever(service.store),
        persisted_vector(service.store, FakeEmbedder(GROUPS, fail=True)),
    )
    assert service.recall("住房预算")[0].id == second.id
    assert path.read_bytes() == unknown_bytes
    assert cache_diagnostics.count("degraded.vector_cache") == 1


def test_failed_cache_write_after_business_commit_keeps_result(service, cache_diagnostics):
    from syvrio.infra.session import SessionStore

    fact = service.note("偏爱清淡的茶", topic="preferences")
    service.store.snapshot()
    embedder = FakeEmbedder(GROUPS)
    service.retriever = HybridRetriever(
        KeywordRetriever(service.store), persisted_vector(service.store, embedder)
    )
    with cache_transaction(service.paths, "vectors") as cache:
        cache.execute("""CREATE TRIGGER fail_cache BEFORE INSERT ON vectors
                       BEGIN SELECT RAISE(ABORT,'cache write unavailable'); END""")
    sessions = SessionStore(service.paths)
    with transaction(service.paths):
        session = sessions.create()
        assert service.recall("咖啡")[0].id == fact.id
        assert "degraded.vector_cache" not in cache_diagnostics
    assert sessions.load(session.id).id == session.id
    assert vector_rows(service.paths) == []
    assert service.recall("咖啡")[0].id == fact.id
    assert len(embedder.passages) == 1
    assert cache_diagnostics.count("degraded.vector_cache") == 1


def test_cache_can_publish_while_another_connection_holds_business_write_lock(tmp_path):
    store = seeded_store(tmp_path)
    vector = persisted_vector(store, FakeEmbedder(GROUPS))
    with ThreadPoolExecutor(max_workers=1) as pool, transaction(store.paths):
        assert pool.submit(vector.search, "咖啡").result(timeout=5)
        assert len(vector_rows(store.paths)) == 2


def _change_vector_in_another_process(paths, memory_id):
    store = MemoryStore(paths)
    with transaction(paths) as db:
        db.execute("UPDATE memories SET text='住房' WHERE id=?", (memory_id,))
    return persisted_vector(store, FakeEmbedder(GROUPS)).search("房子")[0].id


def test_late_vector_publish_cannot_replace_another_writers_result(tmp_path):
    from syvrio.infra.retrieval.vector import text_hash

    store = MemoryStore(tmp_path)
    entry = store.append("preferences", "咖啡")
    ready, resume = Event(), Event()

    class SlowEmbedder(FakeEmbedder):
        def embed(self, texts):
            result = super().embed(texts)
            ready.set()
            assert resume.wait(45)
            return result

    slow = persisted_vector(store, SlowEmbedder(GROUPS))
    with (
        ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as writers,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        old = pool.submit(slow.search, "咖啡")
        try:
            assert ready.wait(10)
            update = writers.submit(_change_vector_in_another_process, store.paths, entry.id)
            assert update.result(timeout=40) == entry.id
        finally:
            resume.set()
        assert old.result(timeout=10)[0].text == "咖啡"

    assert [r["input_digest"] for r in vector_rows(store.paths)] == [text_hash("住房")]
    warm = FakeEmbedder(GROUPS)
    assert persisted_vector(store, warm).search("房子")[0].text == "住房"
    assert not warm.passages


def test_model_change_rebuilds_cache(tmp_path):
    store = seeded_store(tmp_path)
    persisted_vector(store, FakeEmbedder(GROUPS, model_id="fake-a")).search("房子", 3)

    embedder = FakeEmbedder(GROUPS, model_id="fake-b")
    retriever = persisted_vector(store, embedder)
    assert retriever.search("房子", 3)
    assert len(embedder.passages) == 2


def test_vector_search_degrades_to_empty_when_embedder_fails(tmp_path):
    store = seeded_store(tmp_path)
    retriever = persisted_vector(store, FakeEmbedder(GROUPS, fail=True))

    assert retriever.search("房子", 3) == []


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_sync_retries_after_embedding_failure(tmp_path):
    from syvrio.infra.retrieval.vector import RETRY_COOLDOWN_SECONDS

    clock = FakeClock()
    store = seeded_store(tmp_path)
    embedder = FakeEmbedder(GROUPS, fail=True)
    retriever = persisted_vector(store, embedder, clock=clock)
    assert retriever.search("房子", 3) == []
    assert retriever.degraded is True
    passage_calls = len(embedder.passages)

    # 冷却期内不重复打后端，避免每次检索都卡在失败调用上。
    embedder.fail = False
    assert retriever.search("房子", 3) == []
    assert len(embedder.passages) == passage_calls

    # 冷却到期后即使记忆文件没变也要重新同步，而不是一直返回空。
    clock.now += RETRY_COOLDOWN_SECONDS
    hits = retriever.search("房子", 3)
    assert hits and hits[0].topic == "general/housing"
    assert retriever.degraded is False


def test_query_failure_enters_cooldown(tmp_path):
    from syvrio.infra.retrieval.vector import RETRY_COOLDOWN_SECONDS

    clock = FakeClock()
    store = seeded_store(tmp_path)
    embedder = FakeEmbedder(GROUPS)
    retriever = persisted_vector(store, embedder, clock=clock)
    assert retriever.search("房子", 3)

    embedder.fail = True
    assert retriever.search("房子", 3) == []
    assert retriever.degraded is True
    query_calls = len(embedder.queries)

    embedder.fail = False
    assert retriever.search("房子", 3) == []
    assert len(embedder.queries) == query_calls

    clock.now += RETRY_COOLDOWN_SECONDS
    assert retriever.search("房子", 3)
    assert retriever.degraded is False


def test_search_ignores_deleted_entries(tmp_path):
    store = seeded_store(tmp_path)
    entry = store.append("career", "在找后端工作")
    retriever = persisted_vector(store, FakeEmbedder(GROUPS))
    assert retriever.search("后端", 3)

    store.set_status(entry.id, "deprecated")
    assert all(hit.id != entry.id for hit in retriever.search("后端", 3))


class StubRetriever:
    def __init__(self, ids: list[str]):
        self.ids = ids

    def search(self, query: str, limit: int = 10, *, snapshot=None) -> list[SearchHit]:
        return [SearchHit(id=i, text=f"text-{i}", topic="t") for i in self.ids[:limit]]


def test_hybrid_ranks_hits_in_both_paths_first():
    # 夹具需通过字面覆盖闸（查询 bigram 在条目中覆盖率 >= 0.3）
    keyword = StubRetriever(["红轴机械键盘", "键盘轴体", "红轴与键盘手感"])
    vector = StubRetriever(["键盘轴体", "桌面音箱"])
    hits = HybridRetriever(keyword, vector).search("红轴键盘", limit=3)

    assert hits[0].id == "键盘轴体"
    assert len(hits) == 3


def test_hybrid_breaks_rrf_ties_toward_vector_rank():
    # BM25 噪声把 a 排第一、c 排第三；向量把 c 排第一、a 排第三 → RRF 完全平局，
    # 决胜应信任向量名次（c 第一）。夹具全部含查询词"键盘"，通过字面覆盖闸。
    keyword = StubRetriever(["键盘手感", "键盘轴体", "机械键盘套装"])
    vector = StubRetriever(["机械键盘套装", "键盘轴体", "键盘手感"])
    hits = HybridRetriever(keyword, vector).search("键盘", limit=3)

    assert [hit.id for hit in hits[:2]] == ["机械键盘套装", "键盘手感"]


def test_hybrid_falls_back_to_keyword_when_vector_fails(tmp_path):
    store = seeded_store(tmp_path)
    hybrid = HybridRetriever(
        KeywordRetriever(store),
        persisted_vector(store, FakeEmbedder(GROUPS, fail=True)),
    )

    hits = hybrid.search("咖啡", limit=3)

    assert hits and hits[0].topic == "general/preferences"


def test_literal_coverage_does_not_join_words_across_separators():
    from syvrio.infra.retrieval.matching import bigram_coverage

    assert bigram_coverage("姓名，年龄", "姓名") == 0.5
    assert bigram_coverage("姓名 年龄", "姓名 年龄") == 1.0
    assert bigram_coverage("名年", "姓名 年龄") == 0.0


def test_hybrid_preserves_keyword_hits_without_literal_signal():
    class Keyword:
        def search(self, query, limit, *, snapshot=None):
            return [SearchHit(id="keyword", text="docker 部署命令", topic="work")]

    class Vector:
        def __init__(self, hits):
            self.hits = hits

        def search(self, query, limit, *, snapshot=None):
            return self.hits

    other = SearchHit(id="vector", text="其他命中", topic="work")
    for query in ("搜 docker", "docker 是什么"):
        assert {hit.id for hit in HybridRetriever(Keyword(), Vector([other])).search(query)} == {
            "keyword",
            "vector",
        }
    assert [hit.id for hit in HybridRetriever(Keyword(), Vector([])).search("git 是什么")] == [
        "keyword"
    ]


def test_field_mentions_do_not_force_lookup_priority(tmp_path):
    store = MemoryStore(tmp_path)
    name = store.append("identity", "姓名：虚构人物甲")
    store.append("work", "保管地点：合成仓库")
    keyword = KeywordRetriever(store)
    for query in ("帮我修改我的姓名", "写个包含姓名和电话的签名", "把保管地点挪一下"):
        assert not any(hit.matched_field for hit in keyword.search(query))
    assert keyword.search("我的姓名是什么")[0].id == name.id
    assert keyword.search("姓名")[0].matched_field == "姓名"


@pytest.mark.parametrize("add_entry", [False, True])
def test_same_model_dimension_change_rebuilds_stale_cache(tmp_path, add_entry):
    class ResizedEmbedder:
        model_id = "synthetic"
        dimension = 2

        def embed(self, texts):
            return [[1.0] * self.dimension for _ in texts]

        def embed_query(self, text):
            return [1.0] * self.dimension

    store = MemoryStore(tmp_path)
    original = store.append("work", "Synthetic first note.")
    embedder = ResizedEmbedder()
    retriever = persisted_vector(store, embedder)
    assert [hit.id for hit in retriever.search("synthetic")] == [original.id]
    embedder.dimension = 3
    expected = {original.id}
    if add_entry:
        expected.add(store.append("work", "Synthetic second note.").id)
    assert {hit.id for hit in retriever.search("synthetic")} == expected
    assert not retriever.degraded
    assert {r["memory_id"]: r["dimensions"] for r in vector_rows(store.paths)} == dict.fromkeys(
        expected, 3
    )


def test_short_field_coverage_keeps_exact_hit_without_restoring_noise(tmp_path):
    store = MemoryStore(tmp_path)
    name = store.append("identity", "姓名：虚构人物甲", title="合成人物资料")
    noise = store.append("archive", "登记规则会核验称呼，并检查活动报名材料")
    competing = store.append("archive", "登山装备清单")
    query = "姓名 名字 称呼 身份"

    class SemanticHits:
        def search(self, query, limit, *, snapshot=None):
            return [SearchHit(id=competing.id, text=competing.text, topic=competing.topic)]

    keyword = KeywordRetriever(store)
    assert noise.id in {hit.id for hit in keyword.search(query)}
    hits = HybridRetriever(keyword, SemanticHits()).search(query)
    assert [hit.id for hit in hits] == [name.id, competing.id]
    assert hits[0].text == name.text
    assert hits[0].topic == name.topic


def test_field_index_respects_token_boundaries_and_longest_labels(tmp_path):
    store = MemoryStore(tmp_path)
    short = store.append("devices", "卷标：HARBOR")
    long = store.append("devices", "备份卷标：TIDE-12")
    latin = store.append("devices", "PIN: TEST-PIN")
    keyword = KeywordRetriever(store)

    assert [hit.id for hit in keyword.search("我的备份卷标是什么") if hit.matched_field] == [
        long.id
    ]
    assert {hit.id for hit in keyword.search("卷标和备份卷标") if hit.matched_field} == {
        short.id,
        long.id,
    }
    assert not any(hit.matched_field for hit in keyword.search("SPINNING"))
    assert keyword.search("Find my pin")[0].id == latin.id

    store.set_status(long.id, "deprecated")
    assert not any(hit.id == long.id for hit in keyword.search("备份卷标"))


def test_same_field_uses_context_to_choose_between_records(tmp_path):
    store = MemoryStore(tmp_path)
    first = store.append("devices", "运行模式：预览节点使用隔离模式")
    second = store.append("devices", "运行模式：批处理节点使用常规模式")
    keyword = KeywordRetriever(store)
    embedder = FakeEmbedder(
        {
            "preview": ["预览", "隔离"],
            "batch": ["批处理", "常规"],
        }
    )
    hybrid = HybridRetriever(keyword, persisted_vector(store, embedder))

    for query, expected in (("预览节点的运行模式", first), ("批处理节点的运行模式", second)):
        hit = hybrid.search(query, 1)[0]
        assert hit.id == expected.id and hit.matched_field == "运行模式"
        assert "matched_field" not in hit.model_dump()


def test_field_index_accepts_a_terminal_period_but_not_multiple_sentences(tmp_path):
    store = MemoryStore(tmp_path)
    fact = store.append("work", "复核规则：使用校准过的量具。")
    note = store.append("work", "复核规则：使用过期样例。这个旧流程已废弃。")
    hits = KeywordRetriever(store).search("我的复核规则")
    assert [hit.id for hit in hits if hit.matched_field] == [fact.id]
    assert note.id in {hit.id for hit in hits}
    assert fact.embedding_text == fact.index_text


def test_embedding_cache_rebuilds_changed_short_fact_only(tmp_path):
    from syvrio.infra.retrieval.vector import text_hash

    store = MemoryStore(tmp_path)
    short = store.append("identity", "姓名：虚构人物甲", title="合成人物资料")
    long = store.append("preferences", "我喜欢清淡的咖啡，有时也会喝茶")
    embedder = FakeEmbedder({"name": ["姓名"], **GROUPS})
    vector = persisted_vector(store, embedder)
    vector.sync()
    embedder.passages.clear()
    with cache_transaction(store.paths, "vectors") as db:
        db.execute(
            "UPDATE vectors SET input_digest=? WHERE memory_id=?",
            (text_hash("previous input"), short.id),
        )
    vector = persisted_vector(store, embedder)
    assert short.embedding_text == short.text
    assert long.embedding_text == long.index_text
    assert vector.search("姓名")[0].id == short.id
    assert embedder.passages == [short.embedding_text]
    with transaction(store.paths) as db:
        db.execute("UPDATE topics SET title='updated synthetic label' WHERE name=?", (short.topic,))
    embedder.passages.clear()
    vector.sync()
    updated = store.find(short.id)
    assert embedder.passages == []
    assert updated.text == short.text


def test_large_corpus_keeps_explicit_short_fields_without_local_model(tmp_path):
    from evals.short_facts_eval import SHORT_CASES, seed_vault

    assert seed_vault(tmp_path) >= 1200
    store = MemoryStore(tmp_path)
    keyword = KeywordRetriever(store)

    class DistractingVector:
        def search(self, query, limit, *, snapshot=None):
            return [
                SearchHit(id=f"distractor-{n:04}", text="同领域干扰", topic="archive")
                for n in range(min(limit, 80))
            ]

    hybrid = HybridRetriever(keyword, DistractingVector())
    for field, query in SHORT_CASES:
        if query in {"我是谁", "我叫什么名字"}:
            continue  # 这些查询需要改写或语义嵌入。
        assert f"short-{field}" in {hit.id for hit in hybrid.search(query, 3)}, query


def test_build_retriever_falls_back_without_fastembed(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module.LocalEmbedder, "available", staticmethod(lambda: False))
    config = Config(vault=tmp_path, retrieval=RetrievalConfig(method="hybrid"))

    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))

    assert isinstance(retriever, KeywordRetriever)
    assert mode == "hybrid-fallback-keyword"


def test_build_retriever_hybrid_when_local_available(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module.LocalEmbedder, "available", staticmethod(lambda: True))
    config = Config(vault=tmp_path, retrieval=RetrievalConfig(method="hybrid"))

    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))

    assert isinstance(retriever, HybridRetriever)
    assert mode == "hybrid"
    assert isinstance(retriever.vector.embedder.model_id, str)


def test_build_retriever_api_backend(tmp_path, monkeypatch, isolate_llm_credentials):
    config = Config(
        vault=tmp_path,
        retrieval=RetrievalConfig(
            method="hybrid",
            embedding=EmbeddingConfig(
                backend="api", model="BAAI/bge-m3", api_key_env="TEST_EMBED_KEY"
            ),
        ),
    )
    monkeypatch.setenv("TEST_EMBED_KEY", "ignored-environment-key")
    isolate_llm_credentials["TEST_EMBED_KEY"] = "test-key"
    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))
    assert mode == "hybrid"

    del isolate_llm_credentials["TEST_EMBED_KEY"]
    retriever, mode = service_module.build_retriever(config, MemoryStore(tmp_path))
    assert isinstance(retriever, KeywordRetriever)
    assert mode == "hybrid-fallback-keyword"


def test_api_endpoint_change_rebuilds_persisted_vectors_for_same_model(
    tmp_path, monkeypatch, isolate_llm_credentials
):
    endpoint_a = "https://embedding-a.example.invalid/v1"
    endpoint_b = "https://embedding-b.example.invalid/v1"
    isolate_llm_credentials["TEST_EMBED_KEY"] = "synthetic"
    served = []

    def fake_embed(config, texts, *, batch_size):
        served.append((config.base_url, tuple(texts)))
        return [
            ([1.0, 0.0] if ("pear" in text) == (config.base_url == endpoint_a) else [0.0, 1.0])
            for text in texts
        ]

    monkeypatch.setattr("syvrio.infra.llm.client.embed_texts", fake_embed)
    store = MemoryStore(tmp_path)
    store.append("fixtures", "pear")
    store.append("fixtures", "wrench")

    def build(endpoint):
        config = Config(
            vault=tmp_path,
            retrieval=RetrievalConfig(
                method="vector",
                embedding=EmbeddingConfig(
                    backend="api",
                    provider="custom",
                    model="same-model",
                    base_url=endpoint,
                    api_key_env="TEST_EMBED_KEY",
                ),
            ),
        )
        return service_module.build_retriever(config, store)[0]

    for endpoint in (endpoint_a, endpoint_b):
        assert build(endpoint).search("pear")[0].text == "pear"
        assert any(url == endpoint and len(texts) == 2 for url, texts in served)
    served.clear()
    assert build(endpoint_b).search("pear")[0].text == "pear"
    assert served == [(endpoint_b, ("pear",))]


def test_vector_cache_is_outside_vault_git_worktree(tmp_path):
    from syvrio.memory.service import MemoryService

    config = Config(vault=tmp_path / "vault", retrieval=RetrievalConfig(method="keyword"))
    service = MemoryService(config)
    git = GitSyncService(config, service.paths)
    initialize_vault(git, service.initialize_files)
    service.note("喜欢清淡、像茶的风味")
    git.commit(push=False)
    retriever = persisted_vector(service.store, FakeEmbedder(GROUPS))
    retriever.sync()
    assert vector_rows(service.paths) and not git.is_dirty()
    assert not git.repo.has_changes()
    assert not (config.vault / ".syvrio").exists()


def test_init_vault_preserves_user_gitignore_without_final_newline(tmp_path):
    from syvrio.memory.service import MemoryService

    config = Config(vault=tmp_path / "vault", retrieval=RetrievalConfig(method="keyword"))
    service = MemoryService(config)
    service.store.ensure()
    gitignore = config.vault / "export" / ".gitignore"
    gitignore.parent.mkdir()
    gitignore.write_bytes(b"*.log")

    git = GitSyncService(config, service.paths)
    initialize_vault(git, service.initialize_files)

    assert gitignore.read_bytes() == b"*.log\n/attachments/\n/archived/\n"
    initialize_vault(git, service.initialize_files)
    assert gitignore.read_bytes() == b"*.log\n/attachments/\n/archived/\n"


def test_duplicate_id_cannot_enter_second_topic(tmp_path):
    store = MemoryStore(tmp_path)
    first = store.append("a", "one", entry_id="mem-dup")
    with pytest.raises(ValueError, match="Duplicate"):
        store.append("b", "two", entry_id=first.id)
    assert store.entries() == [first]


@pytest.mark.parametrize(
    "assignment",
    ["vector=X'00'", "dimensions='unknown'", "vector='not a vector'"],
)
def test_malformed_vector_is_regenerated(tmp_path, assignment, cache_diagnostics):
    store = seeded_store(tmp_path)
    first = persisted_vector(store, FakeEmbedder(GROUPS))
    first.sync()
    with cache_transaction(store.paths, "vectors") as db:
        db.execute(f"UPDATE vectors SET {assignment}")
    refreshed = persisted_vector(store, FakeEmbedder(GROUPS))
    assert refreshed.search("房子")[0].topic == "general/housing"
    assert cache_diagnostics.count("degraded.vector_cache") == 1
    assert len(vector_rows(store.paths)) == len(store.entries())
    assert all(len(r["vector"]) == 4 * r["dimensions"] for r in vector_rows(store.paths))


def test_retrievers_read_an_independent_source_and_write_only_their_cache(tmp_path):
    from dataclasses import dataclass
    from types import SimpleNamespace

    @dataclass(frozen=True)
    class Record:
        id: str = "synthetic"
        topic: str = "notes"
        text: str = "口味：清淡咖啡"
        index_text: str = "口味：清淡咖啡 茶"
        embedding_text: str = "清淡咖啡"
        timestamp: str = "2026-01-01"

    class Source:
        rows = (Record(),)

        revision = 1

        def snapshot(self):
            return SimpleNamespace(records=self.rows, revision=self.revision)

    source = Source()
    before = set(tmp_path.iterdir())
    keyword = KeywordRetriever(source)
    vector = VectorRetriever(source, FakeEmbedder(GROUPS))
    assert keyword.unique_field("口味") is source.rows[0]
    hits = [keyword.search("口味")[0], vector.search("茶")[0]]
    for hit in hits:
        assert hit.id == source.rows[0].id
        assert hit.text == source.rows[0].text
    assert hits[0].matched_field == "口味"
    assert set(tmp_path.iterdir()) == before
    paths = MemoryStore(tmp_path).paths
    cached = VectorRetriever(source, FakeEmbedder(GROUPS), paths=paths)
    assert cached.search("茶")[0].id == source.rows[0].id
    assert [row["memory_id"] for row in vector_rows(paths)] == [source.rows[0].id]
    reused = FakeEmbedder(GROUPS)
    assert VectorRetriever(source, reused, paths=paths).search("茶")[0].id == source.rows[0].id
    assert reused.passages == []
    assert not database_path(paths).exists()
    source.rows = ()
    source.revision += 1
    cached.sync()
    assert not vector_rows(paths)
    assert keyword.search("口味") == vector.search("茶") == []
