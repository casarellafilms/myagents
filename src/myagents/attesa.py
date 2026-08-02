"""La coda delle richieste di attesa: chi aspetta una tua risposta, e quando
mostrarglielo.

E' il cervello del popup, separato di proposito dalla finestra. Qui non c'e'
niente di macOS, niente PyObjC, niente che disegni sullo schermo: solo la logica
di quali richieste esistono, quali vanno mostrate ORA e quali no. Cosi' si prova
con un test invece che a occhio su una finestra, e il giorno che la finestra
cambia tecnologia questa parte non si tocca.

COSA E' UNA RICHIESTA DI ATTESA. Un agente, in un terminale, si ferma e aspetta
qualcosa da te: una domanda con delle opzioni, un permesso da concedere, un piano
da approvare. Ognuna ha un tipo, la sessione da cui viene, e un identificatore
stabile.

TRE REGOLE, dalla specifica, che vincono su tutto:

1. **Il terminale e' la verita'.** Questa coda e' un promemoria di cosa sta
   aspettando, non la fonte. Se una richiesta si e' chiusa nel terminale (hai
   risposto li'), qui va tolta, non tenuta viva.
2. **Nel dubbio non si mostra.** Un popup che appare quando non serve si impara
   a chiudere, e un popup che si impara a chiudere e' spento in due giorni. Ogni
   cancello che dice "non mostrare" vince su ogni ragione per mostrare.
3. **L'identita' e' una chiave vera, mai un'euristica.** Due richieste diverse
   non devono mai collassare nella stessa, o rispondere a una risponderebbe
   all'altra.

Cosa qui NON si decide: come la risposta arriva alla sessione. Quello e' un
canale esplicito a parte (l'hook che la sessione sta gia' aspettando), e non
passa da una finestra che finge la tua tastiera.
"""
import hashlib
import threading
import time

# I tipi di richiesta, dal piu' al meno urgente. L'ordine conta: se due
# arrivano insieme, si mostra prima quella che blocca di piu'.
TIPI = ("permesso", "domanda", "piano", "elicitation", "inattivo")

# Dopo quanti secondi una richiesta senza piu' segnali si considera scaduta e
# sparisce da sola. Un agente che aspetta lo fa a lungo, ma non per sempre: se
# la sessione e' morta, la richiesta non deve restare appesa in eterno.
SCADENZA = 1800


def chiave(tipo: str, session_id: str, dettaglio: str) -> str:
    """L'identita' stabile di una richiesta.

    `dettaglio` e', in ordine di preferenza: il tool_use_id, l'id
    dell'elicitation, l'id dell'evento, o in ultima istanza l'istante al secondo.
    NON si usa mai un timestamp di stato che potrebbe restare fermo (misurato:
    uno e' rimasto immobile 52 minuti su una sessione viva): due richieste
    diverse condividerebbero la chiave e la seconda verrebbe scartata come "gia'
    vista", perdendola.
    """
    grezzo = f"{tipo}|{session_id}|{dettaglio}"
    return hashlib.sha256(grezzo.encode("utf-8")).hexdigest()[:32]


# Come il campo `notification_type` dell'evento Notification (MISURATO sul campo:
# vedi ~/.myagents/sonde/eventi.jsonl) diventa un tipo della coda. I nomi a
# destra sono quelli di TIPI, ordinati per urgenza. Un tipo non in mappa e' un
# tipo nuovo dell'harness: si tratta come "domanda" e si lascia traccia, invece
# di ignorarlo in silenzio -- questo progetto e' gia' stato morso dal silenzio.
_DA_NOTIFICATION = {
    "permission_prompt": "permesso",
    "agent_needs_input": "domanda",
    "elicitation_dialog": "elicitation",
    "idle_prompt": "inattivo",
}


