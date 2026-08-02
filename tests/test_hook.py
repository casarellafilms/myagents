"""Test per l'entrypoint hook.py.

I payload usati qui riproducono la forma REALE misurata in
docs/findings-fase-1.md (31 eventi reali, Claude Code v2.1.220), non quella
ipotizzata a priori: tutti gli eventi portano `hook_event_name`; il
`tool_response` di Bash contiene esattamente stdout/stderr/interrupted/
isImage/noOutputExpected e MAI exit_code; TodoWrite non e' mai scattato nella
cattura quindi la sua forma resta ignota e va trattata in modo difensivo.
"""
import importlib, io, json
import pytest


@pytest.fixture
def mods(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MYAGENTS_OFF", raising=False)
    import myagents.paths, myagents.spool, myagents.hook
    importlib.reload(myagents.paths)
    importlib.reload(myagents.spool)
    return myagents.spool, importlib.reload(myagents.hook)


def _events(spool):
    out = []
    for f in spool.spool_files():
        out.extend(spool.read_events(f))
    return out


def _run(hook, event_name, data):
    return hook.main([event_name], io.StringIO(json.dumps(data)))


def _error_log_text():
    from myagents.paths import ERROR_LOG
    if not ERROR_LOG.exists():
        return ""
    return ERROR_LOG.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Forma base misurata: tutti gli eventi portano hook_event_name nel payload.
# ---------------------------------------------------------------------------

def test_prompt_is_appended(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "UserPromptSubmit", "transcript_path": "/tmp/t.jsonl",
        "prompt": "ciao", "prompt_id": "p1", "permission_mode": "default",
    }
    assert _run(hook, "UserPromptSubmit", data) == 0
    (ev,) = _events(spool)
    assert ev["kind"] == "prompt"
    assert ev["payload"]["text"] == "ciao"
    assert ev["payload"]["session_id"] == "s1"


def test_hook_event_name_from_payload_is_preferred_over_argv(mods):
    """MISURATO: hook_event_name e' nel payload e va preferito ad argv[0]."""
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "UserPromptSubmit", "prompt": "ciao",
    }
    # argv[0] deliberatamente sbagliato/generico: il payload deve vincere.
    assert hook.main(["Notification"], io.StringIO(json.dumps(data))) == 0
    (ev,) = _events(spool)
    assert ev["kind"] == "prompt"


def test_falls_back_to_argv_when_hook_event_name_missing(mods):
    spool, hook = mods
    data = {"session_id": "s1", "cwd": "/tmp/x", "prompt": "ciao"}
    assert _run(hook, "UserPromptSubmit", data) == 0
    (ev,) = _events(spool)
    assert ev["kind"] == "prompt"


def test_session_start_is_appended(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "SessionStart", "transcript_path": "/tmp/t.jsonl",
        "model": "claude-opus-5", "source": "startup",
    }
    _run(hook, "SessionStart", data)
    (ev,) = _events(spool)
    assert ev["kind"] == "session_start"
    assert ev["payload"]["session_id"] == "s1"


def test_session_end_is_appended(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "SessionEnd", "reason": "clean", "prompt_id": "p9",
    }
    _run(hook, "SessionEnd", data)
    (ev,) = _events(spool)
    assert ev["kind"] == "session_end"


# ---------------------------------------------------------------------------
# PostToolUse / Edit-Write: tool_input misurato per Write = {content, file_path}
# ---------------------------------------------------------------------------

def test_edit_is_appended(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/x/a.py", "content": "print(1)\n"},
        "tool_response": {"filePath": "/tmp/x/a.py"},
        "tool_use_id": "t1", "duration_ms": 12, "effort": "low",
        "permission_mode": "default", "prompt_id": "p1",
    }
    _run(hook, "PostToolUse", data)
    (ev,) = _events(spool)
    assert ev["kind"] == "file_edit"
    assert ev["payload"]["path"] == "/tmp/x/a.py"


# ---------------------------------------------------------------------------
# PostToolUse / Bash: tool_response misurato = ESATTAMENTE questi 5 campi.
# NESSUN exit_code: e' la scoperta che ha cambiato il piano (findings §
# "SCOPERTA CHE CAMBIA IL PROGETTO"). Il test blinda che l'hook non lo inventi.
# ---------------------------------------------------------------------------

def test_bash_records_command_and_stderr_with_measured_response_shape(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "pytest -q", "description": "run tests"},
        "tool_response": {
            "stdout": "3 passed\n",
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
        },
        "tool_use_id": "t2", "duration_ms": 900, "effort": "low",
        "permission_mode": "default", "prompt_id": "p1",
    }
    _run(hook, "PostToolUse", data)
    (ev,) = _events(spool)
    assert ev["kind"] == "command"
    assert ev["payload"]["command"] == "pytest -q"
    assert ev["payload"]["interrupted"] is False
    assert "exit_code" not in ev["payload"]
    assert "stdout" not in ev["payload"]  # non richiesto: solo stderr+interrupted


