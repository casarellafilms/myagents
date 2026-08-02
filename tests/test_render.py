"""Il bigliettino: cosa contiene, e soprattutto quando NON deve esistere."""
import importlib
import io
import json

import pytest


@pytest.fixture
def mods(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MYAGENTS_OFF", raising=False)
    import myagents.paths, myagents.spool, myagents.schema, myagents.project
    import myagents.render, myagents.drain, myagents.hook
    for mod in (myagents.paths, myagents.spool, myagents.schema, myagents.project,
                myagents.render, myagents.drain):
        importlib.reload(mod)
    return myagents.spool, myagents.schema, myagents.drain, myagents.render, \
        importlib.reload(myagents.hook)


def _sessione(spool, cwd, sid="s1"):
    spool.append_event("session_start", {"session_id": sid, "cwd": str(cwd),
                                         "config_dir": "/x/.claude"})


def _testo(schema):
    c = schema.connect()
    return c.execute("SELECT ps.injection FROM project_state ps"
                     " JOIN projects p ON p.id = ps.project_id").fetchone()[0]


def test_una_sessione_senza_attivita_non_produce_bigliettino(mods, tmp_path):
    """Altri strumenti lanciano Claude in headless: quelle invocazioni hanno un
    messaggio e zero attivita' (7 su 8 nel primo test reale). Non devono
    finire nel bigliettino, e la regola robusta e' guardare cosa hanno FATTO,
    non provare a riconoscere chi sono."""
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-fantasma"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("prompt", {"session_id": "s1", "cwd": str(work),
                                  "text": "Below is a conversation log..."})
    drain.drain()
    assert _testo(schema) == ""


def test_il_bigliettino_riporta_attivita_e_ultima_richiesta(mods, tmp_path):
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-vivo"
    (work / "src").mkdir(parents=True)
    _sessione(spool, work)
    spool.append_event("prompt", {"session_id": "s1", "cwd": str(work),
                                  "text": "sistemiamo il badge PRO"})
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Edit",
                                     "path": str(work / "src" / "badge.tsx")})
    drain.drain()
    testo = _testo(schema)
    assert "progetto-vivo" in testo
    assert "1 modifiche" in testo
    assert "sistemiamo il badge PRO" in testo


def test_il_bigliettino_ignora_i_prompt_di_sessioni_senza_attivita(mods, tmp_path):
    """Due sessioni sullo stesso progetto: una lavora, una e' un fantasma.
    L'ultima richiesta mostrata deve essere quella di chi ha lavorato."""
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-misto"
    work.mkdir()
    _sessione(spool, work, "vera")
    spool.append_event("prompt", {"session_id": "vera", "cwd": str(work),
                                  "text": "richiesta vera"})
    spool.append_event("file_edit", {"session_id": "vera", "tool": "Write",
                                     "path": str(work / "f.txt")})
    _sessione(spool, work, "fantasma")
    spool.append_event("prompt", {"session_id": "fantasma", "cwd": str(work),
                                  "text": "Below is a conversation log..."})
    drain.drain()
    testo = _testo(schema)
    assert "richiesta vera" in testo
    assert "conversation log" not in testo


def test_un_task_dichiarato_fatto_compare_come_non_verificato(mods, tmp_path):
    """La ragione d'essere del progetto: 'fatto' e 'detto fatto' devono
    restare distinguibili anche nel bigliettino."""
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-claim"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(work / "f.txt")})
    spool.append_event("todo", {"session_id": "s1", "items": [
        {"content": "Aggiungere auth JWT", "status": "completed"}]})
    drain.drain()
    testo = _testo(schema)
    assert "non verificato" in testo
    assert "Aggiungere auth JWT" in testo


