"""Entrypoint degli hook di Claude Code.

Regole non negoziabili:
  - esce SEMPRE 0, qualunque cosa accada (SPEC P1)
  - non scrive mai su stderr
  - non apre mai SQLite: solo append allo spool (niente lock nel percorso
    critico della sessione dell'utente)

Le forme dei payload qui sotto vengono da docs/findings-fase-1.md (31 eventi
reali, Claude Code v2.1.220), non da un'ipotesi a priori. Due punti misurati
cambiano cosa questo file puo' fare:

  1. `hook_event_name` e' nel payload: l'evento non va dedotto da argv[0].
     argv resta un fallback per quando manca.
  2. Il `tool_response` di Bash contiene esattamente stdout/stderr/
     interrupted/isImage/noOutputExpected. NON c'e' `exit_code' ne' nessun
     altro indicatore di esito: non va inventato. La fase 1 registra solo
     che un comando e' stato eseguito; la verifica esplicita (quale comando,
     con quale esito) arriva con un `tk verify` in una fase successiva.

TodoWrite non e' mai scattato nella cattura (0/31 eventi, due sessioni con
richieste esplicite di una todo list): la sua forma reale resta ignota.
Viene trattata in modo difensivo piu' sotto.
"""
import hashlib
import json
import os
import sys
import time

from .paths import (ERROR_LOG, INJECTION_DIR, MAX_CARATTERI, chiave_cartella,
                    is_disabled, utcnow)
from .spool import append_event

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# TodoWrite e' il nome storico; questa versione dell'harness usa Task*.
# Tenerli tutti: il costo e' nullo, il costo di sbagliare e' copertura zero.
_TODO_TOOLS = {"TodoWrite", "TaskCreate", "TaskUpdate"}
_STDERR_TRUNC = 400
_ERROR_MSG_TRUNC = 400


