"""Startup configuration (listen address, paths, secret key, Xray, logging).

Only values needed *before* the database is opened live here.  Every runtime
setting (check interval, Telegram, site title, ...) is stored in the
``Setting`` table and edited from the admin panel.

Values are read from ``config.py`` in the project root.  ``install.sh`` writes
that file and the updater preserves it.  Without it the defaults below are
used, which is what ``python3 main.py`` relies on for local development.
Set the ``V2UPTIME_CONFIG`` environment variable to load another file.
"""

from __future__ import annotations

import os
import runpy
import secrets
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_FILE = BASE_DIR / "config.py"

# Upper-case names accepted in config.py -> Config field names.
_KEYS: dict[str, str] = {
    "HOST": "host",
    "PORT": "port",
    "DB_PATH": "db_path",
    "SECRET_KEY": "secret_key",
    "XRAY_BIN": "xray_bin",
    "XRAY_API_PORT": "xray_api_port",
    "SOCKS_PORT_BASE": "socks_port_base",
    "LOG_DIR": "log_dir",
    "LOG_LEVEL": "log_level",
    "COOKIE_SECURE": "cookie_secure",
}


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: Path = BASE_DIR / "data" / "uptime.db"
    secret_key: str = ""
    xray_bin: Path = BASE_DIR / "bin" / ("xray.exe" if os.name == "nt" else "xray")
    xray_api_port: int = 10085
    socks_port_base: int = 20000
    log_dir: Path = BASE_DIR / "logs"
    log_level: str = "INFO"
    # True behind nginx/HTTPS (install.sh sets it); False for local http dev.
    cookie_secure: bool = False
    config_file: Path | None = None

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @property
    def xray_run_dir(self) -> Path:
        return self.data_dir / "xray"


def _resolve_path(value: Any) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _coerce(values: dict[str, Any]) -> dict[str, Any]:
    types = {f.name: f.type for f in fields(Config)}
    out: dict[str, Any] = {}
    for name, value in values.items():
        kind = str(types[name])
        if "Path" in kind:
            out[name] = _resolve_path(value)
        elif kind == "int":
            out[name] = int(value)
        elif kind == "bool":
            out[name] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        else:
            out[name] = str(value)
    return out


def _load_or_create_secret(data_dir: Path) -> str:
    """Persist a random secret so sessions survive restarts in dev mode."""
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_file = data_dir / "secret_key"
    if secret_file.is_file():
        secret = secret_file.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    secret = secrets.token_urlsafe(48)
    secret_file.write_text(secret, encoding="utf-8")
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass
    return secret


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path or os.environ.get("V2UPTIME_CONFIG") or DEFAULT_CONFIG_FILE)
    values: dict[str, Any] = {}
    if config_path.is_file():
        namespace = runpy.run_path(str(config_path))
        values = {field: namespace[key] for key, field in _KEYS.items() if key in namespace}
    cfg = Config(**_coerce(values), config_file=config_path if config_path.is_file() else None)
    if not cfg.secret_key:
        cfg = replace(cfg, secret_key=_load_or_create_secret(cfg.data_dir))
    return cfg


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg
