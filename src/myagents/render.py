"""Costruisce il bigliettino che Claude legge all'inizio di ogni messaggio.

Gira nel drainer, mai in un hook: interroga il database con query aggregate.
L'hook si limita a leggere il file gia' scritto da qui.

Tre regole, tutte pagate care nella fase 1:

1. **Stato, non storia.** Cosa stavi facendo, non tutto quello che e' successo.
   Un bigliettino lungo peggiora il contesto invece di migliorarlo, cioe'
   esattamente il problema che dovrebbe risolvere.

2. **Solo sessioni che hanno fatto qualcosa.** Altri strumenti lanciano Claude
   in modalita' headless e quelle invocazioni arrivano indistinguibili dalle
   tue (misurato: 7 su 8 nel primo test reale). Invece di riconoscerle con una
   regola fragile su *chi sono*, si guarda *cosa hanno fatto*: una sessione
   senza attivita' non racconta niente e sparisce da sola.

3. **Niente e' meglio di sbagliato.** Se non c'e' materiale, non si scrive il
   file. Un bigliettino che afferma il falso su cosa stavi facendo ti fa
   ripartire da un presupposto errato senza che tu lo sappia.
"""
import os
import sqlite3

from .paths import (INJECTION_DIR, MAX_CARATTERI, chiave_cartella,
                    ensure_dirs, utcnow)

_MAX_RICHIESTE = 8
_MAX_CARTELLE = 2
# Un titolo dentro il bigliettino sta in una riga. Il tetto complessivo e' 1800
# caratteri: senza questo limite due soli task lo saturavano (misurato).
_MAX_TITOLO = 90

# Non tutto cio' che arriva su UserPromptSubmit e' una tua richiesta. Passano di
# li' anche notifiche interne dell'harness, riassunti automatici di altri
# plugin e blocchi di macchinario. Mostrarli come "ultima richiesta" riempie la
# scheda di spazzatura -- osservato: <task-notification>, tool-use-id, e il
# riassuntore di ECC.
_RUMORE = (
    "<task-notification", "<system-reminder", "<tool-use-id", "<local-command",
    "below is a conversation log", "caveat: the messages below",
    "<command-name>", "<command-message>",
)


def _e_rumore(testo: str) -> bool:
    t = (testo or "").strip().lower()
    if not t or len(t) < 3:
        return True
    return any(t.startswith(x) or x in t[:200] for x in _RUMORE)


def _eta(conn: sqlite3.Connection, quando: str) -> str:
    if not quando:
        return ""
    minuti = conn.execute(
        "SELECT CAST((julianday(?) - julianday(?)) * 1440 AS INTEGER)",
        (utcnow(), quando),
    ).fetchone()[0]
    if minuti is None or minuti < 0:
        return ""
    if minuti < 60:
        return f"{minuti}min fa"
    if minuti < 60 * 48:
        return f"{minuti // 60}h fa"
    return f"{minuti // 1440}gg fa"