def da_notification(payload: dict) -> dict | None:
    """Da un evento Notification grezzo agli argomenti di Coda.apri().

    Lettura difensiva assoluta: un campo mancante vale "non lo so", mai
    un'eccezione. Ritorna None se non c'e' abbastanza per farne una richiesta
    (nessun session_id): senza sessione non si saprebbe a chi appartiene, ne'
    quale terminale portare davanti.

    MISURATO (sonda del 2026-08-02): l'evento porta
      notification_type, message, session_id, cwd, transcript_path, prompt_id.
    Il primo tipo visto e' `idle_prompt` con message "Claude is waiting for your
    input". Gli altri tipi sono attesi ma non ancora osservati: la mappa li
    prevede, e cio' che non riconosce diventa una domanda tracciata.
    """
    if not isinstance(payload, dict):
        return None
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return None
    grezzo = str(payload.get("notification_type") or "").strip()
    tipo = _DA_NOTIFICATION.get(grezzo, "domanda")
    # dettaglio: la cosa piu' stabile che identifica QUESTA richiesta. Mai un
    # timestamp di stato (misurato: puo' restare fermo).
    dettaglio = (str(payload.get("tool_use_id") or "").strip()
                 or str(payload.get("prompt_id") or "").strip()
                 or grezzo or "attesa")
    return {
        "tipo": tipo, "session_id": session_id, "dettaglio": dettaglio,
        "testo": str(payload.get("message") or "").strip(),
        "cwd": str(payload.get("cwd") or "").strip(),
        "tipo_grezzo": grezzo,
    }


# I tool le cui richieste di permesso valgono la pena di un popup: sono quelli
# in cui l'agente sta chiedendo QUALCOSA all'utente, non eseguendo un comando.
# Gli altri PermissionRequest (Bash, Edit, ...) sono rumore per il popup.
_TOOL_CHE_CHIEDONO = {"AskUserQuestion": "domanda", "ExitPlanMode": "piano"}


def da_permission(payload: dict) -> dict | None:
    """Da un evento PermissionRequest agli argomenti di Coda.apri(), con le
    opzioni della domanda.

    MISURATO (sonda): PermissionRequest porta tool_name e tool_input. Per
    AskUserQuestion, tool_input.questions e' [{question, header, options:
    [{label, description}], multiSelect}]. Sono la domanda e i bottoni, gia'
    pronti da mostrare -- l'unica cosa che serve per far vedere all'utente COSA
    gli si chiede senza che cambi finestra.

    Ritorna None per i tool che non chiedono niente all'utente (un Bash, un
    Edit): quelli non meritano un popup.
    """
    if not isinstance(payload, dict):
        return None
    tool = str(payload.get("tool_name") or "")
    tipo = _TOOL_CHE_CHIEDONO.get(tool)
    if not tipo:
        return None
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return None
    ti = payload.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    domande = ti.get("questions") if isinstance(ti.get("questions"), list) else []

    testo, opzioni = "", []
    if domande and isinstance(domande[0], dict):
        prima = domande[0]
        testo = str(prima.get("question") or "").strip()
        for o in (prima.get("options") or []):
            if isinstance(o, dict) and o.get("label"):
                opzioni.append(str(o["label"]))
            elif isinstance(o, str):
                opzioni.append(o)
    return {
        "tipo": tipo, "session_id": session_id,
        "dettaglio": str(payload.get("tool_use_id") or payload.get("prompt_id")
                         or tool),
        "testo": testo, "opzioni": opzioni[:6],
        "cwd": str(payload.get("cwd") or "").strip(),
    }


