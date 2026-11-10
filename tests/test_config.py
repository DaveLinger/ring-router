import os
import stat

from ringrouter.config import parse_env_file, read_secrets, write_secret


def test_env_file_roundtrip(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.delenv("RING_ROUTER_PATH_TOKEN", raising=False)
    monkeypatch.delenv("RING_ROUTER_PUSHCUT_API_KEY", raising=False)
    write_secret("path_token", "abc", env)
    write_secret("pushcut_api_key", "k1", env)
    write_secret("pushcut_api_key", "k2", env)          # replaces, doesn't duplicate
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert parse_env_file(env)["RING_ROUTER_PUSHCUT_API_KEY"] == "k2"
    s = read_secrets(env)
    assert s["path_token"] == "abc" and s["pushcut_api_key"] == "k2"


def test_real_env_overrides_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nRING_ROUTER_PATH_TOKEN="fromfile"\n')
    monkeypatch.setenv("RING_ROUTER_PATH_TOKEN", "fromenv")
    assert read_secrets(env)["path_token"] == "fromenv"
    monkeypatch.delenv("RING_ROUTER_PATH_TOKEN")
    assert read_secrets(env)["path_token"] == "fromfile"
