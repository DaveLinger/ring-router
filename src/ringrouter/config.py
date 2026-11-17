"""Configuration (TOML) and secrets (git-ignored .env file, real env vars override)."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SECRET_NAMES = ("path_token", "signing_secret", "activity_token", "openrouter_api_key", "pushcut_webhook_url",
                "pushcut_api_key", "hubitat_token")

PROJECT_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_DIR / ".env"
DEFAULT_CONFIG = PROJECT_DIR / "config.toml"
EXAMPLE_CONFIG = PROJECT_DIR / "config.example.toml"

log = logging.getLogger(__name__)


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path
    secrets: dict[str, str] = field(default_factory=dict)
    # Not a config.toml setting: `ring-router route` and the tests run handlers without side effects.
    # The service's safety switch is [server] test_mode (worked out, then waits for Proceed).
    dry_run: bool = False

    # convenient accessors -------------------------------------------------
    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    @property
    def server(self) -> dict[str, Any]:
        return self.section("server")

    @property
    def state_dir(self) -> Path:
        return Path(os.path.expanduser(self.server.get("state_dir", "~/Library/Application Support/ring-router")))

    def module_on(self, name: str) -> bool:
        """[modules] <name> = false turns a whole feature off (default: on)."""
        return bool(self.raw.get("modules", {}).get(name, True))

    def is_dry_run(self, route: str) -> bool:
        return self.dry_run

    def secret(self, name: str) -> str | None:
        return self.secrets.get(name)


def load_config(path: str | os.PathLike | None = None, *, load_secrets: bool = True) -> Config:
    p = Path(path) if path else (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG)
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    cfg = Config(raw=raw, path=p)
    server = raw.get("server", {})
    if "dry_run" in server or "dry_run_routes" in server:   # replaced by test_mode
        if server.get("dry_run") or server.get("dry_run_routes"):
            server["test_mode"] = True          # an old "don't act" setting must never quietly go live
        log.warning("[server] dry_run / dry_run_routes are gone; using test_mode = %s. Remove them from %s.",
                    bool(server.get("test_mode")), p.name)
    if load_secrets:
        cfg.secrets = read_secrets()
    return cfg


def env_key(name: str) -> str:
    return f"RING_ROUTER_{name.upper()}"


def parse_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    """KEY=value lines; # comments and blank lines ignored; optional quotes stripped."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def read_secrets(path: Path = ENV_FILE) -> dict[str, str]:
    """Real environment variables win; otherwise the project's .env file."""
    file_vals = parse_env_file(path)
    out: dict[str, str] = {}
    for name in SECRET_NAMES:
        val = os.environ.get(env_key(name)) or file_vals.get(env_key(name), "")
        if val:
            out[name] = val
    return out


def write_secret(name: str, value: str, path: Path = ENV_FILE) -> None:
    """Set one secret in .env (created with mode 600), keeping other lines."""
    key = env_key(name)
    lines = path.read_text().splitlines() if path.exists() else [
        "# ring-router secrets. Git-ignored; keep this file mode 600."]
    lines = [ln for ln in lines if not ln.strip().startswith(key + "=")] + [f"{key}={value}"]
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