def test_bash_records_interrupted_flag(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "sleep 100", "description": "long"},
        "tool_response": {
            "stdout": "", "stderr": "", "interrupted": True,
            "isImage": False, "noOutputExpected": False,
        },
    }
    _run(hook, "PostToolUse", data)
    (ev,) = _events(spool)
    assert ev["payload"]["interrupted"] is True
    assert "exit_code" not in ev["payload"]


def test_bash_stderr_is_truncated(mods):
    spool, hook = mods
    long_stderr = "x" * 5000
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "boom", "description": "d"},
        "tool_response": {
            "stdout": "", "stderr": long_stderr, "interrupted": False,
            "isImage": False, "noOutputExpected": False,
        },
    }
    _run(hook, "PostToolUse", data)
    (ev,) = _events(spool)
    assert len(ev["payload"]["stderr"]) <= 400


def test_bash_missing_tool_response_never_fails(mods):
    """tool_response potrebbe mancare del tutto (es. comando che non parte)."""
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "true"},
    }
    assert _run(hook, "PostToolUse", data) == 0
    (ev,) = _events(spool)
    assert ev["payload"]["command"] == "true"
    assert "exit_code" not in ev["payload"]


# ---------------------------------------------------------------------------
# TodoWrite: MAI misurato negli eventi reali (0/31). Trattamento difensivo:
# accetta la forma ragionevole, logga su ERROR_LOG qualunque forma diversa.
# ---------------------------------------------------------------------------

def test_todowrite_reasonable_shape_is_mirrored(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "TodoWrite",
        "tool_input": {"todos": [{"content": "Fare X", "status": "pending"}]},
    }
    _run(hook, "PostToolUse", data)
    (ev,) = _events(spool)
    assert ev["kind"] == "todo"
    assert ev["payload"]["items"][0]["content"] == "Fare X"
    assert _error_log_text() == ""  # forma riconosciuta: nessun log


def test_todowrite_unrecognized_shape_is_logged_not_dropped_silently(mods):
    """Una forma che non corrisponde a quella ragionevole non deve sparire nel
    nulla: va registrata su ERROR_LOG cosi' la prima occorrenza reale si
    scopre dal log invece che dal silenzio (vedi findings-fase-1.md)."""
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "TodoWrite",
        "tool_input": {"todos": [{"chiave_mai_vista": 42, "altro": [1, 2]}]},
    }
    assert _run(hook, "PostToolUse", data) == 0
    log = _error_log_text()
    assert "TodoWrite" in log
    assert "chiave_mai_vista" in log


def test_taskcreate_is_mirrored_like_todowrite(mods):
    """TodoWrite non esiste piu' in questa versione dell'harness: si chiama
    TaskCreate/TaskUpdate. Cercando solo il nome storico il mirror aveva
    copertura ZERO (verificato sul campo: 0 task da una sessione che ne aveva
    creati tre)."""
    spool, hook = mods
    for tool, campo in (("TaskCreate", "tasks"), ("TaskUpdate", "todos")):
        data = {
            "session_id": "s1", "cwd": "/tmp/x",
            "hook_event_name": "PostToolUse", "tool_name": tool,
            "tool_input": {campo: [{"content": f"voce da {tool}", "status": "pending"}]},
        }
        assert _run(hook, "PostToolUse", data) == 0
    eventi = [e for f in spool.spool_files() for e in spool.read_events(f)
              if e["kind"] == "todo"]
    assert len(eventi) == 2
    assert {e["payload"]["tool"] for e in eventi} == {"TaskCreate", "TaskUpdate"}
    assert eventi[0]["payload"]["items"][0]["content"] == "voce da TaskCreate"


def test_taskcreate_with_a_single_item_not_a_list(mods):
    """La forma di TaskCreate non e' mai stata osservata: potrebbe passare una
    voce sola invece di una lista. Va accettata, non buttata."""
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "TaskCreate",
        "tool_input": {"content": "voce singola", "status": "in_progress"},
    }
    assert _run(hook, "PostToolUse", data) == 0
    eventi = [e for f in spool.spool_files() for e in spool.read_events(f)
              if e["kind"] == "todo"]
    assert eventi[0]["payload"]["items"] == [
        {"content": "voce singola", "status": "in_progress"}]


