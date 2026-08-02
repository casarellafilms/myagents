import importlib
import pytest

def _reload(monkeypatch, **env):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    import myagents.paths as p
    return importlib.reload(p)

def test_root_honours_taskdb_home(monkeypatch, tmp_path):
    p = _reload(monkeypatch, MYAGENTS_HOME=str(tmp_path))
    assert p.ROOT == tmp_path
    assert p.DB_PATH == tmp_path / "tasks.db"
    assert p.SPOOL_DIR == tmp_path / "spool"

def test_root_defaults_outside_claude_config(monkeypatch):
    p = _reload(monkeypatch, MYAGENTS_HOME=None)
    assert p.ROOT.name == ".myagents"
    assert "/.claude/" not in str(p.ROOT) + "/"

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("false", False),
    ("1", True), ("true", True), ("yes", True),
])
def test_is_disabled(monkeypatch, value, expected):
    p = _reload(monkeypatch, MYAGENTS_OFF=value)
    assert p.is_disabled() is expected

def test_ensure_dirs_creates_spool(monkeypatch, tmp_path):
    p = _reload(monkeypatch, MYAGENTS_HOME=str(tmp_path / "x"))
    p.ensure_dirs()
    assert p.SPOOL_DIR.is_dir()

def test_utcnow_format(monkeypatch, tmp_path):
    p = _reload(monkeypatch, MYAGENTS_HOME=str(tmp_path))
    ts = p.utcnow()
    assert ts.endswith("Z") and len(ts) == 20 and ts[10] == "T"


def test_il_file_sentinella_spegne_la_cattura(monkeypatch, tmp_path):
    """La barra deve poter fermare tutto subito, anche le sessioni gia' aperte.
    Una variabile d'ambiente vale solo per i processi avviati dopo: serve un file."""
    p = _reload(monkeypatch, MYAGENTS_HOME=str(tmp_path), MYAGENTS_OFF=None)
    assert p.is_disabled() is False
    p.SPENTO.parent.mkdir(parents=True, exist_ok=True)
    p.SPENTO.touch()
    assert p.is_disabled() is True
    p.SPENTO.unlink()
    assert p.is_disabled() is False


def test_la_variabile_ambiente_vince_comunque(monkeypatch, tmp_path):
    """MYAGENTS_OFF resta valido anche senza file: due strade indipendenti."""
    p = _reload(monkeypatch, MYAGENTS_HOME=str(tmp_path), MYAGENTS_OFF="1")
    assert p.is_disabled() is True
