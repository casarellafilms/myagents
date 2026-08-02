import importlib, sqlite3
import pytest

@pytest.fixture
def schema(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path))
    import myagents.paths, myagents.schema
    importlib.reload(myagents.paths)
    return importlib.reload(myagents.schema)

def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}

def _seed_project(conn):
    conn.execute(
        "INSERT INTO projects (key,name,root_path,created_at,last_seen_at)"
        " VALUES ('p','p','/tmp','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')")

def test_migrate_creates_all_tables(schema):
    conn = schema.connect()
    schema.migrate(conn)
    expected = {
        "schema_meta", "projects", "sessions", "prompts", "tasks",
        "prompt_tasks", "evidence", "activity", "curation_queue",
        "curation_runs", "suggestions", "project_state", "settings",
        "applied_events",
    }
    assert expected <= _tables(conn)

def test_migrate_is_idempotent(schema):
    conn = schema.connect()
    schema.migrate(conn)
    schema.migrate(conn)
    assert conn.execute("SELECT version FROM schema_meta").fetchall() == \
        [(schema.SCHEMA_VERSION,)]

def test_wal_and_foreign_keys_enabled(schema):
    conn = schema.connect()
    schema.migrate(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

def test_task_status_is_constrained(schema):
    conn = schema.connect()
    schema.migrate(conn)
    _seed_project(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (task_key,project_id,title,status,origin,"
            "created_at,updated_at,last_touched_at) VALUES"
            " ('p/T-1',1,'x','inventato','manual',"
            "'2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')")

def test_evidence_source_is_constrained(schema):
    conn = schema.connect()
    schema.migrate(conn)
    _seed_project(conn)
    conn.execute(
        "INSERT INTO tasks (task_key,project_id,title,status,origin,"
        "created_at,updated_at,last_touched_at) VALUES"
        " ('p/T-1',1,'x','open','manual',"
        "'2026-08-01T00:00:00Z','2026-08-01T00:00:00Z','2026-08-01T00:00:00Z')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO evidence (task_id,kind,payload,source,ts) VALUES"
            " (1,'file_edit','{}','inventato','2026-08-01T00:00:00Z')")

def test_hot_indexes_exist(schema):
    conn = schema.connect()
    schema.migrate(conn)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    names = {r[0] for r in rows}
    assert "idx_tasks_project_status" in names
    assert "idx_activity_session" in names
