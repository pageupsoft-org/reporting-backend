"""Settings from environment variables.

Development (default) has safe local defaults: generated secrets in ``data/``, a SQLite app
database. Production (``REPORTING_ENV=production``) refuses to start unless secrets and key
settings are supplied explicitly, and reports every problem at once.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("Invalid configuration:\n  - " + "\n  - ".join(problems))
        self.problems = problems


def _secret_file(path: Path, make: Callable[[], str]) -> str:
    """Read a generated dev secret, creating it (mode 600) on first use."""
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = make()
    path.write_text(value)
    path.chmod(0o600)
    return value


def _load_env_file() -> None:
    """Load ``REPORTING_ENV_FILE`` (or ``./.env``) without overriding real env vars."""
    path = os.environ.get("REPORTING_ENV_FILE") or ".env"
    if Path(path).is_file():
        from dotenv import load_dotenv

        load_dotenv(path, override=False)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    app_db_url: str
    master_key: str
    jwt_secret: str
    environment: str = "development"
    redis_url: str | None = None
    cache_ttl_s: int = 300
    session_ttl_s: int = 12 * 3600
    admin_username: str = "admin"
    admin_password: str | None = None
    cors_origins: tuple[str, ...] = ()
    embed_key: str | None = None
    public_url: str | None = None  # origin used in embed snippets, e.g. https://reports.example.com
    static_dir: Path | None = None
    data_dir: Path = field(default_factory=lambda: Path("data"))
    api_docs: bool = True
    max_body_bytes: int = 8 * 1024 * 1024
    login_attempts_per_minute: int = 10

    @property
    def production(self) -> bool:
        return self.environment == "production"

    @property
    def https(self) -> bool:
        return bool(self.public_url and self.public_url.startswith("https://"))

    @classmethod
    def from_env(cls, data_dir: str | Path | None = None) -> Settings:
        _load_env_file()
        env = os.environ.get("REPORTING_ENV", "development").strip().lower()
        production = env == "production"
        data = Path(data_dir or os.environ.get("REPORTING_DATA_DIR", "data")).resolve()
        static = os.environ.get("REPORTING_STATIC_DIR")
        default_static = Path(__file__).resolve().parents[2] / "frontend" / "dist"

        def secret(name: str, file: str, make: Callable[[], str]) -> str:
            value = os.environ.get(name)
            if value or production:
                return value or ""  # production: validated below, never generated
            return _secret_file(data / file, make)

        from reporting.connections import generate_master_key

        settings = cls(
            environment=env,
            app_db_url=os.environ.get("REPORTING_APP_DB_URL")
            or ("" if production else f"sqlite:///{data / 'app.db'}"),
            master_key=secret("REPORTING_MASTER_KEY", "master.key", generate_master_key),
            jwt_secret=secret("REPORTING_JWT_SECRET", "jwt.secret",
                              lambda: secrets.token_urlsafe(48)),
            redis_url=os.environ.get("REDIS_URL") or None,
            cache_ttl_s=int(os.environ.get("REPORTING_CACHE_TTL", "300")),
            session_ttl_s=int(os.environ.get("REPORTING_SESSION_TTL", str(12 * 3600))),
            admin_username=os.environ.get("REPORTING_ADMIN_USER", "admin"),
            admin_password=os.environ.get("REPORTING_ADMIN_PASSWORD") or None,
            embed_key=os.environ.get("REPORTING_EMBED_KEY") or None,
            public_url=(os.environ.get("REPORTING_PUBLIC_URL") or "").rstrip("/") or None,
            cors_origins=tuple(
                o.strip() for o in os.environ.get("REPORTING_CORS_ORIGINS", "").split(",")
                if o.strip()),
            static_dir=Path(static) if static else (default_static if default_static.exists()
                                                    else None),
            data_dir=data,
            api_docs=_bool("REPORTING_API_DOCS", not production),
            max_body_bytes=int(os.environ.get("REPORTING_MAX_BODY_BYTES", str(8 * 1024 * 1024))),
            login_attempts_per_minute=int(os.environ.get("REPORTING_LOGIN_ATTEMPTS", "10")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        problems: list[str] = []
        if self.environment not in ("development", "production"):
            problems.append("REPORTING_ENV must be 'development' or 'production'")
        try:
            if len(base64.b64decode(self.master_key, validate=True)) != 32:
                raise ValueError
        except (ValueError, binascii.Error):
            problems.append("REPORTING_MASTER_KEY must be a base64-encoded 32-byte key "
                            "(generate one with: python scripts/generate_secrets.py)")
        if len(self.jwt_secret) < 32:
            problems.append("REPORTING_JWT_SECRET must be at least 32 characters")
        if self.embed_key is not None and len(self.embed_key) < 32:
            problems.append("REPORTING_EMBED_KEY must be at least 32 characters")
        if self.max_body_bytes < 1024:
            problems.append("REPORTING_MAX_BODY_BYTES is too small")
        if self.production:
            if not self.app_db_url:
                problems.append("REPORTING_APP_DB_URL is required in production "
                                "(e.g. postgresql+psycopg://user:pass@host/reporting)")
            if not self.public_url:
                problems.append("REPORTING_PUBLIC_URL is required in production "
                                "(e.g. https://reports.example.com)")
            else:
                parts = urlsplit(self.public_url)
                local = parts.hostname in ("localhost", "127.0.0.1")
                if parts.scheme != "https" and not local:
                    problems.append("REPORTING_PUBLIC_URL must use https:// in production")
                if parts.path not in ("", "/") or parts.query:
                    problems.append("REPORTING_PUBLIC_URL must be an origin, without a path")
            if self.static_dir is None or not Path(self.static_dir, "index.html").exists():
                problems.append("The front end is not built (run scripts/build.ps1 or "
                                "scripts/build.sh), or set REPORTING_STATIC_DIR")
        if problems:
            raise ConfigError(problems)

    @property
    def sqlite_app_db(self) -> bool:
        return self.app_db_url.startswith("sqlite")
