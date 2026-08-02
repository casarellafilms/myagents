"""Percorsi e interruttori. Nessuna logica applicativa, nessun import pesante."""
import hashlib
import os
import time
from pathlib import Path

_FALSEY = {"", "0", "false", "no", "off"}

ROOT = Path(os.environ.get("MYAGENTS_HOME") or (Path.home() / ".myagents"))
DB_PATH = ROOT / "tasks.db"
SPOOL_DIR = ROOT / "spool"
ERROR_LOG = ROOT / "hook-errors.log"
OVERRIDES = ROOT / "overrides.json"
# Un file di testo gia' renderizzato per ogni cartella di lavoro conosciuta.
# L'hook piu' caldo legge da qui invece che dal database: resta fuori da SQLite,
# quindi non puo' mai contendere un lock ne' rallentare una sessione.
INJECTION_DIR = ROOT / "injection"


# File-sentinella: la sua sola esistenza spegne la cattura. Serve perche' una
# variabile d'ambiente si puo' impostare solo per i processi che avvii DOPO,
# mentre la barra deve poter spegnere subito anche le sessioni gia' aperte.
SPENTO = ROOT / "SPENTO"


def is_disabled() -> bool:
    """True se la cattura e' spenta, da variabile d'ambiente o da file.

    Un solo `stat` in piu' nel percorso critico (~0.01ms): il prezzo di poter
    spegnere tutto da un menu invece che riavviando le sessioni.
    """
    if os.environ.get("MYAGENTS_OFF", "").strip().lower() not in _FALSEY:
        return True
    try:
        return SPENTO.exists()
    except OSError:
        return False


def popup_attivo() -> bool:
    """True se il popup (il Ciottolo) puo' comparire. Spento di default.

    Opt-in di proposito. La funzione e' giovane e il segnale che la alimenta
    (l'hook Notification) va registrato con `tk install`: finche' non l'hai
    acceso di tua volonta', il servizio non lancia mai la finestra, e nessun
    flash puo' comparire. Si accende con MYAGENTS_POPUP=1, oppure -- piu' comodo
    -- creando il file ~/.myagents/POPUP (cosi' vale anche per il servizio gia'
    avviato, senza doverlo riavviare con la variabile).
    """
    if os.environ.get("MYAGENTS_POPUP", "").strip().lower() not in _FALSEY:
        return True
    try:
        return (ROOT / "POPUP").exists()
    except OSError:
        return False


def ensure_dirs() -> None:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    INJECTION_DIR.mkdir(parents=True, exist_ok=True)


# Tetto di sicurezza del bigliettino, non obiettivo: in pratica sta sotto i 400.
MAX_CARATTERI = 1800


def chiave_cartella(cwd: str) -> str:
    """Nome del file di bigliettino per una cartella di lavoro.

    Vive qui, non nel renderer, perche' la usano DUE processi: il drainer per
    scrivere il file e l'hook per leggerlo. Se le due parti calcolassero la
    chiave in modo anche solo leggermente diverso, il file verrebbe scritto con
    un nome e cercato con un altro, e il sintomo sarebbe silenzio totale --
    nessun errore, nessuna iniezione, nessun indizio.

    Sta in paths.py e non in render.py perche' l'hook non deve importare il
    renderer: quello tira dentro sqlite3, e il percorso critico resta fuori dal
    database.
    """
    normalizzato = os.path.realpath(os.path.expanduser(cwd or ""))
    return hashlib.sha256(normalizzato.encode("utf-8")).hexdigest()[:32]


def utcnow() -> str:
    """ISO-8601 UTC, es. 2026-08-01T18:42:03Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Segreto locale per le azioni che consegnano input a una sessione. Serve perche'
# ascoltare su 127.0.0.1 e validare l'origine ferma il browser, ma NON ferma un
# altro processo locale qualunque: chiunque giri sulla macchina puo' fare un POST
# a 127.0.0.1:7777. Per le azioni innocue (leggere lo stato, archiviare un task)
# basta l'origine; per quelle che parlano a una sessione dell'utente serve
# conoscere questo file, che ha permessi 0600 e lo leggono solo i processi di
# myagents. La dashboard nel browser NON lo ha, e quindi non puo' rispondere a
# una richiesta di attesa: di proposito.
TOKEN_FILE = ROOT / "token"


def token_locale() -> str:
    """Il segreto locale, creandolo la prima volta. "" se non e' ottenibile.

    Creato con O_CREAT|O_EXCL cosi' due processi che partono insieme non se lo
    sovrascrivono a vicenda: chi perde la corsa rilegge quello che ha scritto
    l'altro. Un fallimento qui non deve mai propagare -- al massimo le azioni
    protette restano indisponibili, il che e' sicuro.
    """
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        import secrets
        ROOT.mkdir(parents=True, exist_ok=True)
        segreto = secrets.token_hex(16)
        fd = os.open(str(TOKEN_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, segreto.encode("utf-8"))
        finally:
            os.close(fd)
        return segreto
    except FileExistsError:
        try:
            return TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    except OSError:
        return ""
