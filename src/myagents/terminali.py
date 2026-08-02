"""I terminali aperti, e come tornarci.

Perche' esiste. La barra aveva gia' un "Apri il terminale qui", ma faceva
`open -a Terminal <cartella>`: apriva **una finestra nuova**. Con sei progetti
aperti significa ritrovarsi con sei finestre in piu' accanto alle sei che avevi
gia', e la sessione dell'agente che stava girando in una di quelle resta
sepolta. Quello che serve e' il contrario: accorgersi che una finestra su quel
progetto c'e' gia', e riportarla davanti.

Come funziona, e perche' cosi'. Tutto quello che segue e' stato misurato su
questa macchina, non dedotto:

  1. `ps` elenca i processi con il loro tty. Un tty (`ttys001`) identifica un
     pannello di terminale, ed e' l'unica cosa che i processi e l'applicazione
     terminale hanno in comune.
  2. `lsof -d cwd` da' la cartella di lavoro di un processo. Una sola chiamata
     per tutti i pid insieme: una chiamata per processo, con venti terminali
     aperti, costa secondi interi.
  3. Terminal.app espone `tty of tab`, iTerm2 espone `tty of session`. Da li' si
     risale alla finestra e la si porta davanti.

Trappola AppleScript, gia' pagata due volte: `repeat with t in tabs of w` produce
riferimenti che poi non si riescono a usare (errore -1700, "Can't make ... into
type Unicode text"). Finestre e tab vanno indicizzate esplicitamente, come piu'
sotto.

Seconda trappola: `tell application "iTerm2"` **avvia iTerm2** se non e' in
esecuzione. Per questo si chiede prima a `ps` quali terminali stanno davvero
girando, e si interrogano solo quelli: nessuno vuole che una dashboard apra
applicazioni per conto suo.

Costo e collocazione. Questo modulo non e' mai nel percorso critico di una
sessione: lo chiama il servizio locale, che sta gia' fuori. La cache di pochi
secondi c'e' perche' la barra chiede lo stato ogni dieci secondi, e non ha senso
rifare `lsof` e due `osascript` a ogni giro.

Permessi. La prima volta che si porta davanti una finestra, macOS chiede il
permesso di Automazione per Terminal o iTerm. Se lo neghi, `porta_in_primo_piano`
ritorna False e chi chiama ricade sull'aprire una finestra nuova: la funzione
degrada, non si rompe.
"""
import os
import subprocess
import time

from .paths import ERROR_LOG, utcnow

# Le shell che reggono un pannello di terminale, e il processo che ci interessa
# davvero trovare li' dentro.
_SHELL = {"zsh", "bash", "fish", "sh", "-zsh", "-bash", "-fish", "-sh"}
_AGENTE = "claude"

# Frammento del percorso del processo -> nome con cui AppleScript conosce l'app.
_APP_NOTE = {"Terminal.app/Contents/MacOS/": "Terminal",
             "iTerm.app/Contents/MacOS/": "iTerm2"}

_TIMEOUT = 5
_CACHE_SECONDI = 3
_cache: tuple = (0.0, [])


def _log(msg: str) -> None:
    """Append-and-never-raise, come ovunque nel progetto: un problema nel
    registrare un errore non deve diventare esso stesso una causa di rottura."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow()} terminali: {msg}\n")
    except Exception:
        pass


def _esegui(comando: list) -> str:
    """Esegue un comando e restituisce stdout. Stringa vuota su qualunque
    problema: qui niente e' indispensabile, e un terminale non trovato e' un
    disservizio minore, mentre un'eccezione fermerebbe il servizio."""
    try:
        esito = subprocess.run(comando, capture_output=True, text=True,
                               timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"{comando[0]}: {type(exc).__name__}: {exc}")
        return ""
    return esito.stdout or ""


def _processi() -> list:
    """(pid, tty, comando) dei processi attaccati a un tty vero.

    I processi senza terminale hanno tty '??' e qui non servono: non c'e'
    nessuna finestra a cui riportarli.
    """
    fuori = []
    for riga in _esegui(["ps", "-Ao", "pid=,tty=,comm="]).splitlines():
        pezzi = riga.split(None, 2)
        if len(pezzi) < 3:
            continue
        pid, tty, comando = pezzi
        if tty in ("??", "-") or not pid.isdigit():
            continue
        fuori.append((int(pid), tty, os.path.basename(comando.strip())))
    return fuori


