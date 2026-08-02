import importlib
import pytest

@pytest.fixture
def mods(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path / "home"))
    import myagents.paths, myagents.spool, myagents.schema, myagents.project, myagents.drain
    importlib.reload(myagents.paths)
    importlib.reload(myagents.spool)
    importlib.reload(myagents.schema)
    importlib.reload(myagents.project)
    return myagents.spool, myagents.schema, importlib.reload(myagents.drain)

def _session(spool, cwd, sid="sess-1"):
    spool.append_event("session_start", {
        "session_id": sid, "cwd": cwd, "config_dir": "/Users/x/.claude"})

def test_drain_creates_project_and_session(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-a"; work.mkdir()
    _session(spool, str(work))
    assert drain.drain() == 1
    conn = schema.connect()
    assert conn.execute("SELECT key FROM projects").fetchone()[0] == "progetto-a"
    assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "sess-1"

def test_drain_is_idempotent(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-b"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("prompt", {"session_id": "sess-1", "text": "ciao"})
    assert drain.drain() == 2
    assert drain.drain() == 0
    conn = schema.connect()
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1

def test_prompt_is_stored(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-c"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("prompt", {"session_id": "sess-1", "text": "sistemiamo il badge"})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT text FROM prompts").fetchone()[0] == "sistemiamo il badge"

def test_file_edit_creates_activity(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-d"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("file_edit", {
        "session_id": "sess-1", "tool": "Edit", "path": "/x/y.tsx"})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT tool, target FROM activity").fetchone() == ("Edit", "/x/y.tsx")

def test_command_records_exit_code(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-e"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("command", {
        "session_id": "sess-1", "tool": "Bash", "command": "pytest", "exit_code": 0})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT exit_code FROM activity").fetchone()[0] == 0

def test_todo_mirror_creates_tasks(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-f"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("todo", {"session_id": "sess-1", "items": [
        {"content": "Aggiungere auth JWT", "status": "in_progress"},
        {"content": "Fix layout mobile", "status": "pending"},
    ]})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT title,status,origin FROM tasks ORDER BY id").fetchall() == [
        ("Aggiungere auth JWT", "in_progress", "todo_mirror"),
        ("Fix layout mobile", "open", "todo_mirror"),
    ]

def test_completed_todo_becomes_claimed_not_verified(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-g"; work.mkdir()
    _session(spool, str(work))
    for status in ("pending", "completed"):
        spool.append_event("todo", {"session_id": "sess-1", "items": [
            {"content": "Aggiungere auth JWT", "status": status}]})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT title,status FROM tasks").fetchall() == \
        [("Aggiungere auth JWT", "claimed")]

def test_session_end_closes_and_enqueues(mods, tmp_path):
    spool, schema, drain = mods
    work = tmp_path / "progetto-h"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("session_end", {"session_id": "sess-1"})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT ended_at FROM sessions").fetchone()[0] is not None
    assert conn.execute("SELECT COUNT(*) FROM curation_queue").fetchone()[0] == 1

def test_event_for_unknown_session_creates_it_instead_of_losing_it(mods, tmp_path):
    """Un evento la cui sessione non e' ancora nota NON va perso.

    Ogni invocazione di hook e' un processo diverso -> un file di spool diverso,
    e i file si leggono in ordine di PID: il session_start puo' arrivare DOPO
    gli eventi della sua stessa sessione. Scartarli come "orfani" perdeva il
    57% degli eventi gia' catturati, in silenzio (misurato su dati reali).
    Ogni evento porta session_id e cwd: bastano a ricostruire la sessione.
    """
    spool, schema, drain = mods
    work = tmp_path / "progetto-orfano"
    work.mkdir()
    spool.append_event("prompt", {"session_id": "mai-vista", "cwd": str(work),
                                  "text": "non deve sparire"})
    assert drain.drain() == 1
    conn = schema.connect()
    assert conn.execute("SELECT text FROM prompts").fetchone()[0] == "non deve sparire"
    assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "mai-vista"


def test_late_session_start_does_not_duplicate_the_session(mods, tmp_path):
    """Quando il session_start arriva dopo, deve riconoscere la sessione gia'
    creata dai suoi stessi eventi invece di crearne una seconda."""
    spool, schema, drain = mods
    work = tmp_path / "progetto-tardivo"
    work.mkdir()
    spool.append_event("prompt", {"session_id": "s-tardiva", "cwd": str(work), "text": "prima"})
    drain.drain()
    spool.append_event("session_start", {"session_id": "s-tardiva", "cwd": str(work),
                                         "config_dir": "/x/.claude"})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1


def test_file_edit_is_attributed_to_the_file_project_not_the_session_cwd(mods, tmp_path):
    """Se apri Claude in una cartella e modifichi un file di un altro progetto,
    quella modifica appartiene al progetto DEL FILE.

    Osservato sui dati veri: una sessione partita da ~/Desktop/contabilita ha
    archiviato sotto 'contabilita' sedici modifiche a file di app-web. Nessun
    errore, nessun sintomo: solo lavoro registrato nel progetto sbagliato.
    """
    spool, schema, drain = mods
    sessione = tmp_path / "progetto-di-partenza"
    sessione.mkdir()
    import subprocess
    repo = tmp_path / "progetto-del-file"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    dentro = repo / "apps" / "web" / "src"
    dentro.mkdir(parents=True)
    spool.append_event("session_start", {"session_id": "s1", "cwd": str(sessione),
                                         "config_dir": "/x"})
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Edit",
                                     "path": str(dentro / "modulo.py")})
    drain.drain()
    conn = schema.connect()
    key = conn.execute("""SELECT p.key FROM activity a
                          JOIN projects p ON p.id = a.project_id""").fetchone()[0]
    # risale alla radice del repo, non alla cartella che contiene il file:
    # altrimenti .../apps/web/src/modulo.py diventerebbe il progetto "src"
    assert key == "progetto-del-file"
    # la sessione resta attribuita alla cartella da cui e' partita
    skey = conn.execute("""SELECT p.key FROM sessions s
                           JOIN projects p ON p.id = s.project_id""").fetchone()[0]
    assert skey == "progetto-di-partenza"