def costruisci_testo(conn: sqlite3.Connection, project_id: int) -> str:
    """Il bigliettino per un progetto. Stringa vuota se non c'e' niente da dire."""
    nome = conn.execute(
        "SELECT key FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if not nome:
        return ""
    righe = [f"[myagents · {nome[0]}]"]

    aperti = conn.execute(
        "SELECT title FROM tasks WHERE project_id = ? AND status IN ('open','in_progress')"
        " ORDER BY last_touched_at DESC LIMIT 3",
        (project_id,),
    ).fetchall()
    if aperti:
        righe.append("Aperti: " + " · ".join(t[0] for t in aperti))

    claimed = conn.execute(
        "SELECT title, claimed_at FROM tasks WHERE project_id = ? AND status = 'claimed'"
        " ORDER BY claimed_at LIMIT 2",
        (project_id,),
    ).fetchall()
    for titolo, quando in claimed:
        eta = _eta(conn, quando)
        # Il titolo si tronca QUI oltre che alla fonte: nel database restano i
        # titoli lunghi scritti prima che il limite esistesse, e due di quelli
        # saturano da soli il tetto del bigliettino. Un avviso che manda in
        # overflow il proprio contesto non e' un avviso.
        breve = " ".join(str(titolo or "").split())
        if len(breve) > _MAX_TITOLO:
            breve = breve[:_MAX_TITOLO] + "…"
        righe.append(f"⚠ Dichiarato fatto ma non verificato: \"{breve}\""
                     + (f" ({eta})" if eta else ""))
    if claimed:
        # Segnalare senza dire cosa fare lascia l'avviso senza sbocco: chi legge
        # sa che qualcosa non torna, e non sa come chiuderlo. Il comando e' uno
        # solo ed e' l'unica strada per il verde (vedi verify.py).
        righe.append("  Per chiuderli serve una prova eseguita:"
                     " tk verify <chiave> --cmd '<comando>'")

    # Solo sessioni con attivita' vera: vedi regola 2 nella docstring.
    ultima = conn.execute(
        "SELECT MAX(ts) FROM activity WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    if ultima:
        recenti = conn.execute(
            "SELECT target FROM activity WHERE project_id = ?"
            " AND tool IN ('Edit','Write','MultiEdit') AND target IS NOT NULL"
            " ORDER BY ts DESC LIMIT 40",
            (project_id,),
        ).fetchall()
        viste, ordinate = set(), []
        for (percorso,) in recenti:
            d = os.path.dirname(str(percorso))
            if d and d not in viste:
                viste.add(d)
                ordinate.append(d)
        quante = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE project_id = ?"
            " AND tool IN ('Edit','Write','MultiEdit')",
            (project_id,),
        ).fetchone()[0]
        riga = f"Ultima attività {_eta(conn, ultima)}"
        if quante:
            riga += f" · {quante} modifiche"
            if ordinate:
                riga += " in " + ", ".join(
                    os.path.basename(d) or d for d in ordinate[:_MAX_CARTELLE])
        righe.append(riga)

    richieste = conn.execute(
        "SELECT p.text FROM prompts p"
        " WHERE p.project_id = ?"
        "   AND EXISTS (SELECT 1 FROM activity a WHERE a.session_id = p.session_id)"
        " ORDER BY p.id DESC LIMIT ?",
        (project_id, _MAX_RICHIESTE),
    ).fetchall()
    for (testo,) in richieste:
        if _e_rumore(testo):
            continue
        pulito = " ".join((testo or "").split())
        if pulito:
            righe.append(f"Ultima richiesta: \"{pulito[:110]}\"")
            break

    if len(righe) == 1:
        return ""  # solo l'intestazione: niente da dire, meglio tacere
    return "\n".join(righe)[:MAX_CARATTERI]


def aggiorna_iniezioni(conn: sqlite3.Connection, project_ids) -> int:
    """Riscrive il bigliettino dei progetti indicati. Ritorna quanti file ha scritto.

    Scrive un file per ogni cartella di lavoro nota di quel progetto: l'hook
    conosce solo il proprio cwd, non il progetto, e non puo' scoprirlo senza git.
    """
    ensure_dirs()
    scritti = 0
    for project_id in {p for p in project_ids if p}:
        testo = costruisci_testo(conn, project_id)
        conn.execute(
            "INSERT INTO project_state (project_id,injection,updated_at) VALUES (?,?,?)"
            " ON CONFLICT(project_id) DO UPDATE SET"
            " injection=excluded.injection, updated_at=excluded.updated_at",
            (project_id, testo, utcnow()),
        )
        cartelle = {r[0] for r in conn.execute(
            "SELECT DISTINCT cwd FROM sessions WHERE project_id = ? AND cwd != ''",
            (project_id,))}
        radice = conn.execute(
            "SELECT root_path FROM projects WHERE id = ?", (project_id,)).fetchone()
        if radice and radice[0]:
            cartelle.add(radice[0])
        for cwd in cartelle:
            percorso = INJECTION_DIR / f"{chiave_cartella(cwd)}.txt"
            try:
                if testo:
                    tmp = percorso.with_suffix(".tmp")
                    tmp.write_text(testo, encoding="utf-8")
                    os.replace(tmp, percorso)
                    scritti += 1
                elif percorso.exists():
                    percorso.unlink()
            except OSError:
                pass
    return scritti
