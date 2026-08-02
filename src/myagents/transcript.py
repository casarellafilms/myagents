"""Recupero delle richieste che gli hook non vedono.

IL BUCO, misurato sul campo il 2026-08-02.

L'hook `UserPromptSubmit` scatta quando mandi un messaggio e l'agente e' fermo.
Non scatta quando lo mandi mentre l'agente sta gia' lavorando: quel messaggio
viene messo in coda dall'harness e consegnato dopo, senza passare dall'hook.

Il risultato e' che myagents perdeva **esattamente** le richieste piu' a rischio
di essere dimenticate. Quando l'agente lavora dieci minuti e nel frattempo gli
scrivi altre quattro cose, quelle quattro sono le prime a sparire: sono arrivate
mentre nessuno le annotava. In una sessione reale il conto e' stato 1 richiesta
catturata su 6.

Non e' un caso limite: e' il caso centrale del prodotto. Questo progetto esiste
per non perdere le cose da fare, e le perdeva proprio quando se ne accumulano.

LA CHIUSURA.

Il transcript su disco, che ogni hook riceve gia' come `transcript_path`,
contiene tutto. Le richieste accodate ci stanno con un tipo tutto loro:

    {"type":"queue-operation","operation":"enqueue",
     "content":"il testo che hai scritto",
     "timestamp":"2026-08-02T01:21:59.000Z","sessionId":"..."}

`enqueue` quando la scrivi, `remove` quando viene consumata. Si leggono le
`enqueue`: sono le richieste vere. Le `remove` sono la stessa richiesta un
istante dopo, e prenderle entrambe significherebbe duplicare tutto.

DOVE GIRA, E PERCHE' NON NELL'HOOK.

Leggere un transcript significa scorrere un file che a fine giornata arriva a
migliaia di righe. Nel percorso critico di una sessione non ci va (SPEC P1, P3):
l'hook si limita ad annotare DOVE sta il transcript, e il travaso -- che gira nel
servizio, dove nessuno lo sta aspettando -- lo legge con calma.
"""
import json
import os

_TIPO_CODA = "queue-operation"
_ENQUEUE = "enqueue"
# Un transcript di una giornata lunga sta sotto le poche decine di MB. Oltre
# questa soglia si rinuncia: meglio nessun recupero che un servizio che si
# pianta a leggere un file patologico.
MAX_BYTE = 64 * 1024 * 1024
MAX_TESTO = 4000


def _testo_utente(messaggio) -> str:
    """Il testo scritto dall'utente dentro un messaggio del transcript.

    Il contenuto e' una stringa oppure una lista di blocchi tipizzati; di quella
    lista servono solo i blocchi 'text'. Gli altri sono risultati di strumenti,
    cioe' macchinario, non richieste.
    """
    contenuto = (messaggio or {}).get("content")
    if isinstance(contenuto, str):
        return contenuto
    if isinstance(contenuto, list):
        pezzi = [b.get("text", "") for b in contenuto
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in pezzi if p)
    return ""


def _normalizza_ts(grezzo) -> str:
    """Da 2026-08-02T01:21:59.000Z al formato del database, senza millisecondi."""
    testo = str(grezzo or "").strip()
    if not testo:
        return ""
    if "." in testo:
        testo = testo.split(".", 1)[0] + "Z"
    return testo if testo.endswith("Z") else testo + "Z"


