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
