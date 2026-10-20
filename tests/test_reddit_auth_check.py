import importlib.util
import json

from src.paths import ROOT, SETTINGS_FILE

TOOL = ROOT / "tools" / "reddit_auth_check.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("reddit_auth_check", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reads_settings_from_the_repo_not_the_working_directory(monkeypatch, tmp_path, capsys):
    tool = _load_tool()
    # No vault and no credentials, so the check stops at BLOCKED and never
    # calls Reddit.
    monkeypatch.setattr(tool, "load_secrets", lambda: None)
    for env_var in json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))["reddit"].values():
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.chdir(tmp_path)

    assert tool.main() == 2
    assert "BLOCKED" in capsys.readouterr().out
