"""Registra gli hook nelle config dir di Claude Code. Rieseguibile."""
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from .paths import ERROR_LOG, utcnow

def trova_config_dirs() -> list:
    """Trova da sola tutte le config dir di Claude Code presenti nella home.

    Claude Code usa ~/.claude, ma chi lavora su piu' contesti crea alias con
    CLAUDE_CONFIG_DIR che puntano a ~/.claude-<qualcosa>. Scriverle a mano
    significa dimenticarne una il giorno che ne aggiungi un'altra, e il sintomo
    sarebbe il silenzio: quel contesto semplicemente non verrebbe registrato.

    Si riconosce una config dir dalla presenza di settings.json o di una delle
    cartelle che Claude Code crea sempre.
    """
    trovate = []
    for cartella in sorted(Path.home().glob(".claude*")):
        if not cartella.is_dir():
            continue
        if ((cartella / "settings.json").is_file()
                or (cartella / "projects").is_dir()
                or (cartella / "history.jsonl").is_file()):
            trovate.append(cartella)
    return trovate or [Path.home() / ".claude"]


CONFIG_DIRS = trova_config_dirs()

MARKER = "myagents.hook"
# TaskCreate/TaskUpdate hanno sostituito TodoWrite in questa versione
# dell'harness: senza di loro nel matcher l'hook non scatta proprio e il
# mirror delle cose da fare ha copertura ZERO (verificato sul campo).
POST_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash|TodoWrite|TaskCreate|TaskUpdate"
# Notification scatta quando una sessione si ferma ad aspettare l'utente
# (misurato: notification_type='idle_prompt', message='Claude is waiting for
# your input'). E' il segnale del popup. Senza matcher: i tipi si filtrano nel
# codice, dove un tipo nuovo si tratta e si traccia invece di sparire.
# PermissionRequest scatta quando un agente sta per usare un tool: myagents lo
# osserva SOLO per i tool che chiedono qualcosa all'utente (AskUserQuestion,
# ExitPlanMode), per mostrare nel popup la domanda con le sue opzioni. Non emette
# mai una decisione: il permesso segue il suo corso normale.
EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "SessionEnd",
          "Notification", "PermissionRequest"]


class _Unreadable(RuntimeError):
    """settings.json esiste ma non si puo' leggere, non e' JSON valido, o ha
    una forma diversa da quella attesa (oggetto con 'hooks': {evento:
    [blocchi]}). Il chiamante deve rinunciare a scrivere quella dir, mai
    inventare un contenuto vuoto al suo posto (vedi Defect 1 e Defect 3)."""


def _command(python: str, repo: str, event: str) -> str:
    """Costruisce il comando shell che esegue l'hook. Doppio livello di
    quoting: il path del repo entra nello script python come stringa
    letterale (repr, cosi' sopravvive a spazi/apici), e l'intero argomento
    -c e gli altri argomenti vengono poi quotati per la shell con
    shlex.quote — altrimenti un path con uno spazio (es. sys.executable in
    "Desktop/Progetti AI/...") spezza il comando in piu' parole e l'hook non
    viene mai eseguito (Defect 2)."""
    src_path = str(Path(repo) / "src")
    inner = (f"import sys;sys.path.insert(0,{src_path!r});"
             f"from {MARKER} import main;sys.exit(main())")
    return " ".join(shlex.quote(part) for part in (python, "-c", inner, event))


def _entry(python: str, repo: str, event: str) -> dict:
    block = {"hooks": [{"type": "command", "command": _command(python, repo, event)}]}
    if event == "PostToolUse":
        block["matcher"] = POST_MATCHER
    return block


def _is_ours(block: dict) -> bool:
    for hook in block.get("hooks", []):
        if MARKER in str(hook.get("command", "")):
            return True
    return False


def _load(path: Path) -> dict:
    """Ritorna {} se il file non esiste (caso normale, sicuro). Solleva
    _Unreadable se il file esiste ma non e' interpretabile: illeggibile, JSON
    non valido, o di forma inattesa (top-level non oggetto, 'hooks' non
    oggetto, un evento non lista, un blocco non oggetto)."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _Unreadable(f"lettura fallita: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _Unreadable(f"JSON non valido: {exc}") from exc
    if not isinstance(data, dict):
        raise _Unreadable("il contenuto di primo livello non e' un oggetto JSON")
    hooks = data.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            raise _Unreadable("'hooks' non e' un oggetto")
        for event, blocks in hooks.items():
            if not isinstance(blocks, list):
                raise _Unreadable(f"hooks.{event} non e' una lista")
            for block in blocks:
                if not isinstance(block, dict):
                    raise _Unreadable(f"un blocco in hooks.{event} non e' un oggetto")
    return data


def _write(path: Path, data: dict) -> None:
    """Scrive settings.json in modo atomico: temp file nella stessa dir +
    os.replace(). Un crash o un disco pieno a meta' scrittura lascia il temp
    file orfano ma il file vero intonso (Defect 4). Preserva i permessi del
    file esistente, se c'era gia'."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    mode = path.stat().st_mode if path.exists() else None
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _log_skip(path: Path, reason: str) -> None:
    """Stessa disciplina append-and-never-raise di drain._log_error: un
    problema qui (disco pieno, permessi) non deve mai bloccare l'installer,
    e non deve mai mascherare il motivo per cui la dir e' stata saltata."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{utcnow()} install: skip {path}: {reason}\n")
    except Exception:
        pass


def install(dirs=None, python: str | None = None, repo: str | None = None) -> list[Path]:
    python = python or sys.executable
    repo = repo or str(Path(__file__).resolve().parents[2])
    touched = []
    for config_dir in (dirs if dirs is not None else CONFIG_DIRS):
        config_dir = Path(config_dir)
        if not config_dir.is_dir():
            continue
        settings_path = config_dir / "settings.json"
        try:
            data = _load(settings_path)
            hooks = data.setdefault("hooks", {})
            for event in EVENTS:
                blocks = [b for b in hooks.get(event, []) if not _is_ours(b)]
                blocks.append(_entry(python, repo, event))
                hooks[event] = blocks
            _write(settings_path, data)
        except _Unreadable as exc:
            _log_skip(settings_path, str(exc))
            continue
        except OSError as exc:
            _log_skip(settings_path, f"scrittura fallita: {exc}")
            continue
        touched.append(settings_path)
    return touched


def uninstall(dirs=None) -> list[Path]:
    touched = []
    for config_dir in (dirs if dirs is not None else CONFIG_DIRS):
        settings_path = Path(config_dir) / "settings.json"
        if not settings_path.is_file():
            continue
        try:
            data = _load(settings_path)
            hooks = data.get("hooks", {})
            for event in list(hooks):
                hooks[event] = [b for b in hooks[event] if not _is_ours(b)]
                if not hooks[event]:
                    del hooks[event]
            _write(settings_path, data)
        except _Unreadable as exc:
            _log_skip(settings_path, str(exc))
            continue
        except OSError as exc:
            _log_skip(settings_path, f"scrittura fallita: {exc}")
            continue
        touched.append(settings_path)
    return touched