def test_file_outside_any_repo_falls_back_to_the_session_project(mods, tmp_path):
    """Senza un repository non si inventa un progetto dal nome della cartella:
    si resta su quello della sessione, che almeno e' reale."""
    spool, schema, drain = mods
    sessione = tmp_path / "sessione-vera"
    sessione.mkdir()
    sparso = tmp_path / "cartella" / "a" / "caso"
    sparso.mkdir(parents=True)
    spool.append_event("session_start", {"session_id": "s1", "cwd": str(sessione),
                                         "config_dir": "/x"})
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(sparso / "note.md")})
    drain.drain()
    conn = schema.connect()
    key = conn.execute("""SELECT p.key FROM activity a
                          JOIN projects p ON p.id = a.project_id""").fetchone()[0]
    assert key == "sessione-vera"


def test_command_stores_stderr_and_interrupted(mods, tmp_path):
    """exit_code non esiste nei payload di Claude Code (misurato): cio' che
    l'hook osserva davvero e' stderr e interrupted, e devono arrivare nel DB."""
    spool, schema, drain = mods
    work = tmp_path / "progetto-cmd"
    work.mkdir()
    spool.append_event("session_start", {"session_id": "s1", "cwd": str(work),
                                         "config_dir": "/x"})
    spool.append_event("command", {"session_id": "s1", "tool": "Bash",
                                   "command": "pytest -q",
                                   "stderr": "boom", "interrupted": True})
    drain.drain()
    conn = schema.connect()
    row = conn.execute(
        "SELECT target, exit_code, stderr, interrupted FROM activity").fetchone()
    assert row == ("pytest -q", None, "boom", 1)

def test_drained_file_is_retired_when_stale(mods, tmp_path):
    """Senza pulizia, drain() rilegge per sempre migliaia di file: uno per ogni
    invocazione di hook. Vedi la nota 'Pulizia dello spool' sopra."""
    import os, time as _t
    spool, schema, drain = mods
    work = tmp_path / "progetto-retire"; work.mkdir()
    _session(spool, str(work))
    (f,) = spool.spool_files()
    vecchio = _t.time() - (drain.STALE_AFTER + 5)
    os.utime(f, (vecchio, vecchio))
    drain.drain()
    assert spool.spool_files() == []

def test_fresh_file_is_not_retired(mods, tmp_path):
    """Un file appena scritto puo' appartenere a un processo vivo: non si tocca."""
    spool, schema, drain = mods
    work = tmp_path / "progetto-fresco"; work.mkdir()
    _session(spool, str(work))
    drain.drain()
    assert len(spool.spool_files()) == 1

def test_malformed_payload_shape_does_not_abort_drain(mods, tmp_path):
    """Una riga di spool corrotta (payload non-dict) non deve far cadere l'intero
    drain(): gli altri eventi vanno comunque applicati."""
    import json
    spool, schema, drain = mods
    work = tmp_path / "progetto-corrotto"; work.mkdir()
    _session(spool, str(work))
    (f,) = spool.spool_files()
    with open(f, "a") as fh:
        fh.write(json.dumps({
            "event_id": "bad-payload", "kind": "prompt",
            "ts": "2026-01-01T00:00:00Z", "payload": "not-a-dict",
        }) + "\n")
    spool.append_event("prompt", {"session_id": "sess-1", "text": "dopo il guasto"})
    n = drain.drain()
    assert n == 3  # session_start + evento corrotto (marcato applicato) + prompt valido
    conn = schema.connect()
    assert conn.execute("SELECT text FROM prompts").fetchone()[0] == "dopo il guasto"


