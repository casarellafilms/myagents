"""Spool -> DB. Unico scrittore del database."""
import os
import sqlite3
import time
import traceback

from .paths import ERROR_LOG, utcnow
from .project import detect
from .render import aggiorna_iniezioni
from .schema import connect, migrate
from .spool import read_events, spool_files

_TODO_STATUS = {
    "pending": "open",
    "in_progress": "in_progress",
    "completed": "claimed",
}

# I file di spool si chiamano <pid>-<data>.jsonl e i PID vengono riciclati dal
# sistema operativo: cancellare un file scritto negli ultimi STALE_AFTER secondi
# rischierebbe di perdere eventi da un nuovo processo che ha riusato quel PID.
STALE_AFTER = 60


def upsert_project(conn: sqlite3.Connection, cwd: str) -> int:
    info = detect(cwd)
    now = utcnow()
    conn.execute(
        "INSERT INTO projects (key,name,root_path,git_remote,created_at,last_seen_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (info["key"], info["name"], info["root_path"], info["git_remote"], now, now),
    )
    return conn.execute(
        "SELECT id FROM projects WHERE key = ?", (info["key"],)
    ).fetchone()[0]


def progetto_del_file(conn, percorso, cache: dict, ripiego: int) -> int:
    """Progetto a cui appartiene un file modificato.

    Il progetto NON e' quello della cartella da cui hai lanciato Claude: se apri
    una sessione in ~/Desktop/contabilita e modifichi un file dentro app-web, quel
    lavoro e' di app-web. Attribuirlo alla cartella di partenza archivia le
    modifiche sotto un progetto che non c'entra -- il sistema non da' errori e
    ti racconta una cosa falsa, che e' il fallimento peggiore per un progetto
    che esiste per ricordare correttamente.

    `detect()` invoca git, quindi si tiene una cache per cartella valida per la
    durata di un solo drain(): senza, sarebbero due fork di git per ogni riga.
    """
    if not percorso:
        return ripiego
    cartella = os.path.dirname(str(percorso))
    if not cartella:
        return ripiego
    if cartella in cache:
        return cache[cartella] if cache[cartella] is not None else ripiego

    esito = None
    try:
        info = detect(cartella)
        # Solo se il file sta dentro un repository git vero. Senza repo,
        # detect() ripiega sul nome della cartella che contiene il file, e
        # .../app-web/apps/web/src/app/api/cron/route.ts finirebbe sotto un
        # progetto chiamato "cron". Meglio la cartella della sessione, che
        # almeno e' un progetto reale.
        if os.path.isdir(os.path.join(info["root_path"], ".git")):
            esito = upsert_project(conn, cartella)
    except (sqlite3.Error, OSError):
        esito = None
    cache[cartella] = esito
    return esito if esito is not None else ripiego


