"""Coda di scrittura su file. Nessun SQLite: gli hook non devono mai contendere un lock."""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Iterator

from .paths import SPOOL_DIR, ensure_dirs, utcnow

MAX_LINE = 4096
_TRUNC_TO = 400


def _dump(event: dict) -> str:
    """Serializza un evento. Non solleva mai: un evento perso e' peggio di uno degradato.

    I payload degli hook nascono da JSON, quindi sono sempre serializzabili. Ma
    `append_event` gira dentro un hook che ingoia le eccezioni (SPEC P1): se qui
    saltasse fuori un TypeError o un ValueError, l'evento sparirebbe in silenzio,
    cioe' esattamente il fallimento che lo spool esiste per impedire.
    `default=str` copre i tipi non serializzabili (bytes, set, Path); il fallback
    copre il resto (interi oltre il limite di cifre di json).
    """
    try:
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return json.dumps(
            {
                **{k: v for k, v in event.items() if k != "payload"},
                "payload": {
                    "session_id": payload.get("session_id"),
                    "unserializable": True,
                },
                "truncated": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _shrink(payload: dict, event_template: dict) -> dict:
    """
    Degrade payload in stages until it fits MAX_LINE (4096 bytes).

    Stages (applied in order):
    1. Full payload as-is
    2. Shrink strings + replace oversized non-strings with markers
    3. Keep only critical fields (session_id, cwd) + list of dropped keys
    4. Last resort: only session_id

    session_id always survives (drainer uses it for indexing).
    """

    def _estimate_size(p: dict) -> int:
        """Estimate serialized size with given payload."""
        test_event = event_template.copy()
        test_event["payload"] = p
        test_event["truncated"] = True
        return len(_dump(test_event).encode("utf-8"))

    # Stage 1: Check if full payload already fits
    if _estimate_size(payload) <= MAX_LINE:
        return payload

    # Stage 2: Shrink strings and replace oversized non-strings with descriptors
    shrunk = {}
    for key, value in payload.items():
        if isinstance(value, str):
            # Truncate long strings
            shrunk[key] = value[:_TRUNC_TO] if len(value) > _TRUNC_TO else value
        elif isinstance(value, (dict, list)):
            # Replace oversized structures with markers
            serialized = _dump(value)
            if len(serialized.encode("utf-8")) > _TRUNC_TO:
                shrunk[key] = f"<{type(value).__name__}:{len(str(value))}>"
            else:
                shrunk[key] = value
        else:
            # Keep other types as-is (int, bool, None, etc.)
            shrunk[key] = value

    if _estimate_size(shrunk) <= MAX_LINE:
        return shrunk

    # Stage 3: Keep only critical fields + list of dropped keys
    minimal = {}
    dropped_keys = []

    for key, value in payload.items():
        if key in ("session_id", "cwd"):
            # Preserve critical fields but truncate if needed
            if isinstance(value, str):
                minimal[key] = value[:_TRUNC_TO]
            else:
                minimal[key] = value
        else:
            dropped_keys.append(key)

    if dropped_keys:
        minimal["_dropped_keys"] = dropped_keys

    if _estimate_size(minimal) <= MAX_LINE:
        return minimal

    # Stage 4: Last resort - only session_id (absolutely critical)
    if "session_id" in payload:
        sid = payload["session_id"]
        if isinstance(sid, str):
            sid = sid[:_TRUNC_TO]
        last_resort = {"session_id": sid}
        if _estimate_size(last_resort) <= MAX_LINE:
            return last_resort

    # Absolute fallback (should never reach here)
    return {}


def append_event(kind: str, payload: dict) -> str:
    """Appende un evento allo spool e ritorna il suo event_id."""
    ensure_dirs()
    event = {
        "event_id": str(uuid.uuid4()),
        "kind": kind,
        "ts": utcnow(),
        "payload": payload,
    }
    line = _dump(event)
    if len(line.encode("utf-8")) > MAX_LINE:
        # Payload is oversized; degrade it stage by stage
        event["payload"] = _shrink(payload, event)
        event["truncated"] = True
        line = _dump(event)
    path = SPOOL_DIR / f"{os.getpid()}-{time.strftime('%Y%m%d')}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return event["event_id"]


def spool_files() -> list[Path]:
    if not SPOOL_DIR.is_dir():
        return []
    return sorted(SPOOL_DIR.glob("*.jsonl"))


def read_events(path: Path) -> Iterator[dict]:
    """Legge un file di spool saltando le righe illeggibili senza interrompersi."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and "event_id" in event:
                yield event