class Coda:
    """Le richieste di attesa vive, con la regola di cosa mostrare.

    Vive nel servizio, in memoria: se il servizio muore, muore anche la coda, e
    va bene -- una richiesta di attesa e' vera solo finche' la sessione e' viva,
    e la sessione e' viva solo finche' il servizio la vede.
    """

    def __init__(self):
        self._richieste: dict = {}      # chiave -> dict
        self._silenzi: dict = {}        # ambito -> scade_a (epoch)
        self._lock = threading.Lock()

    # -- ingresso e uscita ---------------------------------------------------

    def apri(self, tipo: str, session_id: str, dettaglio: str,
             testo: str = "", opzioni=None, cwd: str = "", tty: str = "",
             adesso: float | None = None) -> str:
        """Registra una richiesta, o ne aggiorna una gia' presente.

        Ritorna la chiave. Riaprire la stessa richiesta (stessa chiave) non la
        duplica: ne rinfresca solo l'istante, cosi' non scade mentre e' viva.
        """
        adesso = time.time() if adesso is None else adesso
        if tipo not in TIPI:
            tipo = "domanda"
        k = chiave(tipo, session_id or "", dettaglio or "")
        with self._lock:
            esistente = self._richieste.get(k)
            self._richieste[k] = {
                "chiave": k, "tipo": tipo, "session_id": session_id or "",
                "testo": (testo or "").strip()[:400],
                "opzioni": list(opzioni or []),
                "cwd": cwd, "tty": tty,
                "aperta_a": esistente["aperta_a"] if esistente else adesso,
                "vista_a": adesso,
            }
        return k

    def chiudi(self, chiave_o_session) -> int:
        """Toglie una richiesta per chiave, o tutte quelle di una sessione.

        E' la gamba piu' importante: quando rispondi nel terminale, la richiesta
        va tolta subito. Tenerla mostrerebbe un popup per qualcosa a cui hai gia'
        risposto -- il modo piu' rapido di far perdere fiducia allo strumento.
        """
        with self._lock:
            if chiave_o_session in self._richieste:
                del self._richieste[chiave_o_session]
                return 1
            tolte = [k for k, r in self._richieste.items()
                     if r["session_id"] == chiave_o_session]
            for k in tolte:
                del self._richieste[k]
            return len(tolte)

    def _sfronda(self, adesso: float) -> None:
        """Toglie richieste scadute e silenzi finiti. Da chiamare col lock preso."""
        morte = [k for k, r in self._richieste.items()
                 if adesso - r["vista_a"] > SCADENZA]
        for k in morte:
            del self._richieste[k]
        finiti = [a for a, fino in self._silenzi.items() if fino <= adesso]
        for a in finiti:
            del self._silenzi[a]

    # -- silenziamento -------------------------------------------------------

    def silenzia(self, ambito: str, secondi: float, adesso: float | None = None) -> None:
        """Non mostrare piu' niente per un certo ambito, per un po'.

        `ambito` e' "tutto", oppure "progetto:<cwd>", "sessione:<id>",
        "tipo:<tipo>". Cosi' puoi zittire una sessione rumorosa senza perderti
        le richieste delle altre.
        """
        adesso = time.time() if adesso is None else adesso
        self._silenzi[ambito] = adesso + max(0.0, secondi)

    def _silenziata(self, r: dict, adesso: float) -> bool:
        ambiti = ["tutto", f"tipo:{r['tipo']}", f"sessione:{r['session_id']}"]
        if r.get("cwd"):
            ambiti.append(f"progetto:{r['cwd']}")
        return any(self._silenzi.get(a, 0) > adesso for a in ambiti)

    # -- cosa mostrare -------------------------------------------------------

    def da_mostrare(self, disabilitato: bool = False, in_primo_piano: str = "",
                    adesso: float | None = None) -> list:
        """Le richieste che vanno mostrate ORA, la piu' urgente per prima.

        I cancelli, in ordine, il primo "no" vince (regola 2):
          1. la cattura e' spenta (kill-switch globale) -> niente
          2. la richiesta e' silenziata per il suo ambito -> saltala
          3. il terminale di quella sessione e' gia' davanti a te -> saltala:
             la stai gia' guardando, un popup sopra sarebbe rumore

        `in_primo_piano` e' il tty del terminale attualmente in focus, se e' un
        terminale; vuoto altrimenti. La richiesta la cui sessione e' proprio
        quella davanti non si mostra.
        """
        if disabilitato:
            return []
        adesso = time.time() if adesso is None else adesso
        with self._lock:
            self._sfronda(adesso)
            fuori = []
            for r in self._richieste.values():
                if self._silenziata(r, adesso):
                    continue
                if in_primo_piano and r.get("tty") == in_primo_piano:
                    continue
                fuori.append(dict(r))
        fuori.sort(key=lambda r: (TIPI.index(r["tipo"]) if r["tipo"] in TIPI
                                  else len(TIPI), r["aperta_a"]))
        return fuori

    def tutte(self) -> list:
        """Tutte le richieste vive, mostrabili o no. Per diagnostica e test."""
        with self._lock:
            return [dict(r) for r in self._richieste.values()]
