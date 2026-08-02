import importlib, json
import pytest

@pytest.fixture
def install(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path / "home"))
    import myagents.paths, myagents.install
    importlib.reload(myagents.paths)
    return importlib.reload(myagents.install)

def _settings(d):
    return json.loads((d / "settings.json").read_text(encoding="utf-8"))

def test_installs_all_events(install, tmp_path):
    d = tmp_path / ".claude"; d.mkdir()
    install.install([d], python="/usr/bin/python3", repo="/repo")
    hooks = _settings(d)["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PostToolUse", "SessionEnd"}

def test_posttooluse_matcher_covers_needed_tools(install, tmp_path):
    d = tmp_path / ".claude"; d.mkdir()
    install.install([d], python="/usr/bin/python3", repo="/repo")
    matcher = _settings(d)["hooks"]["PostToolUse"][0]["matcher"]
    for tool in ("Edit", "Write", "MultiEdit", "Bash", "TodoWrite"):
        assert tool in matcher

def test_preserves_existing_unrelated_settings(install, tmp_path):
    d = tmp_path / ".claude"; d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command",
                                             "command": "altro"}]}]}}),
        encoding="utf-8")
    install.install([d], python="/usr/bin/python3", repo="/repo")
    data = _settings(d)
    assert data["model"] == "opus"
    assert "PreToolUse" in data["hooks"]

def test_is_rerunnable_without_duplicating(install, tmp_path):
    d = tmp_path / ".claude"; d.mkdir()
    install.install([d], python="/usr/bin/python3", repo="/repo")
    install.install([d], python="/usr/bin/python3", repo="/repo")
    assert len(_settings(d)["hooks"]["SessionStart"]) == 1

def test_uninstall_removes_only_taskdb_entries(install, tmp_path):
    d = tmp_path / ".claude"; d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                               "command": "estraneo"}]}]}}),
        encoding="utf-8")
    install.install([d], python="/usr/bin/python3", repo="/repo")
    install.uninstall([d])
    entries = _settings(d)["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == "estraneo"

def test_missing_config_dir_is_skipped(install, tmp_path):
    assert install.install([tmp_path / "non-esiste"],
                           python="/usr/bin/python3", repo="/repo") == []


# --- Defect 1: un settings.json che esiste ma non si puo' interpretare non
# va mai sovrascritto. Deve restare esattamente com'era, e la sua dir non
# deve comparire nel valore di ritorno.

def test_invalid_json_is_never_overwritten_and_excluded_from_result(install, tmp_path):
    d = tmp_path / ".claude"; d.mkdir()
    original = ('{"model": "opus", "permissions": {"allow": ["Bash"]}, '
                '"hooks": {"PreToolUse": [{"hooks": [{"type": "command", '
                '"command": "ecc-hook"}]}]},}')  # virgola finale: JSON non valido
    (d / "settings.json").write_text(original, encoding="utf-8")
    result = install.install([d], python="/usr/bin/python3", repo="/repo")
    assert (d / "settings.json").read_text(encoding="utf-8") == original
    assert result == []


def test_second_dir_still_installed_when_first_is_malformed(install, tmp_path):
    bad = tmp_path / "bad"; bad.mkdir()
    (bad / "settings.json").write_text("{non e' json", encoding="utf-8")
    good = tmp_path / "good"; good.mkdir()
    result = install.install([bad, good], python="/usr/bin/python3", repo="/repo")
    assert result == [good / "settings.json"]
    assert _settings(good)["hooks"]


# --- Defect 2: il comando generato deve sopravvivere a spazi nel path del
# python interprete (il caso reale: sys.executable in un repo con spazio nel
# nome, es. "Desktop/Progetti AI/..."). Bersaglio: uno stub myagents.hook.main
# creato ad hoc (il modulo vero vive altrove), su un
# repo il cui path contiene anch'esso uno spazio, cosi' da provare sia il
# livello shell (path del python) sia il livello python (path del repo
# incorporato come stringa nello script -c).

def test_command_survives_space_in_python_path(install, tmp_path):
    import os
    import subprocess
    import sys

    python_dir = tmp_path / "python bin"
    python_dir.mkdir()
    python_bin = python_dir / "python3"
    os.symlink(sys.executable, python_bin)

    repo_dir = tmp_path / "repo dir"
    (repo_dir / "src" / "myagents").mkdir(parents=True)
    (repo_dir / "src" / "myagents" / "__init__.py").write_text("", encoding="utf-8")
    (repo_dir / "src" / "myagents" / "hook.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8")

    cmd = install._command(str(python_bin), str(repo_dir), "SessionStart")
    result = subprocess.run(cmd, shell=True)
    assert result.returncode == 0


# --- Defect 3: forme JSON valide ma inattese non devono mai far esplodere
# install() con un AttributeError a meta' strada. Vanno trattate come il
# Defect 1: dir saltata, file intonso, nessun crash.

@pytest.mark.parametrize("raw", [
    "[1, 2, 3]",
    '{"hooks": "non e un oggetto"}',
    '{"hooks": {"SessionStart": "non e una lista"}}',
    '{"hooks": {"SessionStart": ["non e un oggetto"]}}',
])
def test_malformed_shapes_are_skipped_without_crashing(install, tmp_path, raw):
    d = tmp_path / ".claude"; d.mkdir()
    (d / "settings.json").write_text(raw, encoding="utf-8")
    result = install.install([d], python="/usr/bin/python3", repo="/repo")
    assert result == []
    assert (d / "settings.json").read_text(encoding="utf-8") == raw


# --- Un config valido, con una entry estranea gia' presente, deve continuare
# a installarsi correttamente e il file risultante deve restare JSON valido
# con entrambe le entry.

def test_valid_config_installs_and_stays_valid_json_with_foreign_entry(install, tmp_path):
    d = tmp_path / ".claude"; d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command",
                                             "command": "estraneo"}]}]}}),
        encoding="utf-8")
    result = install.install([d], python="/usr/bin/python3", repo="/repo")
    assert result == [d / "settings.json"]
    data = _settings(d)
    assert data["model"] == "opus"
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "estraneo"
    assert "SessionStart" in data["hooks"]


# --- Defect 4: la scrittura deve essere atomica (temp file + os.replace) e
# preservare i permessi del file esistente. Non simuliamo un crash a meta'
# scrittura (difficile da riprodurre in modo affidabile in un test), ma
# verifichiamo che non restino file temporanei e che i permessi sopravvivano.

def test_write_preserves_file_mode_and_leaves_no_temp_files(install, tmp_path):
    import os
    import stat

    d = tmp_path / ".claude"; d.mkdir()
    settings_path = d / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    os.chmod(settings_path, 0o640)

    install.install([d], python="/usr/bin/python3", repo="/repo")

    mode = stat.S_IMODE(os.stat(settings_path).st_mode)
    assert mode == 0o640
    leftovers = [p for p in d.iterdir() if p.name != "settings.json"]
    assert leftovers == []