def test_session_start_captures_source(mods):
    """`source` e' il solo dato che distingue una sessione vera da una headless
    lanciata da un altro strumento: 7 sessioni fantasma su 8 nel primo test
    reale. Va registrato, non dedotto."""
    spool, hook = mods
    data = {"session_id": "s1", "cwd": "/tmp/x",
            "hook_event_name": "SessionStart", "source": "startup"}
    assert _run(hook, "SessionStart", data) == 0
    eventi = [e for f in spool.spool_files() for e in spool.read_events(f)
              if e["kind"] == "session_start"]
    assert eventi[0]["payload"]["source"] == "startup"


def test_todowrite_missing_todos_key_is_logged(mods):
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "TodoWrite",
        "tool_input": {},
    }
    assert _run(hook, "PostToolUse", data) == 0
    assert "TodoWrite" in _error_log_text()
    assert _events(spool) == []


def test_todowrite_empty_list_is_not_logged_as_unrecognized(mods):
    """Una todo list vuota e' una forma ragionevole (nessun item), non un
    formato inatteso: non deve inondare ERROR_LOG."""
    spool, hook = mods
    data = {
        "session_id": "s1", "cwd": "/tmp/x",
        "hook_event_name": "PostToolUse", "tool_name": "TodoWrite",
        "tool_input": {"todos": []},
    }
    _run(hook, "PostToolUse", data)
    assert _events(spool) == []
    assert _error_log_text() == ""


# ---------------------------------------------------------------------------
# Robustezza generale: l'hook non deve MAI far cadere la sessione utente.
# ---------------------------------------------------------------------------

def test_unknown_tool_is_ignored(mods):
    spool, hook = mods
    data = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "WebFetch"}
    _run(hook, "PostToolUse", data)
    assert _events(spool) == []


def test_kill_switch_writes_nothing(monkeypatch, mods):
    spool, hook = mods
    monkeypatch.setenv("MYAGENTS_OFF", "1")
    data = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "ciao"}
    assert _run(hook, "UserPromptSubmit", data) == 0
    assert _events(spool) == []


def test_kill_switch_falsey_values_do_not_disable(monkeypatch, mods):
    """paths.is_disabled(): solo '', '0', 'false', 'no', 'off' (case-insensitive)
    disattivano il kill-switch. Qualunque altro valore lo attiva."""
    spool, hook = mods
    monkeypatch.setenv("MYAGENTS_OFF", "0")
    data = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "ciao"}
    _run(hook, "UserPromptSubmit", data)
    assert len(_events(spool)) == 1


def test_malformed_stdin_never_fails(mods):
    spool, hook = mods
    assert hook.main(["UserPromptSubmit"], io.StringIO("non json")) == 0
    assert _events(spool) == []


def test_non_dict_json_never_fails(mods):
    spool, hook = mods
    assert hook.main(["UserPromptSubmit"], io.StringIO("[1, 2, 3]")) == 0
    assert _events(spool) == []


def test_missing_argument_and_no_hook_event_name_never_fails(mods):
    spool, hook = mods
    assert hook.main([], io.StringIO("{}")) == 0
    assert _events(spool) == []


def test_internal_exception_never_fails(monkeypatch, mods):
    spool, hook = mods

    def boom(*args, **kwargs):
        raise RuntimeError("esplosione")

    monkeypatch.setattr(hook, "append_event", boom)
    data = {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "prompt": "x"}
    assert _run(hook, "UserPromptSubmit", data) == 0
    assert "esplosione" in _error_log_text()


def test_never_writes_to_stderr(mods, capsys):
    spool, hook = mods
    data = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "x"}, "tool_response": {}}
    _run(hook, "PostToolUse", data)
    captured = capsys.readouterr()
    assert captured.err == ""


def test_alias_predefinito_quando_la_variabile_non_esiste(monkeypatch, mods):
    """CLAUDE_CONFIG_DIR e' impostata solo dagli alias: col comando `claude`
    normale non esiste, e senza ripiego non si capiva da dove arrivava il lavoro."""
    spool, hook = mods
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _run(hook, "SessionStart", {"session_id": "s1", "cwd": "/tmp/x"})
    ev = [e for f in spool.spool_files() for e in spool.read_events(f)
          if e["kind"] == "session_start"][0]
    assert ev["payload"]["config_dir"].endswith("/.claude")


def test_alias_esplicito_viene_rispettato(monkeypatch, mods):
    spool, hook = mods
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/x/.claude-lavoro")
    _run(hook, "SessionStart", {"session_id": "s2", "cwd": "/tmp/x"})
    ev = [e for f in spool.spool_files() for e in spool.read_events(f)
          if e["kind"] == "session_start"][0]
    assert ev["payload"]["config_dir"] == "/Users/x/.claude-lavoro"