def _session_row(conn: sqlite3.Connection, session_id: str):
    return conn.execute(
        "SELECT id, project_id FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()


def upsert_session(conn, session_id: str, cwd: str, config_dir: str) -> int:
    project_id = upsert_project(conn, cwd)
    conn.execute(
        "INSERT INTO sessions (session_id,project_id,config_dir,cwd,started_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO NOTHING",
        (session_id, project_id, config_dir, cwd, utcnow()),
    )
    return _session_row(conn, session_id)[0]


def _next_task_key(conn, project_id: int) -> str:
    key = conn.execute(
        "SELECT key FROM projects WHERE id = ?", (project_id,)
    ).fetchone()[0]
    count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    return f"{key}/T-{count + 1:04d}"


# Un titolo e' una riga, non un rapporto. Misurato sul campo: senza questo
# limite il mirror ha prodotto titoli da 1254 caratteri -- l'intera risposta
# dell'agente finita dentro il campo `content` di un TaskCreate. Le conseguenze
# erano due, entrambe silenziose:
#   - il bigliettino iniettato ha un tetto di 1800 caratteri: DUE task cosi' lo
#     saturavano, e l'agente riceveva un muro di testo invece dell'avviso;
#   - nella dashboard una scheda diventava alta 759 pixel.
# Il revisore troncava gia' a 70; il mirror no, e nessuno se n'era accorto.
MAX_TITOLO = 90


def _mirror_todo(conn, project_id: int, item: dict) -> None:
    grezzo = " ".join((item.get("content") or "").split())
    if not grezzo:
        return
    title = grezzo[:MAX_TITOLO] + ("…" if len(grezzo) > MAX_TITOLO else "")
    # Il testo pieno non si butta: finisce nel dettaglio, dove lo si legge
    # quando serve senza che occupi il posto del titolo ovunque.
    detail = grezzo if len(grezzo) > MAX_TITOLO else None
    raw_status = item.get("status", "pending")
    # raw_status arriva da un evento sullo spool: se non e' una stringa (es. una
    # lista in un evento corrotto), e' anche non hashable, e _TODO_STATUS.get()
    # solleverebbe TypeError invece di limitarsi a non riconoscere lo stato.
    status = _TODO_STATUS.get(raw_status, "open") if isinstance(raw_status, str) else "open"
    now = utcnow()
    existing = conn.execute(
        "SELECT id FROM tasks WHERE project_id = ? AND lower(title) = lower(?)",
        (project_id, title),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, last_touched_at = ?,"
            " claimed_at = CASE WHEN ? = 'claimed' THEN ? ELSE claimed_at END"
            " WHERE id = ?",
            (status, now, now, status, now, existing[0]),
        )
        return
    conn.execute(
        "INSERT INTO tasks (task_key,project_id,title,detail,status,origin,"
        "created_at,updated_at,last_touched_at,claimed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_next_task_key(conn, project_id), project_id, title, detail, status,
         "todo_mirror", now, now, now, now if status == "claimed" else None),
    )


def _recupera_accodate(conn, sid: int, project_id: int, percorso) -> int:
    """Inserisce le richieste accodate del transcript che non sono gia' dentro.

    Perche' qui e non nell'hook: scorrere un transcript da migliaia di righe nel
    percorso critico di una sessione violerebbe il budget di latenza (SPEC P3).
    Il travaso gira nel servizio, dove nessuno lo aspetta, ogni venti secondi --
    quindi in pratica le richieste compaiono quasi subito senza costare nulla a
    chi sta lavorando.

    Il doppione si evita confrontando testo e istante: la stessa riga di
    transcript verra' riletta a ogni giro, e senza questo controllo la stessa
    richiesta si moltiplicherebbe per il numero di travasi.
    """
    if not percorso:
        return 0
    try:
        from .transcript import accodate
        candidate = accodate(str(percorso))
    except Exception:
        return 0  # il recupero e' un di piu': non deve mai fermare il travaso

    inserite = 0
    for voce in candidate:
        testo, quando = voce.get("testo", ""), voce.get("ts") or utcnow()
        if not testo:
            continue
        if conn.execute("SELECT 1 FROM prompts WHERE session_id = ? AND ts = ?"
                        " AND text = ?", (sid, quando, testo)).fetchone():
            continue
        conn.execute(
            "INSERT INTO prompts (session_id,project_id,text,ts) VALUES (?,?,?,?)",
            (sid, project_id, testo, quando))
        inserite += 1
    return inserite


