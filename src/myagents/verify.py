"""La verifica: l'unico modo per cui un task puo' diventare verde.

Perche' esiste. Il piano originale faceva diventare verde un task guardando il
codice di uscita dei comandi lanciati da Claude. Misurando i payload reali
(docs/findings-fase-1.md) si e' scoperto che Claude Code **non espone nessun
codice di uscita**: quel meccanismo non sarebbe mai scattato, e il sistema
sarebbe rimasto giallo per sempre senza che nessuno capisse perche'.

Quindi la verifica non osserva: **esegue**. Taskdb lancia lui il comando, con
la sua cwd, e registra l'esito reale insieme al commit corrente e a
un'impronta dei file coinvolti.

Il guadagno e' doppio, e chiude anche l'obiezione sollevata da Codex sul
`verify_cmd` debole: se e' Claude a lanciare il test, un test filtrato o il
comando giusto nella cartella sbagliata risultano comunque verdi. Se lo lancia
sempre lo stesso attore nello stesso posto, quel margine sparisce.

**Verifica scaduta.** Un verde di tre giorni fa su codice cambiato ieri non e'
una verifica: e' un ricordo. L'impronta dei file al momento del successo
permette di accorgersene e di tornare a segnalarlo.
"""
import hashlib
import json
import os
import subprocess

from .paths import utcnow

TIMEOUT_SECONDI = 600
_MAX_OUTPUT = 2000
_MAX_FILE = 40


def impronta_file(percorsi) -> str:
    """Impronta di un insieme di file: dimensione e data di modifica.

    Non il contenuto: leggere ogni file sarebbe lento e qui basta sapere SE
    sono cambiati, non come. Un file sparito conta come cambiamento.
    """
    h = hashlib.sha256()
    for percorso in sorted({str(p) for p in percorsi if p}):
        try:
            st = os.stat(percorso)
            h.update(f"{percorso}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))
        except OSError:
            h.update(f"{percorso}:assente".encode("utf-8"))
    return h.hexdigest()[:32]


def _file_del_task(conn, task_id: int) -> list:
    """I file che il sistema ha visto toccare nel progetto di questo task."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT a.target FROM activity a"
        " JOIN tasks t ON t.project_id = a.project_id"
        " WHERE t.id = ? AND a.tool IN ('Edit','Write','MultiEdit')"
        "   AND a.target IS NOT NULL ORDER BY a.ts DESC LIMIT ?",
        (task_id, _MAX_FILE))]


def _commit(cwd: str) -> str:
    try:
        esito = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                               capture_output=True, text=True, timeout=5)
        return esito.stdout.strip() if esito.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def verifica(conn, task_key: str, comando: str = "", cwd: str = "") -> dict:
    """Esegue il comando di verifica di un task e ne registra l'esito reale.

    Verde SOLO su uscita 0. Qualunque altro esito lascia il task dov'e' e
    registra il fallimento: un tentativo andato male e' un'informazione, non
    un motivo per fingere che non sia successo.
    """
    riga = conn.execute(
        "SELECT t.id, t.verify_cmd, t.verify_cwd, p.root_path"
        " FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.task_key = ?",
        (task_key,)).fetchone()
    if not riga:
        return {"ok": False, "errore": f"task {task_key} inesistente"}
    tid, cmd_salvato, cwd_salvata, radice = riga

    cmd = comando or cmd_salvato
    if not cmd:
        return {"ok": False,
                "errore": "nessun comando di verifica: passane uno con --cmd"}
    cartella = cwd or cwd_salvata or radice
    if not cartella or not os.path.isdir(cartella):
        return {"ok": False, "errore": f"cartella non valida: {cartella!r}"}

    # Si ricorda il comando: la prossima verifica non richiede di riscriverlo.
    conn.execute("UPDATE tasks SET verify_cmd = ?, verify_cwd = ?, updated_at = ?"
                 " WHERE id = ?", (cmd, cartella, utcnow(), tid))

    try:
        # shell=True perche' un comando di verifica reale contiene pipe e &&.
        # E' un comando che hai scritto tu per il tuo progetto: stesso livello
        # di fiducia del progetto stesso.
        esito = subprocess.run(cmd, cwd=cartella, shell=True, capture_output=True,
                               text=True, timeout=TIMEOUT_SECONDI)
        uscita, out, err = esito.returncode, esito.stdout, esito.stderr
    except subprocess.TimeoutExpired:
        uscita, out, err = -1, "", f"scaduto dopo {TIMEOUT_SECONDI}s"
    except (OSError, subprocess.SubprocessError) as exc:
        uscita, out, err = -1, "", f"{type(exc).__name__}: {exc}"

    adesso = utcnow()
    prova = json.dumps({
        "comando": cmd, "cwd": cartella, "exit_code": uscita,
        "stdout": (out or "")[-_MAX_OUTPUT:], "stderr": (err or "")[-_MAX_OUTPUT:],
    }, ensure_ascii=False)

    if uscita == 0:
        impronta = impronta_file(_file_del_task(conn, tid))
        conn.execute(
            "UPDATE tasks SET status='verified', verified_at=?, verified_commit=?,"
            " verified_fingerprint=?, updated_at=?, last_touched_at=? WHERE id=?",
            (adesso, _commit(cartella), impronta, adesso, adesso, tid))
        # source='hook': prova raccolta eseguendo, non dichiarata da nessuno.
        conn.execute(
            "INSERT INTO evidence (task_id,kind,payload,source,ts) VALUES (?,?,?,?,?)",
            (tid, "test", prova, "hook", adesso))
        return {"ok": True, "verde": True, "task": task_key, "uscita": 0}

    conn.execute(
        "INSERT INTO evidence (task_id,kind,payload,source,ts) VALUES (?,?,?,?,?)",
        (tid, "command", prova, "hook", adesso))
    conn.execute("UPDATE tasks SET last_touched_at=? WHERE id=?", (adesso, tid))
    return {"ok": True, "verde": False, "task": task_key, "uscita": uscita,
            "stderr": (err or out or "")[-400:]}


def verifica_scaduta(conn, task_id: int) -> bool:
    """True se il task e' verde ma i file coinvolti sono cambiati dopo.

    Un verde di tre giorni fa su codice cambiato ieri non e' una verifica: e'
    un ricordo. Va segnalato, non lasciato passare per buono.
    """
    riga = conn.execute(
        "SELECT status, verified_fingerprint FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not riga or riga[0] != "verified" or not riga[1]:
        return False
    return impronta_file(_file_del_task(conn, task_id)) != riga[1]


def scadute(conn) -> list:
    """I task verdi la cui verifica non vale piu'."""
    fuori = []
    for tid, chiave, titolo in conn.execute(
            "SELECT id, task_key, title FROM tasks WHERE status='verified'"
            " AND verified_fingerprint IS NOT NULL"):
        if verifica_scaduta(conn, tid):
            fuori.append({"task_key": chiave, "titolo": titolo})
    return fuori
