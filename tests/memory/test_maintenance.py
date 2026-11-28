import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from syvrio.infra.session import SessionStore
from syvrio.infra.storage.database import transaction, transaction_mode
from syvrio.memory.background import BackgroundMemory
from syvrio.memory.background import maintenance as scanner
from syvrio.memory.background.jobs import list_jobs
from syvrio.memory.maintenance_review import ReviewConflict, ReviewDecision
from syvrio.memory.models import Entry, MemoryContext, MemoryCorrection, MemoryDraft
from syvrio.memory.turn_undo import TurnJournal


def drain(task):
    for _ in range(100):
        if task.status()["state"] != "queued":
            break
        task.tick(Event())
    assert task.status()["state"] == "completed", task.status()


def scan(task):
    task.request()
    drain(task)


def fake_model(monkeypatch, classify):
    calls = []

    def ask(config, prompt, payload, schema):
        assert transaction_mode() is None
        assert schema is scanner.Assessment
        calls.append(payload)
        result = []
        for candidate in payload["candidates"]:
            kind = classify(payload["target"], candidate)
            if kind:
                result.append(
                    {"candidate_id": candidate["id"], "kind": kind, "reason": "Synthetic evidence"}
                )
        return schema(suggestions=result)

    monkeypatch.setattr(scanner.model, "ask", ask)
    return calls


def decision(action, selected_id=None, **kwargs):
    return ReviewDecision(confirmed=True, action=action, selected_id=selected_id, **kwargs)


@pytest.mark.parametrize(
    ("a", "b", "kind"),
    [
        ("Alice always orders tea at work", "At work Alice always orders tea", "duplicate"),
        ("Alice always orders tea at work", "Alice never orders tea at work", "conflict"),
        ("Alice orders tea at work", "Alice buys tea for visitors at work", None),
        ("Alice orders tea at work", "Ben never orders tea at work", None),
        ("Alice orders tea at work", "Alice never orders tea at home", None),
    ],
)
def test_detection_only_suggests_and_keeps_other_workflows_intact(service, monkeypatch, a, b, kind):
    first = service.note(a)
    second = service.note(b)
    ordinary = service.create_pending(
        MemoryDraft(topic="notes", text="Unconfirmed tea claim"), "Is this correct?", []
    )
    task = BackgroundMemory(service).maintenance
    calls = fake_model(monkeypatch, lambda *_: kind)
    scan(task)
    assert calls and all(len(p["candidates"]) <= 4 for p in calls)
    assert {p["target"]["id"] for p in calls} <= {first.id, second.id}
    assert service.store.find(first.id) == first and service.store.find(second.id) == second
    assert [p.id for p in service.pending()] == [ordinary.id]
    assert service.list_inferences() == []
    assert {hit.id for hit in service.recall("tea", 20)} <= {first.id, second.id}
    assert not service.recall("Synthetic evidence")
    assert list_jobs(service.paths, "dream") == []
    assert [x["kind"] for x in task.reviews.pending()["items"]] == ([kind] if kind else [])
    n = len(calls)
    scan(task)
    assert len(calls) == n


def test_disjoint_effective_dates_excluded_before_model(service, monkeypatch):
    for year in ("2024", "2025"):
        service.add(
            MemoryDraft(
                topic="schedule",
                text="Alice works Tuesdays",
                context=MemoryContext(valid_from=year, valid_to=year),
            )
        )
    calls = fake_model(monkeypatch, lambda *_: "conflict")
    task = scanner.Maintenance(service)
    scan(task)
    assert not calls and not task.reviews.pending()["items"]


@pytest.mark.parametrize(
    "state_context",
    [
        MemoryContext(event_time="2025-01-01", valid_from="2025-01"),
        MemoryContext(event_time="2025-01-01"),
    ],
)
def test_recorded_event_does_not_expire_ongoing_claim(service, monkeypatch, state_context):
    for body, context in (
        ("Station kettle is offline indefinitely", state_context),
        ("Station kettle is online", MemoryContext(event_time="2025-02-01")),
    ):
        service.add(MemoryDraft(topic="station", text=body, context=context))
    calls = fake_model(monkeypatch, lambda *_: "conflict")
    task = scanner.Maintenance(service)
    scan(task)
    assert calls and len(task.reviews.pending()["items"]) == 1


def test_different_event_dates_reach_model_with_original_precision(service, monkeypatch):
    for date in ("2024", "2025-09"):
        service.add(
            MemoryDraft(
                topic="station",
                text="Station kettle inspection",
                context=MemoryContext(event_time=date),
            )
        )
    calls = fake_model(monkeypatch, lambda *_: None)
    task = scanner.Maintenance(service)
    scan(task)
    assert calls
    pair = [calls[0]["target"], *calls[0]["candidates"]]
    assert {fact["context"]["event_time"] for fact in pair} == {"2024", "2025-09"}
    assert not task.reviews.pending()["items"]


def test_common_terms_find_distant_match_with_bounded_shortlist(monkeypatch):
    words = ["station", "kettle", "offline"]
    texts = [" ".join(words), *(words[i % 3] for i in range(600)), "offline station kettle"]
    rows = [
        Entry(id=f"id{i:04}", topic="station", timestamp="", text=t) for i, t in enumerate(texts)
    ]
    index = scanner.CandidateIndex(SimpleNamespace(active_entries_at=lambda _: rows), 1)
    search = index.retriever.search
    sizes = []

    def measure(query, *, limit, snapshot):
        sizes.append(len(snapshot.records))
        assert limit == 4
        return search(query, limit=limit, snapshot=snapshot)

    monkeypatch.setattr(index.retriever, "search", measure)
    for target, match in ((rows[0], rows[-1]), (rows[-1], rows[0])):
        hits = index.candidates(target)
        assert match.id in hits
        assert len(hits) <= 4
    assert len(sizes) == 2 and max(sizes) <= 128


def test_ignore_is_version_specific_and_stale_confirmation_is_refused(service, monkeypatch):
    a = service.note("Station serves tea")
    b = service.note("Station serves only tea")
    task = scanner.Maintenance(service)
    fake_model(monkeypatch, lambda *_: "duplicate")
    scan(task)
    sid = task.reviews.pending()["items"][0]["id"]
    task.reviews.resolve(sid, decision("ignore"))
    with pytest.raises(ReviewConflict):
        task.reviews.resolve(sid, decision("deprecate", a.id))
    assert service.store.find(b.id) == b
    # 已忽略的两版本也不会在重新读同一切点时出现。
    with transaction(service.paths):
        scanner.save_job(task.key, None)
    scan(task)
    assert not task.reviews.pending()["items"]
    service.correct(b.id, MemoryCorrection(text="Station always serves tea"))
    scan(task)
    new = task.reviews.pending()["items"][0]
    assert new["id"] != sid
    service.correct(a.id, MemoryCorrection(text="Station sometimes serves tea"))
    with pytest.raises(ReviewConflict, match="changed"):
        task.reviews.resolve(new["id"], decision("deprecate", b.id))
    assert not task.reviews.pending()["items"]
    scan(task)
    assert len(task.reviews.pending()["items"]) == 1


@pytest.mark.parametrize("kind", ["duplicate", "conflict"])
def test_keep_choice_is_explicit_and_atomic_under_double_click(service, monkeypatch, kind):
    a = service.note("Alice orders coffee")
    b = service.note("Alice always orders coffee")
    task = scanner.Maintenance(service)
    fake_model(monkeypatch, lambda *_: kind)
    scan(task)
    row = task.reviews.pending()["items"][0]["id"]
    with pytest.raises(ValueError):
        task.reviews.resolve(row, decision("deprecate"))

    def click():
        try:
            return task.reviews.resolve(row, decision("deprecate", a.id))["state"]
        except ReviewConflict:
            return "already reviewed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: click(), range(2))) == ["already reviewed", "applied"]
    assert service.store.find(a.id) == a
    assert service.store.find(b.id).status == "deprecated"
    with transaction(service.paths, write=False) as db:
        assert (
            db.execute("SELECT count(*) FROM audit WHERE action='maintenance_applied'").fetchone()[
                0
            ]
            == 1
        )


def test_transition_preserves_both_sources_and_requires_explicit_dates(service, monkeypatch):
    sessions = SessionStore(service.paths)
    session = sessions.create()
    facts = []
    for text in ("Alice travels to work by bus", "Alice travels to work by bike"):
        message = sessions.append(session, dict(type="message", role="user", content=text))
        facts.append(
            service.add(
                MemoryDraft(text=text, topic="travel"),
                session_id=session.id,
                message_id=message["id"],
            )
        )
    a, b = facts
    sessions.set_archived(session.id, True)
    task = scanner.Maintenance(service)
    fake_model(monkeypatch, lambda *_: "conflict")
    scan(task)
    row = task.reviews.pending()["items"][0]
    assert {f["source"] for f in row["facts"]} == {a.text, b.text}
    assert all(f["source_archived"] and f["source_at"] for f in row["facts"])
    for extra in ({}, {"previous_end": "2026-10", "current_start": "2026-09"}):
        with pytest.raises(ValueError):
            task.reviews.resolve(row["id"], decision("transition", b.id, **extra))
    assert service.store.find(a.id) == a
    task.reviews.resolve(
        row["id"], decision("transition", b.id, previous_end="2026-09", current_start="2026-10")
    )
    old, current = service.store.find(a.id), service.store.find(b.id)
    assert len(service.store.entries()) == 2 and old.context.valid_to == "2026-09"
    assert current.context.valid_from == "2026-10" and current.supersedes == old.id
    assert (old.metadata, current.metadata) == (a.metadata, b.metadata)
    scan(task)
    assert not task.reviews.pending()["items"]


