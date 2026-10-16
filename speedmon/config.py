"""Settings from the environment (systemd unit in production, scripts/dev.sh in development).

Secrets and data live in the data directory (var/, 0700, service user). Nothing secret is in git.
The agent's shared HMAC key arrives through systemd's credential store ($CREDENTIALS_DIRECTORY/agent-key)
or, in development, SPEEDMON_AGENT_KEY_FILE.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VERSION = "1.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENTS = ("production", "development", "test")


@dataclass
class Settings:
    env: str = "production"
    data_dir: Path = PROJECT_ROOT / "var"
    host: str = "127.0.0.1"
    port: int = 8772
    base_path: str = "/speedtest"
    public_origins: tuple = ("https://example.com",)
    secure_cookies: bool = True
    session_idle_minutes: int = 240
    session_absolute_hours: int = 24
    trusted_proxy: str = "127.0.0.1"
    entrance_max_failures: int = 5
    entrance_window_seconds: int = 900
    entrance_overall_failures: int = 30
    agent_key_file: Path | None = None
    export_keep_hours: int = 24
    raw_rows_per_sheet: int = 1_048_575            # Excel's limit (1,048,576 rows) minus the header row
    status_file: Path | None = None                 # public-safe status for other apps on the Pi (statusfile.py)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "speedmon.sqlite3"

    @property
    def phrase_file(self) -> Path:
        return self.data_dir / "entrance.json"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def agent_key(self) -> bytes | None:
        candidates = []
        if os.environ.get("CREDENTIALS_DIRECTORY"):
            candidates.append(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "agent-key")
        if self.agent_key_file:
            candidates.append(self.agent_key_file)
        for path in candidates:
            try:
                key = path.read_text().strip()
            except OSError:
                continue
            if len(key) >= 32:
                return key.encode()
        return None


def load_settings() -> Settings:
    env = os.environ
    s = Settings()
    s.env = env.get("SPEEDMON_ENV", "production")
    if s.env not in ENVIRONMENTS:
        raise SystemExit(f"SPEEDMON_ENV must be one of {', '.join(ENVIRONMENTS)}")
    if env.get("SPEEDMON_DATA_DIR"):
        s.data_dir = Path(env["SPEEDMON_DATA_DIR"])
    s.host = env.get("SPEEDMON_HOST", s.host)
    s.port = int(env.get("SPEEDMON_PORT", s.port))
    if env.get("SPEEDMON_PUBLIC_ORIGINS"):
        s.public_origins = tuple(o.strip().rstrip("/") for o in env["SPEEDMON_PUBLIC_ORIGINS"].split(",") if o.strip())
    s.secure_cookies = s.is_production or env.get("SPEEDMON_SECURE_COOKIES", "1") != "0"
    if env.get("SPEEDMON_AGENT_KEY_FILE"):
        s.agent_key_file = Path(env["SPEEDMON_AGENT_KEY_FILE"])
    if env.get("SPEEDMON_STATUS_FILE"):
        s.status_file = Path(env["SPEEDMON_STATUS_FILE"])
    if env.get("SPEEDMON_RAW_ROWS_PER_SHEET") and not s.is_production:   # tests only: exercise sheet overflow cheaply
        s.raw_rows_per_sheet = int(env["SPEEDMON_RAW_ROWS_PER_SHEET"])
    return s
