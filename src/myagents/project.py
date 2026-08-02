"""Rileva a quale progetto appartiene una cartella.

Gira nel drainer, MAI in un hook: usa subprocess (git) ed e' troppo lento
per il percorso critico.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from .paths import OVERRIDES

_REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def _run_git(args: list[str], cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _canon(path: str) -> str:
    """Forma canonica di un percorso, per confrontare gli override.

    Un override lo scrive un umano a mano: puo' avere la tilde non espansa, una
    barra finale, o passare per un link simbolico. Senza normalizzazione non
    combacerebbe mai con il percorso che arriva da git, e il fallimento sarebbe
    silenzioso -- l'override verrebbe semplicemente ignorato, che e' il modo
    peggiore in cui questo meccanismo puo' rompersi.
    """
    try:
        return os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError, TypeError):
        return path


def _load_overrides() -> dict:
    """Carica gli override: sempre un dict di stringhe, con chiavi canonicalizzate.

    Non si fida del contenuto del file. Le voci con chiave o valore non testuali
    vengono scartate: `detect()` non deve MAI sollevare, e un valore del tipo
    sbagliato faceva fallire `forced.split()` un livello piu' in giu' del
    controllo sul tipo del file.
    """
    try:
        parsed = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        _canon(k): v
        for k, v in parsed.items()
        if isinstance(k, str) and isinstance(v, str) and v
    }


def detect(cwd: str) -> dict:
    """Ritorna {key, name, root_path, git_remote} per la cartella indicata."""
    overrides = _load_overrides()

    # 1. Exact match su cwd (cheap, senza git)
    cwd_canon = _canon(cwd)
    if cwd_canon in overrides:
        forced = overrides[cwd_canon]
        return {
            "key": forced,
            "name": forced.split("/")[-1],
            "root_path": cwd,
            "git_remote": None,
        }

    # 2. Risolvi git root e remote
    root = _run_git(["rev-parse", "--show-toplevel"], cwd) if Path(cwd).is_dir() else None
    remote = _run_git(["remote", "get-url", "origin"], root) if root else None

    # 3. Se ho un git root, controlla l'override su quel root (defect 3)
    root_canon = _canon(root) if root else None
    if root_canon and root_canon in overrides:
        forced = overrides[root_canon]
        return {
            "key": forced,
            "name": forced.split("/")[-1],
            "root_path": root,
            "git_remote": None,
        }

    # 4. Se ho un git remote, usa owner/repo
    if remote:
        match = _REMOTE_RE.search(remote)
        if match:
            owner, repo = match.group(1), match.group(2)
            return {
                "key": f"{owner}/{repo}",
                "name": repo,
                "root_path": root,
                "git_remote": remote,
            }

    # 5. Fallback: directory name
    # root_path deve essere sempre assoluto (defect minor)
    base_str = root or cwd
    base = Path(base_str)
    # Assicura che root_path sia assoluto
    abs_root = os.path.abspath(base_str)

    # Genera il nome: solo il vero home ha key "home" (defect 2)
    if base == Path.home():
        name = "home"
    elif base.name:
        name = base.name
    else:
        # Path("/").name è '', quindi usiamo "root" per il filesystem root
        name = "root"

    return {"key": name, "name": name, "root_path": abs_root, "git_remote": remote}