def test_undo_after_completed_cursor_rechecks_restored_revision(service, monkeypatch):
    a = service.note("Alice drinks station tea")
    b = service.note("Station tea is Alice's drink")
    sessions = SessionStore(service.paths)
    session = sessions.create()
    message = sessions.append(session, dict(type="message", role="user", content="synthetic"))
    journal = TurnJournal(service.paths, session.id, message["id"])
    with journal.capture():
        service.deprecate(b.id)
    fake_model(monkeypatch, lambda *_: "duplicate")
    task = scanner.Maintenance(service)
    scan(task)
    previous_cursor = task.status()["cursor"]
    assert not task.reviews.pending()["items"]
    journal.restore()
    scan(task)
    row = task.reviews.pending()["items"][0]
    assert {f["id"] for f in row["facts"]} == {a.id, b.id}
    assert (
        task.status()["cursor"] > previous_cursor and service.store.find(b.id).revision > b.revision
    )


def test_undo_new_fact_removes_reviewability_without_advancing_a_partial_write(
    service, monkeypatch
):
    kept = service.note("The lab supplies coffee")
    sessions = SessionStore(service.paths)
    session = sessions.create()
    message = sessions.append(
        session, dict(type="message", role="user", content="Synthetic coffee fact")
    )
    journal = TurnJournal(service.paths, session.id, message["id"])
    with journal.capture():
        removed = service.note("Coffee is supplied by the lab")
    fake_model(monkeypatch, lambda *_: "duplicate")
    task = scanner.Maintenance(service)
    scan(task)
    (row,) = task.reviews.pending()["items"]
    cursor = task.status()["cursor"]
    journal.restore()
    assert service.store.find(removed.id) is None
    with pytest.raises(ReviewConflict, match="changed"):
        task.reviews.resolve(row["id"], decision("deprecate", removed.id))
    scan(task)
    assert task.status()["cursor"] > cursor and not task.reviews.pending()["items"]
    assert service.store.find(kept.id) == kept


def test_model_race_defers_changed_pair_to_next_pinned_run(service, monkeypatch):
    a = service.note("Alice buys station tea")
    b = service.note("Station tea bought by Alice")
    task = scanner.Maintenance(service)

    def edit_while_classifying(*_):
        # A separate writer would time out if the comparison held a write transaction.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(
                service.correct, b.id, MemoryCorrection(text="Alice buys office tea")
            ).result(3)
        return "duplicate"

    fake_model(monkeypatch, edit_while_classifying)
    original = task.request()
    task.tick(Event())
    assert task.status()["cursor"] > original["cursor"]
    assert task.status()["evidence_commit"] == original["evidence_commit"]
    assert not task.reviews.pending()["items"]
    calls = fake_model(monkeypatch, lambda *_: "conflict")
    service.note("An unrelated fact written between batches")
    task.tick(Event())
    assert calls[-1]["target"]["text"] == b.text
    assert all("unrelated fact" not in item["text"] for item in calls[-1]["candidates"])
    assert task.status()["evidence_commit"] == original["evidence_commit"]
    drain(scanner.Maintenance(service))
    item = task.reviews.pending()["items"][0]
    assert {f["text"] for f in item["facts"]} == {a.text, "Alice buys office tea"}


def test_failure_rolls_back_pair_and_cursor_then_restart_resumes(service, monkeypatch):
    service.note("Alice prefers tea")
    service.note("Tea is preferred by Alice")
    fake_model(monkeypatch, lambda *_: "duplicate")
    task = scanner.Maintenance(service)
    job = task.request()
    save = scanner.save_job

    def interrupt(key, state):
        if state.get("state") == "queued" and state["cursor"] > job["cursor"]:
            raise RuntimeError("simulated checkpoint write failure")
        return save(key, state)

    monkeypatch.setattr(scanner, "save_job", interrupt)
    task.tick(Event())
    assert task.status()["state"] == "failed" and task.status()["cursor"] == job["cursor"]
    assert (
        task.status()["evidence_commit"] == job["evidence_commit"]
        and not task.reviews.pending()["items"]
    )
    monkeypatch.setattr(scanner, "save_job", save)
    restarted = scanner.Maintenance(service)
    service.note("Unrelated evidence written while the scanner was offline")
    assert restarted.request()["evidence_commit"] == job["evidence_commit"]
    restarted.tick(Event())
    assert restarted.status()["cursor"] > job["cursor"]
    drain(scanner.Maintenance(service))
    assert len(restarted.reviews.pending()["items"]) == 1


