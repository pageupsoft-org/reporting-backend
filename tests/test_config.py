import base64
import os

import pytest

from reporting.app.config import ConfigError, Settings

KEY = base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("REPORTING_") or k == "REDIS_URL":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("REPORTING_DATA_DIR", str(tmp_path / "data"))
    return monkeypatch


def test_development_defaults_generate_local_secrets(env, tmp_path):
    s = Settings.from_env()
    assert not s.production and s.api_docs and s.sqlite_app_db
    assert (tmp_path / "data" / "master.key").exists()


def test_production_requires_everything(env):
    env.setenv("REPORTING_ENV", "production")
    env.setenv("REPORTING_STATIC_DIR", "/nonexistent")
    with pytest.raises(ConfigError) as exc:
        Settings.from_env()
    text = str(exc.value)
    for fragment in ("REPORTING_MASTER_KEY", "REPORTING_JWT_SECRET", "REPORTING_APP_DB_URL",
                     "REPORTING_PUBLIC_URL", "front end is not built"):
        assert fragment in text


def test_production_valid(env, tmp_path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<html></html>")
    env.setenv("REPORTING_ENV", "production")
    env.setenv("REPORTING_MASTER_KEY", KEY)
    env.setenv("REPORTING_JWT_SECRET", "s" * 48)
    env.setenv("REPORTING_APP_DB_URL", "postgresql+psycopg://u:p@db/reporting")
    env.setenv("REPORTING_PUBLIC_URL", "https://reports.example.com/")
    env.setenv("REPORTING_STATIC_DIR", str(static))
    s = Settings.from_env()
    assert s.production and s.https and not s.api_docs
    assert s.public_url == "https://reports.example.com"
    assert not (tmp_path / "data" / "master.key").exists()  # never generated in production
    env.setenv("REPORTING_PUBLIC_URL", "http://reports.example.com")
    with pytest.raises(ConfigError, match="https"):
        Settings.from_env()


def test_env_file_is_loaded_without_overriding(env, tmp_path):
    (tmp_path / ".env").write_text("REPORTING_CACHE_TTL=42\nREPORTING_SESSION_TTL=99\n")
    env.setenv("REPORTING_SESSION_TTL", "7")
    s = Settings.from_env()
    assert s.cache_ttl_s == 42 and s.session_ttl_s == 7


def test_bad_master_key(env):
    env.setenv("REPORTING_MASTER_KEY", "not-base64!")
    with pytest.raises(ConfigError, match="base64"):
        Settings.from_env()