def _cwd_di(pids: list) -> dict:
    """Cartella di lavoro di piu' processi, in UNA sola chiamata a lsof.

    Il formato -Fpn emette righe 'p<pid>' e 'n<percorso>' alternate.
    """
    if not pids:
        return {}
    grezzo = _esegui(["lsof", "-a", "-d", "cwd", "-Fpn",
                      "-p", ",".join(str(p) for p in pids)])
    fuori, corrente = {}, None
    for riga in grezzo.splitlines():
        if riga.startswith("p") and riga[1:].isdigit():
            corrente = int(riga[1:])
        elif riga.startswith("n") and corrente is not None:
            fuori[corrente] = riga[1:]
            corrente = None
    return fuori


def _app_in_esecuzione() -> list:
    """I terminali davvero in esecuzione, col nome che vuole AppleScript.

    Si chiede a `ps` e non ad AppleScript perche' `tell application "iTerm2"`
    AVVIA iTerm2 se non gira: interrogare solo chi c'e' gia' evita di aprire
    applicazioni al posto dell'utente.
    """
    elenco = _esegui(["ps", "-Ao", "comm="])
    return [nome for frammento, nome in _APP_NOTE.items() if frammento in elenco]


_TTY_TERMINAL = 'tell application "Terminal" to get tty of every tab of every window'
_TTY_ITERM = ('tell application "iTerm2" to get tty of every session'
              ' of every tab of every window')


def _tty_per_app() -> dict:
    """Mappa tty -> applicazione che lo possiede.

    Serve perche' per portare davanti una finestra bisogna parlare
    all'applicazione giusta. Senza questa mappa si dovrebbero provare tutte a
    ogni clic, e ogni tentativo a vuoto e' un osascript sprecato.
    """
    mappa = {}
    for app in _app_in_esecuzione():
        grezzo = _esegui(["osascript", "-e",
                          _TTY_ITERM if app == "iTerm2" else _TTY_TERMINAL])
        for pezzo in grezzo.replace("\n", ",").split(","):
            tty = pezzo.strip()
            if tty.startswith("/dev/"):
                mappa[tty[5:]] = app
    return mappa


def pannelli(usa_cache: bool = True) -> list:
    """I pannelli di terminale vivi, con la cartella in cui si trovano.

    Ogni voce: {tty, cwd, pid, claude, app}. `claude` dice se in quel pannello
    sta girando una sessione dell'agente, che e' il caso interessante: non "un
    terminale aperto li'" ma "il lavoro che stavi facendo".

    Quando in un pannello gira l'agente si prende la SUA cartella, non quella
    della shell che lo ospita: se dopo averlo avviato la shell ha fatto `cd`
    altrove le due divergono, e quella che dice su cosa si sta lavorando e' la
    prima.
    """
    global _cache
    adesso = time.time()
    if usa_cache and _cache[1] and (adesso - _cache[0]) < _CACHE_SECONDI:
        return _cache[1]

    interessanti = [(pid, tty, comando) for pid, tty, comando in _processi()
                    if comando in _SHELL or comando == _AGENTE]
    if not interessanti:
        _cache = (adesso, [])
        return []

    cwd = _cwd_di([pid for pid, _, _ in interessanti])
    proprietario = _tty_per_app()

    per_tty: dict = {}
    for pid, tty, comando in interessanti:
        cartella = cwd.get(pid)
        if not cartella:
            continue
        e_agente = comando == _AGENTE
        precedente = per_tty.get(tty)
        # L'agente ha la precedenza sulla shell che lo ospita: un pannello dove
        # gira una sessione non e' "un terminale in quella cartella", e' quella
        # sessione. Se e' gia' registrato, la shell non lo sovrascrive.
        if precedente is not None and precedente["claude"] and not e_agente:
            continue
        per_tty[tty] = {"tty": tty, "cwd": cartella, "pid": pid,
                        "claude": e_agente, "app": proprietario.get(tty, "")}

    fuori = sorted(per_tty.values(), key=lambda v: v["tty"])
    _cache = (adesso, fuori)
    return fuori


def _dentro(cartella: str, radice: str) -> bool:
    """True se `cartella` e' la radice o sta sotto di essa.

    Il confronto e' sui pezzi del percorso e non sulle stringhe: altrimenti
    /Users/x/app-vecchio risulterebbe dentro /Users/x/app.
    """
    if not cartella or not radice:
        return False
    try:
        a = os.path.realpath(cartella).split(os.sep)
        b = os.path.realpath(radice).split(os.sep)
    except OSError:
        return False
    return len(a) >= len(b) and a[:len(b)] == b