def test_first_scheduled_run_and_manual_request_share_job(service, monkeypatch):
    monkeypatch.setattr(scanner.time, "time", lambda: 100.0)
    task = scanner.Maintenance(service)
    task.schedule_first_run()
    assert not task.tick(Event())
    assert task.status()["next_at"] == 100 + scanner.INTERVAL
    monkeypatch.setattr(scanner.time, "time", lambda: 101 + scanner.INTERVAL)
    assert task.tick(Event())
    job = task.status()
    assert task.request() == job
    drain(task)
    assert not task.due(102 + scanner.INTERVAL)


def test_different_explicit_locations_and_empty_audit_do_not_call_model(service, monkeypatch):
    task = scanner.Maintenance(service)
    calls = fake_model(monkeypatch, lambda *_: "conflict")
    scan(task)
    assert task.status()["cursor"] == 0
    for place in ("Lab east", "Lab west"):
        service.add(
            MemoryDraft(
                text="The station serves tea", topic="tea", context=MemoryContext(location=place)
            )
        )
    scan(task)
    assert not calls and not task.reviews.pending()["items"]


def test_structured_background_model_has_no_write_tools(service, monkeypatch):
    service.note("Tea at the lab is free")
    service.note("The lab supplies tea at no cost")
    requests = []

    def complete(config, messages, **kwargs):
        assert transaction_mode() is None
        assert kwargs["response_format"] == {"type": "json_object"} and "tools" not in kwargs
        payload = json.loads(messages[1]["content"])
        requests.append(payload)
        answer = {
            "suggestions": [
                {
                    "candidate_id": payload["candidates"][0]["id"],
                    "kind": "duplicate",
                    "reason": "Both facts describe free lab tea.",
                }
            ]
        }
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content=json.dumps(answer))
                )
            ]
        )

    monkeypatch.setattr(scanner.model, "complete", complete)
    task = scanner.Maintenance(service)
    scan(task)
    assert len(requests) == 1
    (item,) = task.reviews.pending()["items"]
    assert item["reason"] == "Both facts describe free lab tea."
    assert all(f.status == "active" for f in service.store.entries())


def test_unknown_candidate_and_stop_during_model_leave_no_partial_publication(service, monkeypatch):
    a = service.note("Station tea is free")
    b = service.note("Free station tea")
    task = scanner.Maintenance(service)
    stop = Event()
    job = task.request()
    monkeypatch.setattr(
        scanner.model,
        "ask",
        lambda *args: scanner.Assessment(
            suggestions=[
                scanner.PairSuggestion(
                    candidate_id="outside-shortlist", kind="duplicate", reason="invalid"
                )
            ]
        ),
    )
    task.tick(stop)
    assert task.status()["state"] == "failed" and task.status()["cursor"] == job["cursor"]

    def cancel(*_):
        stop.set()
        return "duplicate"

    fake_model(monkeypatch, cancel)
    task.request()
    task.tick(stop)
    assert task.status()["cursor"] == job["cursor"] and not task.reviews.pending()["items"]
    assert service.store.find(a.id) == a and service.store.find(b.id) == b
    fake_model(monkeypatch, lambda *_: "duplicate")
    drain(scanner.Maintenance(service))
    assert len(task.reviews.pending()["items"]) == 1


def test_model_response_with_both_suggestions_and_unrelated_facts(service, monkeypatch):
    facts = [
        service.note(s)
        for s in (
            "Lab tea costs zero",
            "Lab tea is free",
            "Lab tea is never free",
            "Lab tea glasses are blue",
        )
    ]
    expected = {
        frozenset((facts[0].id, facts[1].id)): "duplicate",
        frozenset((facts[0].id, facts[2].id)): "conflict",
    }

    def classify(target, candidate):
        return expected.get(frozenset((target["id"], candidate["id"])))

    calls = fake_model(monkeypatch, classify)
    task = scanner.Maintenance(service)
    scan(task)
    rows = task.reviews.pending()["items"]
    assert any(len(p["candidates"]) > 1 for p in calls)
    assert {frozenset(f["id"] for f in row["facts"]): row["kind"] for row in rows} == expected
    assert service.store.entries() == facts
