import json

import pytest

from hazelst import cli


def test_help_lists_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "config" in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage: hazelst" in capsys.readouterr().out


def test_config_never_prints_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret-abc")
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "secret-abc" not in out
    assert json.loads(out)["settings"]["claude_token"] == "set"


def test_config_error_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HAZELST_AGENT_DAILY_CALLS", "many")
    assert cli.main(["config"]) == 2
    assert "configuration error" in capsys.readouterr().err