def per_cartella(radice: str, elenco: list | None = None) -> list:
    """I pannelli aperti dentro una certa cartella, l'agente per primo."""
    voci = [p for p in (pannelli() if elenco is None else elenco)
            if _dentro(p.get("cwd", ""), radice)]
    return sorted(voci, key=lambda v: (not v["claude"], v["tty"]))


# Finestre e tab vanno indicizzate a mano: `repeat with t in tabs of w` genera
# riferimenti che AppleScript poi rifiuta (errore -1700). Misurato, non supposto.
_DAVANTI_TERMINAL = """
tell application "Terminal"
  repeat with wi from 1 to (count of windows)
    repeat with ti from 1 to (count of tabs of window wi)
      set t to tab ti of window wi
      if tty of t is "%s" then
        set selected of t to true
        set frontmost of window wi to true
        activate
        return "ok"
      end if
    end repeat
  end repeat
end tell
return "no"
"""

_DAVANTI_ITERM = """
tell application "iTerm2"
  repeat with wi from 1 to (count of windows)
    repeat with ti from 1 to (count of tabs of window wi)
      repeat with si from 1 to (count of sessions of tab ti of window wi)
        set s to session si of tab ti of window wi
        if tty of s is "%s" then
          select window wi
          select tab ti of window wi
          select s
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no"
"""


_FRONTMOST = 'tell application "System Events" to get name of first process whose frontmost is true'
_TTY_FRONT_TERMINAL = ('tell application "Terminal" to get tty of selected tab'
                       ' of front window')
_TTY_FRONT_ITERM = ('tell application "iTerm2" to get tty of current session'
                    ' of current window')


def tty_in_primo_piano() -> str:
    """Il tty del terminale che hai davanti in questo momento, o "".

    Serve al popup per NON comparire sopra la sessione che stai gia' guardando:
    senza questo dato, il cancello "non disturbare il terminale in focus" non
    puo' scattare, e il popup diventa rumore proprio nel caso in cui sei gia'
    li'. Solo lettura: chiede l'app frontmost e, se e' un terminale, il tty
    della sua finestra davanti. Se l'app in primo piano non e' un terminale,
    ritorna "" -- e allora nessuna soppressione, il che e' giusto: stai
    guardando altro.
    """
    davanti = _esegui(["osascript", "-e", _FRONTMOST]).strip()
    if not davanti:
        return ""
    # "Terminal", "iTerm2", "iTerm" a seconda della versione.
    if davanti == "Terminal":
        script = _TTY_FRONT_TERMINAL
    elif davanti.startswith("iTerm"):
        script = _TTY_FRONT_ITERM
    else:
        return ""  # l'app davanti non e' un terminale: niente da sopprimere
    tty = _esegui(["osascript", "-e", script]).strip()
    return tty[5:] if tty.startswith("/dev/") else tty


def porta_in_primo_piano(tty: str, app: str = "") -> bool:
    """Porta davanti il pannello con quel tty. False se non ci riesce.

    False non e' un errore da nascondere: e' il segnale che chi chiama deve
    ricadere sull'aprire una finestra nuova. Capita se la finestra e' stata
    chiusa nel frattempo, o se il permesso di Automazione e' stato negato.
    """
    if not tty:
        return False
    nome = tty if tty.startswith("/dev/") else f"/dev/{tty}"
    candidate = [app] if app in _APP_NOTE.values() else _app_in_esecuzione()
    for applicazione in candidate:
        script = _DAVANTI_ITERM if applicazione == "iTerm2" else _DAVANTI_TERMINAL
        if _esegui(["osascript", "-e", script % nome]).strip() == "ok":
            return True
    return False


def apri_o_torna(radice: str) -> dict:
    """Torna al terminale gia' aperto su questa cartella, o ne apre uno nuovo.

    E' la funzione che serve alla barra e alla dashboard: UNA voce che fa la
    cosa giusta, invece di due fra cui l'utente deve scegliere sapendo cosa c'e'
    gia' aperto -- che e' esattamente l'informazione che non ha.
    """
    if not radice or not os.path.isdir(radice):
        return {"ok": False, "errore": "cartella inesistente"}
    for pannello in per_cartella(radice):
        if porta_in_primo_piano(pannello["tty"], pannello.get("app", "")):
            return {"ok": True, "azione": "in_primo_piano", "tty": pannello["tty"],
                    "claude": pannello["claude"]}
    _esegui(["open", "-a", "Terminal", radice])
    return {"ok": True, "azione": "aperto_nuovo"}