def richieste(percorso: str) -> list:
    """Le richieste dell'utente presenti in un transcript, in ordine.

    Ogni voce: {"testo": str, "ts": str, "accodata": bool}. `accodata` distingue
    cio' che e' stato scritto mentre l'agente lavorava -- l'unica cosa che gli
    hook non vedono -- da cio' che era gia' stato catturato per la sua strada.

    Non solleva mai: un transcript illeggibile o di formato ignoto vale zero
    richieste recuperate, non un travaso interrotto.
    """
    if not percorso:
        return []
    try:
        if not os.path.isfile(percorso) or os.path.getsize(percorso) > MAX_BYTE:
            return []
    except OSError:
        return []

    fuori, visti = [], set()
    try:
        with open(percorso, encoding="utf-8", errors="replace") as fh:
            for riga in fh:
                if '"type"' not in riga:
                    continue
                try:
                    dato = json.loads(riga)
                except ValueError:
                    continue  # una riga rotta non invalida le altre
                if not isinstance(dato, dict):
                    continue

                tipo = dato.get("type")
                if tipo == _TIPO_CODA:
                    # Solo le enqueue: la remove e' la stessa richiesta un
                    # istante dopo, e prenderle entrambe raddoppierebbe tutto.
                    if dato.get("operation") != _ENQUEUE:
                        continue
                    testo, accodata = str(dato.get("content") or ""), True
                elif tipo == "user" and not dato.get("isSidechain"):
                    testo, accodata = _testo_utente(dato.get("message")), False
                else:
                    continue

                testo = testo.strip()
                if not testo:
                    continue
                chiave = (testo[:400], _normalizza_ts(dato.get("timestamp")))
                if chiave in visti:
                    continue
                visti.add(chiave)
                fuori.append({"testo": testo[:MAX_TESTO],
                              "ts": _normalizza_ts(dato.get("timestamp")),
                              "accodata": accodata})
    except OSError:
        return fuori
    return fuori


def risposte(percorso: str, massimo: int = 14) -> list:
    """Cosa l'agente ha DETTO di aver fatto, in coda alla sessione.

    Chiude il rischio R3 della specifica: il revisore vedeva le richieste e i
    file toccati, mai le risposte. Senza di esse non puo' distinguere "l'utente
    ha chiesto X" da "X e' stato fatto", e quindi crea un task aperto per ogni
    richiesta -- comprese quelle gia' evase dieci minuti dopo. Misurato: in una
    sessione sola ha aperto quattro task per lavori conclusi nella sessione
    stessa. Un elenco di cose da fare che contiene cose fatte si smette di
    guardare, ed e' il modo piu' rapido di rendere inutile tutto il resto.

    Si prendono le ULTIME risposte, non le prime: le conclusioni stanno in
    fondo. E solo il testo, non le chiamate agli strumenti, che sono rumore.

    Attenzione a cosa NON e': questo resta cio' che l'agente DICHIARA. Non e'
    una prova, e chi lo usa non deve trattarlo come tale -- serve a far passare
    un task da "da fare" a "da controllare", mai a "verificato".
    """
    if not percorso:
        return []
    try:
        if not os.path.isfile(percorso) or os.path.getsize(percorso) > MAX_BYTE:
            return []
    except OSError:
        return []

    fuori: list = []
    try:
        with open(percorso, encoding="utf-8", errors="replace") as fh:
            for riga in fh:
                if '"assistant"' not in riga:
                    continue
                try:
                    dato = json.loads(riga)
                except ValueError:
                    continue
                if (not isinstance(dato, dict) or dato.get("type") != "assistant"
                        or dato.get("isSidechain")):
                    continue
                contenuto = (dato.get("message") or {}).get("content")
                if not isinstance(contenuto, list):
                    continue
                testo = " ".join(
                    b.get("text", "") for b in contenuto
                    if isinstance(b, dict) and b.get("type") == "text")
                testo = " ".join(testo.split())
                if len(testo) < 40:
                    continue  # frasi di servizio: non dicono cosa e' stato fatto
                fuori.append(testo[:600])
    except OSError:
        pass
    return fuori[-massimo:]


def accodate(percorso: str) -> list:
    """Solo le richieste che gli hook NON possono aver visto.

    E' quello che serve al recupero: le altre sono gia' nel database, e
    reinserirle vorrebbe dire duplicarle.
    """
    return [r for r in richieste(percorso) if r["accodata"]]
