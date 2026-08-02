"""DDL e connessione. Unico posto in cui vive lo schema."""
import sqlite3

from .paths import DB_PATH, ensure_dirs

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS projects (
  id           INTEGER PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,
  name         TEXT NOT NULL,
  root_path    TEXT NOT NULL,
  git_remote   TEXT,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL UNIQUE,
  project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  config_dir  TEXT NOT NULL,
  cwd         TEXT NOT NULL,
  is_internal INTEGER NOT NULL DEFAULT 0,
  source      TEXT,             -- campo 'source' di SessionStart: distingue le
                                -- sessioni vere da quelle headless di altri strumenti
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  end_reason  TEXT,
  curated_at  TEXT
);

CREATE TABLE IF NOT EXISTS prompts (
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text       TEXT NOT NULL,
  ts         TEXT NOT NULL,
  curated    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
  id                   INTEGER PRIMARY KEY,
  task_key             TEXT NOT NULL UNIQUE,
  project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title                TEXT NOT NULL,
  detail               TEXT,
  status               TEXT NOT NULL
                       CHECK (status IN ('open','in_progress','claimed','verified','archived')),
  origin               TEXT NOT NULL
                       CHECK (origin IN ('todo_mirror','curator','manual','mcp')),
  origin_ref           TEXT,
  verify_cmd           TEXT,
  verify_cwd           TEXT,
  priority             INTEGER NOT NULL DEFAULT 0,
  confidence           REAL,
  parent_id            INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  last_touched_at      TEXT NOT NULL,
  claimed_at           TEXT,
  verified_at          TEXT,
  verified_commit      TEXT,
  verified_fingerprint TEXT,
  closed_at            TEXT
);

CREATE TABLE IF NOT EXISTS prompt_tasks (
  prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
  task_id   INTEGER NOT NULL REFERENCES tasks(id)   ON DELETE CASCADE,
  PRIMARY KEY (prompt_id, task_id)
);

CREATE TABLE IF NOT EXISTS evidence (
  id              INTEGER PRIMARY KEY,
  task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL
                  CHECK (kind IN ('file_edit','command','test','claim','manual_confirm')),
  payload         TEXT NOT NULL,
  payload_version INTEGER NOT NULL DEFAULT 1,
  source          TEXT NOT NULL CHECK (source IN ('hook','mcp','user')),
  ts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  tool        TEXT NOT NULL,
  target      TEXT,
  exit_code   INTEGER,          -- misurato assente nei payload di Claude Code v2.1.220
  stderr      TEXT,             -- cio' che l'hook osserva davvero al posto di exit_code
  interrupted INTEGER,
  cwd         TEXT,
  ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS curation_queue (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
  enqueued_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS curation_runs (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  input_hash  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('leased','done','failed')),
  lease_until TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  error       TEXT,
  UNIQUE (session_id, input_hash)
);

CREATE TABLE IF NOT EXISTS suggestions (
  id         INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
  title      TEXT NOT NULL,
  detail     TEXT,
  confidence REAL NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending','accepted','rejected')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_state (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  injection  TEXT NOT NULL,
  summary    TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applied_events (
  event_id   TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_status
  ON tasks(project_id, status, last_touched_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_session
  ON activity(session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_task
  ON evidence(task_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_prompts_uncurated
  ON prompts(session_id) WHERE curated = 0;
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=3.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


# Colonne aggiunte dopo la v1, su database che possono gia' esistere.
# CREATE TABLE IF NOT EXISTS non tocca una tabella gia' creata: le colonne
# nuove vanno aggiunte a mano, altrimenti un DB nato con la v1 resta senza.
_COLONNE_AGGIUNTE = [
    # (tabella, colonna, tipo)
    ("activity", "stderr", "TEXT"),
    ("activity", "interrupted", "INTEGER"),
    ("sessions", "source", "TEXT"),
    # Il percorso del transcript della sessione. Serve al revisore per sapere
    # non solo cosa e' stato chiesto, ma cosa l'agente ha risposto di aver
    # fatto (rischio R3 della SPEC): senza, apre task per lavori gia' conclusi.
    ("sessions", "transcript_path", "TEXT"),
]


def _aggiungi_colonne_mancanti(conn: sqlite3.Connection) -> None:
    for tabella, colonna, tipo in _COLONNE_AGGIUNTE:
        presenti = {r[1] for r in conn.execute(f"PRAGMA table_info({tabella})")}
        if colonna not in presenti:
            conn.execute(f"ALTER TABLE {tabella} ADD COLUMN {colonna} {tipo}")


def migrate(conn: sqlite3.Connection) -> None:
    """Applica lo schema. Sicuro da rieseguire, anche su un DB gia' popolato."""
    conn.executescript(DDL)
    _aggiungi_colonne_mancanti(conn)
    row = conn.execute("SELECT version FROM schema_meta").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] != SCHEMA_VERSION:
        conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))
