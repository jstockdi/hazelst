import json
from pathlib import Path

import pytest

from hazelst import config


def test_defaults() -> None:
    settings = config.settings_from_env({})
    assert settings.db_path == Path("hazelst.db")
    assert settings.nws_user_agent == "hazelst-forecast (github.com/jstockdi/hazelst)"
    assert settings.enable_agents is False
    assert settings.agent_daily_calls == 10
    assert settings.claude_token is None


def test_env_overrides() -> None:
    settings = config.settings_from_env(
        {
            "HAZELST_DB": "/data/hazelst.db",
            "HAZELST_ENABLE_AGENTS": "true",
            "HAZELST_AGENT_DAILY_CALLS": "3",
            "CLAUDE_CODE_OAUTH_TOKEN": "token-value",
        }
    )
    assert settings.db_path == Path("/data/hazelst.db")
    assert settings.enable_agents is True
    assert settings.agent_daily_calls == 3
    assert settings.claude_token is not None
    assert settings.claude_token.get_secret_value() == "token-value"


def test_agents_need_token() -> None:
    with pytest.raises(config.ConfigError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        config.settings_from_env({"HAZELST_ENABLE_AGENTS": "1"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HAZELST_ENABLE_AGENTS", "maybe"),
        ("HAZELST_AGENT_DAILY_CALLS", "ten"),
        ("HAZELST_AGENT_DAILY_CALLS", "-1"),
        ("NWS_USER_AGENT", "  "),
    ],
)
def test_invalid_env(name: str, value: str) -> None:
    with pytest.raises(config.ConfigError):
        config.settings_from_env({name: value})


def test_public_dict_hides_token() -> None:
    settings = config.settings_from_env({"CLAUDE_CODE_OAUTH_TOKEN": "secret-123"})
    public = settings.public_dict()
    assert public["claude_token"] == "set"
    assert "secret-123" not in json.dumps(public)
    assert "secret-123" not in repr(settings)


def test_roster_loads() -> None:
    cfg = config.load({})
    keys = [m.key for m in cfg.mountains]
    assert keys == [
        "killington",
        "pico",
        "sugarbush",
        "stratton",
        "loon",
        "cranmore",
        "sunday_river",
        "sugarloaf",
    ]
    assert cfg.mountain("sugarloaf").summit_ft == 4237
    assert cfg.timezone == "America/New_York"


def test_roster_rejects_duplicates(tmp_path: Path) -> None:
    entry = json.loads(config.DEFAULT_MOUNTAINS.read_text())[0]
    path = tmp_path / "mountains.json"
    path.write_text(json.dumps([entry, entry]))
    with pytest.raises(config.ConfigError, match="duplicate"):
        config.load_mountains(path)


def test_roster_rejects_bad_entry(tmp_path: Path) -> None:
    entry = json.loads(config.DEFAULT_MOUNTAINS.read_text())[0]
    entry["base_ft"] = entry["summit_ft"] + 1
    path = tmp_path / "mountains.json"
    path.write_text(json.dumps([entry]))
    with pytest.raises(config.ConfigError, match="summit_ft"):
        config.load_mountains(path)