def test_todo_with_unhashable_status_does_not_abort_drain(mods, tmp_path):
    """Uno status non-stringa (es. una lista) in un item todo corrotto non deve
    far esplodere il lookup su _TODO_STATUS ne' far cadere il drain()."""
    spool, schema, drain = mods
    work = tmp_path / "progetto-status-rotto"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("todo", {"session_id": "sess-1", "items": [
        {"content": "Task con status rotto", "status": ["non", "valido"]}]})
    drain.drain()
    conn = schema.connect()
    assert conn.execute("SELECT title,status FROM tasks").fetchall() == \
        [("Task con status rotto", "open")]


def test_retired_file_is_not_reprocessed(mods, tmp_path):
    """Dopo la pulizia, un secondo drain non deve trovare nulla da fare."""
    import os, time as _t
    spool, schema, drain = mods
    work = tmp_path / "progetto-due-giri"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("prompt", {"session_id": "sess-1", "text": "ciao"})
    (f,) = spool.spool_files()
    vecchio = _t.time() - (drain.STALE_AFTER + 5)
    os.utime(f, (vecchio, vecchio))
    assert drain.drain() == 2
    assert drain.drain() == 0
    conn = schema.connect()
    assert conn.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 1


def test_unapplied_event_blocks_retirement_even_when_stale(mods, tmp_path):
    """Un evento che fallisce dentro la sua transazione (qui: session_id NULL
    viola il vincolo NOT NULL) non deve mai finire in applied_events. Se il
    file viene comunque ritirato perche' 'vecchio', quell'evento e' perso per
    sempre: nessun errore, nessuna traccia. L'evento buono nello stesso file
    deve comunque applicarsi."""
    import os, time as _t
    spool, schema, drain = mods
    work = tmp_path / "progetto-evento-perso"; work.mkdir()
    _session(spool, str(work))  # evento buono: session_start valido
    spool.append_event("session_start", {
        "session_id": None, "cwd": str(work), "config_dir": "/x"})  # NOT NULL -> fallisce
    (f,) = spool.spool_files()
    vecchio = _t.time() - (drain.STALE_AFTER + 5)
    os.utime(f, (vecchio, vecchio))
    n = drain.drain()
    assert n == 1  # solo l'evento buono va a buon fine
    conn = schema.connect()
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert spool.spool_files() != []  # il file NON deve essere cancellato


def test_stale_file_with_all_events_already_applied_is_retired(mods, tmp_path):
    """Guardia di regressione per la trappola opposta: se un file e' stato
    interamente applicato in un giro precedente, un giro successivo lo deve
    comunque ritirare quando diventa vecchio. 'gia' applicato' (rowcount==0)
    e' un successo, non un fallimento: trattarlo come fallimento
    bloccherebbe la pulizia per sempre."""
    import os, time as _t
    spool, schema, drain = mods
    work = tmp_path / "progetto-gia-applicato"; work.mkdir()
    _session(spool, str(work))
    spool.append_event("prompt", {"session_id": "sess-1", "text": "ciao"})
    assert drain.drain() == 2  # prima passata: entrambi applicati, file fresco -> non ritirato
    assert len(spool.spool_files()) == 1
    (f,) = spool.spool_files()
    vecchio = _t.time() - (drain.STALE_AFTER + 5)
    os.utime(f, (vecchio, vecchio))
    assert drain.drain() == 0  # seconda passata: gia' applicati, nessun fallimento
    assert spool.spool_files() == []  # ora il file viene ritirato


def test_broad_catch_writes_to_error_log(mods, tmp_path):
    """Quando la except Exception generica scatta, deve restare traccia in
    ERROR_LOG con l'id dell'evento che ha fallito: altrimenti un bug vero
    (NameError da un typo, AttributeError dopo un refactor) sparisce nel
    nulla insieme al defect 1."""
    import myagents.paths as paths
    spool, schema, drain = mods
    work = tmp_path / "progetto-log-errori"; work.mkdir()
    event_id = spool.append_event("session_start", {
        "session_id": None, "cwd": str(work), "config_dir": "/x"})
    drain.drain()
    assert paths.ERROR_LOG.exists()
    content = paths.ERROR_LOG.read_text()
    assert event_id in content