def test_hook_inietta_nel_formato_di_claude_code_e_solo_quello(mods, tmp_path, capsys):
    """Claude Code legge sia additional_context sia hookSpecificOutput senza
    deduplicare: emetterli entrambi iniettherebbe il testo DUE volte a ogni
    messaggio (docs/findings-fase-2.md)."""
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-iniezione"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(work / "f.txt")})
    drain.drain()
    capsys.readouterr()
    data = {"session_id": "s2", "cwd": str(work), "prompt": "ciao",
            "hook_event_name": "UserPromptSubmit"}
    assert hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data))) == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload.keys()) == ["hookSpecificOutput"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "progetto-iniezione" in payload["hookSpecificOutput"]["additionalContext"]


def test_hook_tace_se_non_ce_un_bigliettino(mods, tmp_path, capsys):
    """Nessuna iniezione e' meglio di una sbagliata: un bigliettino che afferma
    il falso ti fa ripartire da un presupposto errato senza saperlo."""
    spool, schema, drain, render, hook = mods
    capsys.readouterr()
    data = {"session_id": "s1", "cwd": str(tmp_path / "mai-vista"),
            "prompt": "ciao", "hook_event_name": "UserPromptSubmit"}
    assert hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data))) == 0
    assert capsys.readouterr().out == ""


def test_hook_non_inietta_se_il_kill_switch_e_attivo(monkeypatch, mods, tmp_path, capsys):
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-spento"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(work / "f.txt")})
    drain.drain()
    monkeypatch.setenv("MYAGENTS_OFF", "1")
    import myagents.paths, myagents.hook
    importlib.reload(myagents.paths)
    h = importlib.reload(myagents.hook)
    capsys.readouterr()
    data = {"session_id": "s2", "cwd": str(work), "prompt": "ciao"}
    assert h.main(["UserPromptSubmit"], io.StringIO(json.dumps(data))) == 0
    assert capsys.readouterr().out == ""


def test_lo_stesso_bigliettino_non_viene_riemesso_a_ogni_messaggio(mods, tmp_path, capsys):
    """Misurato sul campo: tre messaggi = tre copie identiche nel contesto.
    Il bigliettino nasce per ripulire il contesto, non per riempirlo."""
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-ripetuto"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(work / "f.txt")})
    drain.drain()
    data = {"session_id": "sessione-utente", "cwd": str(work), "prompt": "ciao"}
    capsys.readouterr()

    hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data)))
    primo = capsys.readouterr().out
    assert "progetto-ripetuto" in primo

    for _ in range(3):
        hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data)))
        assert capsys.readouterr().out == ""


def test_un_bigliettino_cambiato_viene_riemesso(mods, tmp_path, capsys):
    """La deduplicazione non deve nascondere un aggiornamento vero."""
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-aggiornato"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(work / "a.txt")})
    drain.drain()
    data = {"session_id": "sessione-utente", "cwd": str(work), "prompt": "ciao"}
    capsys.readouterr()
    hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data)))
    assert capsys.readouterr().out != ""
    hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data)))
    assert capsys.readouterr().out == ""

    # nuovo lavoro -> il bigliettino cambia -> deve tornare a comparire
    spool.append_event("prompt", {"session_id": "s1", "cwd": str(work),
                                  "text": "una richiesta nuova"})
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Edit",
                                     "path": str(work / "b.txt")})
    drain.drain()
    capsys.readouterr()
    hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data)))
    assert "una richiesta nuova" in capsys.readouterr().out


def test_sessioni_diverse_ricevono_ciascuna_il_proprio(mods, tmp_path, capsys):
    """La deduplicazione e' per sessione: una sessione nuova deve riceverlo
    anche se un'altra l'ha gia' visto."""
    spool, schema, drain, render, hook = mods
    work = tmp_path / "progetto-condiviso"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(work / "f.txt")})
    drain.drain()
    capsys.readouterr()
    for sid in ("prima", "seconda"):
        data = {"session_id": sid, "cwd": str(work), "prompt": "ciao"}
        hook.main(["UserPromptSubmit"], io.StringIO(json.dumps(data)))
        assert "progetto-condiviso" in capsys.readouterr().out


def test_la_chiave_della_cartella_e_la_stessa_da_entrambe_le_parti(mods, tmp_path):
    """Il drainer scrive il file e l'hook lo cerca: se le due parti calcolassero
    la chiave in modo diverso il sintomo sarebbe silenzio totale."""
    spool, schema, drain, render, hook = mods
    import myagents.paths as p
    work = tmp_path / "progetto-chiave"
    work.mkdir()
    _sessione(spool, work)
    spool.append_event("file_edit", {"session_id": "s1", "tool": "Write",
                                     "path": str(work / "f.txt")})
    drain.drain()
    atteso = p.INJECTION_DIR / f"{p.chiave_cartella(str(work))}.txt"
    assert atteso.is_file()
    assert "progetto-chiave" in atteso.read_text(encoding="utf-8")
