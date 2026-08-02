import importlib, json, os
import pytest

@pytest.fixture
def spool(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path))
    import myagents.paths, myagents.spool
    importlib.reload(myagents.paths)
    return importlib.reload(myagents.spool)

def test_append_writes_one_json_line(spool):
    eid = spool.append_event("prompt", {"text": "ciao"})
    files = spool.spool_files()
    assert len(files) == 1
    events = list(spool.read_events(files[0]))
    assert len(events) == 1
    assert events[0]["event_id"] == eid
    assert events[0]["kind"] == "prompt"
    assert events[0]["payload"]["text"] == "ciao"
    assert events[0]["ts"].endswith("Z")

def test_event_ids_are_unique(spool):
    ids = {spool.append_event("prompt", {"text": str(i)}) for i in range(50)}
    assert len(ids) == 50

def test_appends_accumulate_in_same_file(spool):
    for i in range(5):
        spool.append_event("activity", {"i": i})
    (f,) = spool.spool_files()
    assert len(list(spool.read_events(f))) == 5

def test_long_payload_is_truncated_not_dropped(spool):
    spool.append_event("prompt", {"text": "x" * 20000})
    (f,) = spool.spool_files()
    (ev,) = list(spool.read_events(f))
    assert ev["truncated"] is True
    assert len(json.dumps(ev).encode()) <= spool.MAX_LINE
    assert ev["payload"]["text"].startswith("xxx")

def test_corrupt_line_is_skipped_not_fatal(spool):
    spool.append_event("prompt", {"text": "buona"})
    (f,) = spool.spool_files()
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("{ questa non e' json\n")
    events = list(spool.read_events(f))
    assert len(events) == 1
    assert events[0]["payload"]["text"] == "buona"

def test_file_permissions_are_private(spool):
    spool.append_event("prompt", {"text": "ciao"})
    (f,) = spool.spool_files()
    assert oct(os.stat(f).st_mode)[-3:] == "600"

def test_huge_list_is_truncated_not_dropped(spool):
    """Payload with huge list (real-world todo event shape)."""
    payload = {
        "session_id": "s1",
        "cwd": "/home/user",
        "items": [
            {"content": "x" * 100, "status": "pending"}
            for _ in range(500)
        ],
    }
    spool.append_event("todo", payload)
    (f,) = spool.spool_files()
    (ev,) = list(spool.read_events(f))
    assert ev["truncated"] is True
    assert len(json.dumps(ev).encode()) <= spool.MAX_LINE
    assert ev["payload"]["session_id"] == "s1"

def test_many_long_string_fields_are_truncated(spool):
    """Payload with many medium string fields that exceed MAX_LINE together."""
    payload = {
        "session_id": "s2",
        **{f"field_{i}": "y" * 400 for i in range(30)},
    }
    spool.append_event("batch", payload)
    (f,) = spool.spool_files()
    (ev,) = list(spool.read_events(f))
    assert ev["truncated"] is True
    assert len(json.dumps(ev).encode()) <= spool.MAX_LINE
    assert ev["payload"]["session_id"] == "s2"

def test_small_payload_unchanged_no_truncated_flag(spool):
    """Small payload should pass through unchanged, no truncated flag."""
    payload = {"session_id": "s3", "text": "hello world"}
    spool.append_event("small", payload)
    (f,) = spool.spool_files()
    (ev,) = list(spool.read_events(f))
    assert "truncated" not in ev or ev.get("truncated") is False
    assert ev["payload"] == payload

def test_session_id_survives_minimal_degradation(spool):
    """session_id must survive even extreme truncation."""
    # Very large payload that forces degradation to minimal
    huge_dict = {
        "session_id": "critical_session_123",
        "data": {"nested": {"deeply": "x" * 50000}},
        **{f"f{i}": "z" * 5000 for i in range(50)},
    }
    spool.append_event("extreme", huge_dict)
    (f,) = spool.spool_files()
    (ev,) = list(spool.read_events(f))
    assert ev["truncated"] is True
    assert len(json.dumps(ev).encode()) <= spool.MAX_LINE
    assert ev["payload"]["session_id"] == "critical_session_123"


def test_unserializable_value_is_degraded_not_raised(spool):
    """Un evento non serializzabile non deve MAI sollevare: l'hook ingoia le
    eccezioni, quindi un raise qui significa evento perso in silenzio."""
    for payload in ({"session_id": "s1", "b": b"abc"},
                    {"session_id": "s2", "s": {1, 2}},
                    {"session_id": "s3", "n": 10 ** 5000}):
        spool.append_event("todo", payload)  # non deve sollevare
    events = []
    for f in spool.spool_files():
        events.extend(spool.read_events(f))
    assert len(events) == 3
    assert [e["payload"]["session_id"] for e in events] == ["s1", "s2", "s3"]
    for ev in events:
        assert len(json.dumps(ev).encode()) <= spool.MAX_LINE
