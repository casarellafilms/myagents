"""Rete di sicurezza per l'intera suite.

Un test di `tk install` ha eseguito l'installazione contro le config dir REALI
dell'utente (~/.claude, ~/.claude-lavoro, ~/.claude-cliente), scrivendoci hook
che puntavano a un modulo non ancora esistente. Correggere quella singola riga
non basta: finche' le costanti reali sono raggiungibili da un test, prima o poi
qualcuno le raggiunge di nuovo.

Questa fixture e' `autouse`: vale per OGNI test, senza che il test debba
ricordarsi di nulla. Reindirizza sia la radice dati sia l'elenco delle config
dir su cartelle temporanee, cosi' anche una chiamata distratta a `install()`
senza argomenti finisce nel vuoto invece che nella configurazione dell'utente.
"""
import pytest


@pytest.fixture(autouse=True)
def _mai_toccare_le_config_vere(tmp_path, monkeypatch):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path / "taskdb-home"))
    finte = [tmp_path / "finta-claude", tmp_path / "finta-claude-lavoro"]
    for d in finte:
        d.mkdir(parents=True, exist_ok=True)
    import myagents.install
    monkeypatch.setattr(myagents.install, "CONFIG_DIRS", finte, raising=False)
    yield
