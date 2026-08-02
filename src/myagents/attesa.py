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
                "mostrata": esistente["mostrata"] if esistente else False,
            }
        return k

    def chiudi(self, chiave_o_session, adesso: float | None = None) -> int:
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

    def segna_mostrata(self, chiave_: str, adesso: float | None = None) -> None:
        adesso = time.time() if adesso is None else adesso
        with self._lock:
            if chiave_ in self._richieste:
                self._richieste[chiave_]["mostrata"] = True

    def tutte(self) -> list:
        """Tutte le richieste vive, mostrabili o no. Per diagnostica e barra."""
        with self._lock:
            return [dict(r) for r in self._richieste.values()]
