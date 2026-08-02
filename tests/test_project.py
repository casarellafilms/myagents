import importlib, json, subprocess
from pathlib import Path
import pytest

@pytest.fixture
def project(monkeypatch, tmp_path):
    monkeypatch.setenv("MYAGENTS_HOME", str(tmp_path / "home"))
    import myagents.paths, myagents.project
    importlib.reload(myagents.paths)
    return importlib.reload(myagents.project)

def _git_repo(path, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path

def test_key_from_git_remote(project, tmp_path):
    repo = _git_repo(tmp_path / "wrk", "git@github.com:acme/negozio.git")
    info = project.detect(str(repo))
    assert info["key"] == "acme/negozio"
    assert info["name"] == "negozio"

def test_key_from_git_root_when_no_remote(project, tmp_path):
    repo = _git_repo(tmp_path / "senza-remote")
    info = project.detect(str(repo))
    assert info["key"] == "senza-remote"
    assert info["git_remote"] is None

def test_subdirectory_maps_to_repo_root(project, tmp_path):
    repo = _git_repo(tmp_path / "mono", "https://github.com/x/mono.git")
    sub = repo / "packages" / "app"
    sub.mkdir(parents=True)
    assert project.detect(str(sub))["key"] == "x/mono"

def test_non_git_falls_back_to_directory_name(project, tmp_path):
    plain = tmp_path / "cartella-semplice"
    plain.mkdir()
    info = project.detect(str(plain))
    assert info["key"] == "cartella-semplice"
    assert info["root_path"] == str(plain)

def test_home_directory_gets_stable_key(project):
    assert project.detect(str(Path.home()))["key"] == "home"

def test_override_wins(project, tmp_path):
    import myagents.paths as p
    p.ensure_dirs()
    target = tmp_path / "qualsiasi"
    target.mkdir()
    p.OVERRIDES.write_text(json.dumps({str(target): "progetto-forzato"}), encoding="utf-8")
    assert project.detect(str(target))["key"] == "progetto-forzato"

def test_missing_directory_does_not_raise(project):
    assert project.detect("/percorso/che/non/esiste")["key"]

def test_overrides_json_with_non_dict_values_never_raises(project, tmp_path):
    """Defect 1: JSON parsing succeeds but value is not dict -> TypeError on `in` check."""
    import myagents.paths as p
    p.ensure_dirs()

    # Test with integer
    p.OVERRIDES.write_text("42", encoding="utf-8")
    info = project.detect("/test/path")
    assert info["key"]  # Must not raise

    # Test with null
    p.OVERRIDES.write_text("null", encoding="utf-8")
    info = project.detect("/test/path")
    assert info["key"]  # Must not raise

    # Test with string
    p.OVERRIDES.write_text('"stringa"', encoding="utf-8")
    info = project.detect("/test/path")
    assert info["key"]  # Must not raise

    # Test with array
    p.OVERRIDES.write_text("[1, 2, 3]", encoding="utf-8")
    info = project.detect("/test/path")
    assert info["key"]  # Must not raise

def test_root_directory_does_not_get_key_home(project):
    """Defect 2: Path('/').name is '', falsy, so falls through to 'home'. But '/' is not home."""
    info = project.detect("/")
    assert info["key"] != "home", f"Root '/' should not have key 'home', got {info['key']}"
    # Real home must still be 'home'
    assert project.detect(str(Path.home()))["key"] == "home"

def test_override_on_git_root_applies_to_subdirectory(project, tmp_path):
    """Defect 3: Override keyed on repo root must apply when detect() called on subdirectory."""
    import myagents.paths as p
    p.ensure_dirs()

    repo = _git_repo(tmp_path / "myrepo", "https://github.com/owner/myrepo.git")
    sub = repo / "packages" / "app"
    sub.mkdir(parents=True)

    # Set override on repo root
    overrides_dict = {str(repo): "progetto-forzato"}
    p.OVERRIDES.write_text(json.dumps(overrides_dict), encoding="utf-8")

    # Override should apply to exact path
    assert project.detect(str(repo))["key"] == "progetto-forzato"
    # AND to subdirectory (currently fails)
    assert project.detect(str(sub))["key"] == "progetto-forzato"

def test_override_on_exact_path_still_applies(project, tmp_path):
    """Defect 4 (regression): Override keyed on exact path must still work."""
    import myagents.paths as p
    p.ensure_dirs()

    target = tmp_path / "quelsiasi"
    target.mkdir()

    overrides_dict = {str(target): "progetto-esatto"}
    p.OVERRIDES.write_text(json.dumps(overrides_dict), encoding="utf-8")

    info = project.detect(str(target))
    assert info["key"] == "progetto-esatto"

def test_detect_with_relative_path_returns_absolute_root_path(project, tmp_path):
    """Minor: root_path must always be absolute, even if cwd is relative."""
    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(str(tmp_path))
        # Create a non-git directory with relative path
        plain = Path("cartella-relativa")
        plain.mkdir(exist_ok=True)

        info = project.detect("cartella-relativa")
        root = Path(info["root_path"])
        assert root.is_absolute(), f"root_path should be absolute, got {info['root_path']}"
    finally:
        os.chdir(old_cwd)


def test_override_values_of_wrong_type_never_raise(project, tmp_path):
    """detect() non deve MAI sollevare: un valore non testuale faceva fallire
    forced.split() un livello sotto il controllo sul tipo del file."""
    import myagents.paths as p
    p.ensure_dirs()
    for contenuto in ('{"/tmp": 42}', '{"/tmp": null}', '{"/tmp": ["a"]}', '{"/tmp": ""}'):
        p.OVERRIDES.write_text(contenuto, encoding="utf-8")
        assert project.detect("/tmp")["key"]  # non solleva, key non vuota


def test_override_key_is_canonicalised(project, tmp_path):
    """Un override scritto a mano puo' avere barra finale o tilde: deve combaciare
    lo stesso, altrimenti viene ignorato in silenzio."""
    import json as _json
    import myagents.paths as p
    p.ensure_dirs()
    target = tmp_path / "con-barra"
    target.mkdir()
    p.OVERRIDES.write_text(_json.dumps({str(target) + "/": "forzato"}), encoding="utf-8")
    assert project.detect(str(target))["key"] == "forzato"