def _log_error(message: str) -> None:
    """Stessa disciplina append-and-never-raise usata da drain.py: un
    problema qui (disco pieno, permessi) non deve mai propagare, altrimenti
    il logging di un errore diventerebbe esso stesso una causa di rottura."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow()} {message}\n")
    except Exception:
        pass


_CHIAVI_LISTA = ("todos", "tasks", "items")
_CHIAVI_TESTO = ("content", "text", "title", "description", "prompt")


def _estrai_voce(grezzo) -> dict | None:
    """Riduce una voce di lista-cose-da-fare alla coppia (testo, stato).

    Accetta un dict con uno dei nomi noti per il testo. Ritorna None se non
    riesce a estrarre un testo: il chiamante lo trattera' come forma ignota.
    """
    if not isinstance(grezzo, dict):
        return None
    for chiave in _CHIAVI_TESTO:
        valore = grezzo.get(chiave)
        if isinstance(valore, str) and valore.strip():
            stato = grezzo.get("status") or grezzo.get("state") or "pending"
            return {"content": valore.strip(),
                    "status": stato if isinstance(stato, str) else "pending"}
    return None


def _handle_todowrite(tool_input: dict, base: dict, tool: str = "TodoWrite") -> None:
    """Specchia in modo difensivo gli strumenti che gestiscono cose da fare.

    In questa versione dell'harness `TodoWrite` NON esiste: si chiama
    TaskCreate/TaskUpdate/TaskList. Cercare un solo nome dava copertura zero.
    E il formato di TaskCreate non e' mai stato osservato, quindi non si assume:
    si accettano le forme ragionevoli (una lista sotto `todos`/`tasks`/`items`,
    oppure una singola voce direttamente in `tool_input`) e tutto cio' che non
    corrisponde finisce su ERROR_LOG con quello che e' arrivato davvero.
    Cosi' la prima occorrenza reale si scopre dal log invece che dal silenzio:
    questo progetto e' gia' stato morso otto volte da un fallimento silenzioso.
    """
    if not isinstance(tool_input, dict):
        _log_error(f"{tool}: tool_input non e' un oggetto: {type(tool_input).__name__}")
        return

    items, ignota = [], False

    lista = next((tool_input[k] for k in _CHIAVI_LISTA
                  if isinstance(tool_input.get(k), list)), None)
    if lista is not None:
        for grezzo in lista:
            voce = _estrai_voce(grezzo)
            if voce is None:
                ignota = True
            else:
                items.append(voce)
    else:
        # TaskCreate potrebbe passare una sola voce, non una lista.
        voce = _estrai_voce(tool_input)
        if voce is None:
            ignota = True
        else:
            items.append(voce)

    if ignota:
        try:
            dump = json.dumps(tool_input, ensure_ascii=False, default=str)[:_ERROR_MSG_TRUNC]
        except (TypeError, ValueError):
            dump = repr(tool_input)[:_ERROR_MSG_TRUNC]
        _log_error(f"{tool}: forma di tool_input non riconosciuta: {dump}")

    if items:
        append_event("todo", {**base, "items": items, "tool": tool})


_RINFRESCO_MINUTI = 30


def _da_iniettare(session_id: str, testo: str) -> bool:
    """True se questo bigliettino vale la pena di essere iniettato ORA.

    L'hook scatta a ogni messaggio, ma il bigliettino cambia solo quando cambia
    il lavoro. Riemetterlo identico a ogni turno lo accumula nel contesto:
    misurato sul campo, tre messaggi = tre copie dello stesso testo. Il
    bigliettino nasce per ripulire il contesto, non per riempirlo.

    Si riemette quando cambia, e comunque ogni RINFRESCO_MINUTI: se il contesto
    viene compattato, una sessione che chiacchiera senza toccare file resterebbe
    altrimenti senza bigliettino fino alla prossima modifica.

    In caso di qualunque problema si sceglie di iniettare: un bigliettino di
    troppo costa qualche token, uno mancante costa il senso della funzione.
    """
    if not session_id:
        return True
    try:
        impronta = hashlib.sha256(testo.encode("utf-8")).hexdigest()[:16]
        stato = INJECTION_DIR / ".ultimo" / str(session_id)
        adesso = time.time()
        if stato.is_file():
            righe = stato.read_text(encoding="utf-8").split()
            invariato = len(righe) == 2 and righe[0] == impronta
            recente = invariato and (adesso - float(righe[1])) < _RINFRESCO_MINUTI * 60
            if recente:
                return False
        stato.parent.mkdir(parents=True, exist_ok=True)
        stato.write_text(f"{impronta} {adesso:.0f}", encoding="utf-8")
    except (OSError, ValueError):
        return True
    return True


def _inietta(cwd: str, session_id: str = "") -> None:
    """Consegna a Claude il bigliettino gia' scritto per questa cartella.

    Legge un FILE, non il database: l'hook resta fuori da SQLite e non puo'
    contendere un lock ne' rallentare la sessione. Il file lo scrive il drainer.

    Il formato e' quello di Claude Code e SOLO quello. Claude Code legge sia
    `additional_context` sia `hookSpecificOutput` senza deduplicare, quindi
    emetterli entrambi "per sicurezza" iniettherebbe il testo due volte a ogni
    singolo messaggio (vedi docs/findings-fase-2.md).

    Se il file non c'e', e' vuoto o illeggibile non si emette nulla: un
    bigliettino che afferma il falso su cosa stavi facendo e' peggio di nessun
    bigliettino, perche' ti fa ripartire da un presupposto sbagliato.
    """
    try:
        percorso = INJECTION_DIR / f"{chiave_cartella(cwd)}.txt"
        testo = percorso.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return
    if not testo:
        return
    if not _da_iniettare(session_id, testo):
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": testo[:MAX_CARATTERI],
        }
    }, ensure_ascii=False))


_URL_SERVIZIO = "http://127.0.0.1:7777"
# Due tetti diversi, di proposito. `apri` scatta sull'evento Notification, che e'
# raro (una sessione che si ferma ad aspettare): un terzo di secondo li' e'
# invisibile. `chiudi` scatta su UserPromptSubmit, l'hook piu' caldo, a OGNI
# messaggio: li' il tetto dev'essere basso, perche' nel caso peggiore -- servizio
# che accetta la connessione ma tarda a rispondere -- questo tempo lo aspetta
# l'utente prima che il suo messaggio parta. Meglio una chiusura mancata che un
# messaggio trattenuto: il popup comunque scade da solo.
_TIMEOUT_APRI = 0.35
_TIMEOUT_CHIUDI = 0.12


def _segnala_attesa(comando: str, dati: dict) -> None:
    """Consegna al servizio una richiesta di attesa (o la sua chiusura).

    Eccezione a P3 dichiarata e circoscritta: qui l'hook fa rete. E' ammesso
    perche' il fine e' la tempestivita' (una sessione ferma ad aspettare va
    segnalata ORA, non fra venti secondi al prossimo travaso) e perche' e' reso
    inoffensivo: timeout stretto, fire-and-forget, tutto dentro un try che non
    lascia risalire niente. Se il servizio e' giu' il POST fallisce in silenzio
    e nessuna finestra compare -- giusto cosi': senza servizio non ci sarebbe
    comunque nulla da mostrare.
    """
    try:
        import urllib.request
        from .paths import token_locale
        corpo = json.dumps(dati).encode("utf-8")
        req = urllib.request.Request(
            f"{_URL_SERVIZIO}/api/attesa/{comando}", data=corpo, method="POST",
            headers={"Content-Type": "application/json",
                     "X-Myagents-Token": token_locale()})
        timeout = _TIMEOUT_CHIUDI if comando == "chiudi" else _TIMEOUT_APRI
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except Exception:
        pass  # mai propagare: un popup mancato non vale una sessione rotta


def _handle(event_name: str, data: dict) -> None:
    base = {
        "session_id": data.get("session_id"),
        "cwd": data.get("cwd") or os.getcwd(),
        # Ogni evento porta il percorso del transcript, e finora veniva buttato
        # via. E' l'unico posto dove finiscono le richieste scritte MENTRE
        # l'agente lavora: quelle non generano un UserPromptSubmit e quindi
        # l'hook non le vede (misurato: 1 catturata su 6). Qui si annota solo il
        # percorso -- una stringa -- e il travaso legge il file per conto suo,
        # fuori dal percorso critico. Vedi transcript.py.
        "transcript_path": data.get("transcript_path") or "",
    }

    if event_name == "SessionStart":
        append_event("session_start", {
            **base,
            # CLAUDE_CONFIG_DIR e' impostata SOLO dagli alias (claude-lavoro,
            # claude-cliente). Col comando `claude` normale non esiste, e
            # registrare "" rendeva impossibile capire da quale alias arrivava
            # il lavoro: la dashboard mostrava alias=[] per tutto.
            "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")
                          or os.path.expanduser("~/.claude"),
            # Misurato sul campo: 7 sessioni su 8 nel primo test reale erano
            # invocazioni headless di un ALTRO strumento, indistinguibili dalle
            # sessioni vere. `source` e' il solo dato che Claude Code offre per
            # distinguerle: lo registriamo e decideremo la regola sui dati.
            "source": data.get("source") or ""})
        return

    if event_name == "UserPromptSubmit":
        append_event("prompt", {**base, "text": data.get("prompt") or ""})
        _inietta(base["cwd"], base.get("session_id") or "")
        # Hai scritto nel terminale: se questa sessione aveva una richiesta di
        # attesa in sospeso, ora l'hai risolta li'. Il popup va tolto subito,
        # altrimenti resta a mostrare qualcosa a cui hai gia' risposto -- il modo
        # piu' rapido di fargli perdere fiducia. Il terminale e' la verita'.
        _segnala_attesa("chiudi", {"session_id": base.get("session_id") or ""})
        return

    if event_name == "SessionEnd":
        append_event("session_end", base)
        # Alla fine della sessione le sue richieste di attesa non hanno piu'
        # senso: si dice al servizio di chiuderle, cosi' il popup non resta su
        # per una sessione che non c'e' piu'.
        _segnala_attesa("chiudi", {"session_id": base.get("session_id") or ""})
        return

    if event_name == "Notification":
        # MISURATO (sonda): l'evento porta notification_type/message/session_id/
        # cwd. E' il segnale che una sessione si e' fermata ad aspettare
        # l'utente. Si consegna al servizio, che decide se e come mostrarlo. Qui
        # l'hook non decide niente e non blocca: fire-and-forget con timeout
        # cortissimo, e mai un'eccezione che risalga (romperebbe la sessione).
        _segnala_attesa("apri", {
            "notification_type": data.get("notification_type") or "",
            "message": data.get("message") or "",
            "session_id": base.get("session_id") or "",
            "cwd": base.get("cwd") or "",
            "prompt_id": data.get("prompt_id") or "",
            "tool_use_id": data.get("tool_use_id") or "",
        })
        return

    if event_name != "PostToolUse":
        return

    tool = data.get("tool_name") or ""
    tool_input = data.get("tool_input")
    tool_response = data.get("tool_response")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not isinstance(tool_response, dict):
        tool_response = {}

    if tool in _EDIT_TOOLS:
        append_event("file_edit", {
            **base, "tool": tool,
            "path": tool_input.get("file_path") or tool_input.get("notebook_path")})
    elif tool == "Bash":
        # MISURATO (docs/findings-fase-1.md): non c'e' exit_code in
        # tool_response. Si registra solo cio' che e' stato osservato:
        # comando, stderr (troncato) e se e' stato interrotto.
        append_event("command", {
            **base, "tool": tool,
            "command": tool_input.get("command") or "",
            "stderr": (tool_response.get("stderr") or "")[:_STDERR_TRUNC],
            "interrupted": bool(tool_response.get("interrupted"))})
    elif tool in _TODO_TOOLS:
        # `TodoWrite` non esiste piu' in questa versione dell'harness: si chiama
        # TaskCreate/TaskUpdate/TaskList. Cercando solo "TodoWrite" il mirror
        # aveva copertura ZERO, non parziale (verificato sul campo: 0 task
        # catturati da una sessione che ne aveva creati tre). Accettiamo tutti i
        # nomi noti; la forma del payload di TaskCreate non e' ancora stata
        # osservata, quindi _handle_todowrite la registra su ERROR_LOG se non la
        # riconosce, invece di scartarla in silenzio.
        _handle_todowrite(tool_input, base, tool)


def main(argv: list | None = None, stdin=None) -> int:
    try:
        if is_disabled():
            return 0
        args = sys.argv[1:] if argv is None else argv
        stream = sys.stdin if stdin is None else stdin
        raw = stream.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return 0
        # MISURATO: hook_event_name e' nel payload e va preferito ad argv[0],
        # che resta solo un fallback per quando manca.
        event_name = data.get("hook_event_name") or (args[0] if args else None)
        if not event_name:
            return 0
        _handle(event_name, data)
    except BaseException as exc:  # mai propagare: romperebbe la sessione (SPEC P1)
        _log_error(f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