def _apply(conn: sqlite3.Connection, event: dict, cache: dict) -> None:
    kind = event.get("kind")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        # Lo spool arriva da un file su disco: una riga corrotta o un evento
        # scritto da una versione futura potrebbe avere un payload che non e'
        # un dict. Trattarlo come vuoto (niente session_id -> orfano, nessun
        # effetto) invece di lasciar esplodere .get() piu' sotto.
        payload = {}
    session_id = payload.get("session_id")
    ts = event.get("ts") or utcnow()

    if kind == "session_start":
        sid_row = upsert_session(conn, session_id, payload.get("cwd", ""),
                                 payload.get("config_dir", ""))
        # `source` distingue una sessione vera da una headless lanciata da un
        # altro strumento. Misurato sul campo: 7 sessioni fantasma su 8 nel primo
        # test reale erano il riassuntore di un altro plugin, indistinguibili
        # dalle tue. Registriamo il dato invece di indovinare la regola.
        if payload.get("source"):
            conn.execute("UPDATE sessions SET source = ? WHERE id = ?",
                         (payload.get("source"), sid_row))
        return

    if not session_id:
        return  # nessun modo di attribuirlo: marcato applicato, nessun effetto

    row = _session_row(conn, session_id)
    if row is None:
        # NON scartare. Ogni invocazione di hook e' un processo diverso, quindi un
        # file di spool diverso, e i file si leggono in ordine di PID: il
        # `session_start` di una sessione puo' arrivare DOPO i suoi stessi eventi.
        # Scartarli come "orfani" perdeva il 57% degli eventi gia' catturati, in
        # silenzio (misurato su dati reali). Ogni evento porta con se' session_id
        # e cwd: bastano a creare la sessione.
        upsert_session(conn, session_id, payload.get("cwd", ""),
                       payload.get("config_dir", ""))
        row = _session_row(conn, session_id)
        if row is None:
            return
    sid, project_id = row

    if kind == "prompt":
        conn.execute(
            "INSERT INTO prompts (session_id,project_id,text,ts) VALUES (?,?,?,?)",
            (sid, project_id, payload.get("text", ""), ts),
        )
        # Insieme alla richiesta che l'hook HA visto si recuperano quelle che non
        # poteva vedere: i messaggi scritti mentre l'agente lavorava. Vedi
        # transcript.py -- e' il buco piu' grave che questo progetto abbia avuto.
        _recupera_accodate(conn, sid, project_id, payload.get("transcript_path"))
    elif kind == "file_edit":
        percorso = payload.get("path")
        conn.execute(
            "INSERT INTO activity (session_id,project_id,tool,target,cwd,ts)"
            " VALUES (?,?,?,?,?,?)",
            (sid, progetto_del_file(conn, percorso, cache, project_id),
             payload.get("tool", "Edit"), percorso, payload.get("cwd"), ts),
        )
    elif kind == "command":
        # exit_code resta nullo: Claude Code non lo espone (misurato, vedi
        # docs/findings-fase-1.md). Cio' che l'hook osserva davvero e' stderr e
        # interrupted, e finora venivano letti come 'exit_code' e quindi persi.
        conn.execute(
            "INSERT INTO activity"
            " (session_id,project_id,tool,target,exit_code,stderr,interrupted,cwd,ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, project_id, payload.get("tool", "Bash"), payload.get("command"),
             payload.get("exit_code"), payload.get("stderr"),
             1 if payload.get("interrupted") else 0, payload.get("cwd"), ts),
        )
    elif kind == "todo":
        for item in payload.get("items") or []:
            if isinstance(item, dict):
                _mirror_todo(conn, project_id, item)
    elif kind == "session_end":
        # Ultima passata prima di consegnare la sessione al revisore: se le
        # ultime richieste erano accodate, e' l'unica occasione per prenderle.
        _recupera_accodate(conn, sid, project_id, payload.get("transcript_path"))
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = 'clean' WHERE id = ?",
            (ts, sid),
        )
        conn.execute(
            "INSERT INTO curation_queue (session_id,enqueued_at) VALUES (?,?)"
            " ON CONFLICT(session_id) DO NOTHING",
            (sid, ts),
        )


def _log_error(event: dict) -> None:
    """Lascia traccia in ERROR_LOG di un errore imprevisto durante l'apply di
    un evento. Va chiamata da dentro un blocco `except`: usa
    traceback.format_exc() per catturare l'eccezione corrente.

    Stessa disciplina append-and-never-raise usata dagli hook per il proprio
    logging: un problema qui (disco pieno, permessi) non deve mai far cadere
    il drain, e non deve mai mascherare l'eccezione originale."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(
                f"{utcnow()} drain event_id={event.get('event_id')}"
                f" kind={event.get('kind')}\n{traceback.format_exc()}\n"
            )
    except Exception:
        pass


def _retire(path, all_applied: bool) -> None:
    """Cancella un file di spool gia' drenato, ma solo se e' fermo da STALE_AFTER
    secondi (i PID vengono riciclati e un nuovo processo con lo stesso PID
    potrebbe ancora starci scrivendo dentro) E se ogni evento letto da questo
    file in questa passata e' stato applicato con successo o era gia' stato
    applicato in una passata precedente. Un evento fallito (rollback) non deve
    mai finire in applied_events: se il file venisse comunque cancellato,
    quell'evento sparirebbe per sempre senza errore ne' traccia."""
    if not all_applied:
        return
    try:
        if time.time() - path.stat().st_mtime >= STALE_AFTER:
            path.unlink()
    except OSError:
        pass


def drain() -> int:
    """Applica tutti gli eventi non ancora applicati. Ritorna quanti ne ha applicati."""
    conn = connect()
    migrate(conn)
    applied = 0
    # cache cartella -> progetto, valida per un solo drain():
    # detect() invoca git, senza cache sarebbero due fork per riga.
    cache: dict = {}
    toccati: set = set()
    for path in spool_files():
        all_applied = True
        for event in read_events(path):
            event_id = event.get("event_id")
            if not event_id:
                continue
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                # Non siamo nemmeno riusciti ad aprire la transazione (es. db
                # bloccato da un altro processo oltre il busy_timeout): non
                # c'e' nulla da annullare. Si ritenta al prossimo drain(), quindi
                # il file non va ritirato in questa passata.
                all_applied = False
                continue
            try:
                cur = conn.execute(
                    "INSERT INTO applied_events (event_id,applied_at) VALUES (?,?)"
                    " ON CONFLICT(event_id) DO NOTHING",
                    (event_id, utcnow()),
                )
                if cur.rowcount == 0:
                    # Gia' applicato in una passata precedente: e' un successo,
                    # non un fallimento. Non deve bloccare il ritiro del file,
                    # altrimenti i file gia' drenati non verrebbero mai piu'
                    # ripuliti.
                    conn.execute("ROLLBACK")
                    continue
                _apply(conn, event, cache)
                sid = (event.get('payload') or {}).get('session_id') \
                    if isinstance(event.get('payload'), dict) else None
                if sid:
                    r = _session_row(conn, sid)
                    if r:
                        toccati.add(r[1])
                conn.execute("COMMIT")
                applied += 1
            except Exception:
                # Non solo sqlite3.Error: un evento sullo spool e' dato non
                # fidato (file su disco, non un contratto Python). Una forma
                # inattesa che facesse esplodere _apply() non deve mai far
                # cadere l'intero drain() -- gli altri file/eventi vanno
                # comunque processati. L'evento non viene marcato applicato:
                # verra' ritentato al prossimo giro (stesso comportamento
                # gia' in vigore per i veri sqlite3.Error, es. violazioni di
                # vincolo). Il file che lo contiene non va ritirato finche'
                # l'evento non si applica con successo.
                all_applied = False
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    # La transazione era gia' stata chiusa (o mai aperta
                    # correttamente): niente da annullare. Non deve comunque
                    # far cadere il resto del drain().
                    pass
                _log_error(event)
        _retire(path, all_applied)

    # Riscrive il bigliettino dei progetti toccati. Anche quelli raggiunti solo
    # da un file modificato: se lavori su app-web da una sessione aperta
    # altrove, il suo bigliettino deve aggiornarsi lo stesso.
    try:
        toccati |= {r[0] for r in conn.execute(
            "SELECT DISTINCT project_id FROM activity")}
        aggiorna_iniezioni(conn, toccati)
    except (sqlite3.Error, OSError):
        # Il bigliettino e' un extra: non deve mai far fallire il travaso, che
        # e' l'unica cosa che protegge i dati gia' catturati.
        pass
    return applied
