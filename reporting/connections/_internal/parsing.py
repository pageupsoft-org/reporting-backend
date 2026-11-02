"""Connection-string parsing with a per-dialect option allow-list.

Accepts URL form (``postgresql://user:pass@host:5432/db?sslmode=require``) for every dialect,
and ADO.NET form (``Server=host,1433;Database=db;User Id=u;Password=p``) for SQL Server.
Unknown options are rejected; known-dangerous ones get a specific message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy.engine import URL

from reporting.core import Dialect, Issue, ValidationFailed

DEFAULT_PORTS = {Dialect.POSTGRESQL: 5432, Dialect.MSSQL: 1433, Dialect.MYSQL: 3306}

ALLOWED_OPTIONS: dict[Dialect, set[str]] = {
    Dialect.POSTGRESQL: {
        "sslmode", "sslrootcert", "connect_timeout", "application_name", "target_session_attrs",
    },
    Dialect.MYSQL: {
        "ssl_ca", "ssl_verify_cert", "ssl_verify_identity", "charset", "connect_timeout",
    },
    Dialect.MSSQL: {
        "encrypt", "trustservercertificate", "applicationintent", "connection timeout",
        "multisubnetfailover", "driver", "trusted_connection",
    },
    Dialect.SQLITE: set(),
}

DANGEROUS_OPTIONS = {
    "local_infile": "LOCAL INFILE lets the server read files from this machine",
    "allow_local_infile": "LOCAL INFILE lets the server read files from this machine",
    "init_command": "init commands run arbitrary SQL on connect",
    "options": "server-side options can change session behaviour",
    "client_flag": "client flags can enable unsafe protocol features",
    "passfile": "password files are read from the server's filesystem",
    "sslkey": "client keys must be configured by an administrator",
}

_SCHEMES = {
    "postgresql": Dialect.POSTGRESQL, "postgres": Dialect.POSTGRESQL,
    "mysql": Dialect.MYSQL, "mariadb": Dialect.MYSQL,
    "mssql": Dialect.MSSQL, "sqlserver": Dialect.MSSQL,
    "sqlite": Dialect.SQLITE,
}

# ODBC spells these as yes/no; connection strings often use True/False or SSPI.
_MSSQL_BOOLEAN = {"encrypt", "trustservercertificate", "trusted_connection", "multisubnetfailover"}
_TRUE = {"true", "yes", "1", "sspi"}
_FALSE = {"false", "no", "0"}
_MSSQL_ALIASES = {"integrated security": "trusted_connection",
                  "trust server certificate": "trustservercertificate",
                  "multi subnet failover": "multisubnetfailover",
                  "application intent": "applicationintent",
                  "connect timeout": "connection timeout"}
# Best first; the first one installed on this machine is used.
MSSQL_DRIVERS = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                 "ODBC Driver 13 for SQL Server", "SQL Server Native Client 11.0", "SQL Server")


def mssql_driver() -> str:
    try:
        import pyodbc
    except ImportError:
        return MSSQL_DRIVERS[0]
    installed = set(pyodbc.drivers())
    return next((d for d in MSSQL_DRIVERS if d in installed), MSSQL_DRIVERS[0])


_ADO_KEYS = {
    "server": "host", "data source": "host", "address": "host", "addr": "host",
    "database": "database", "initial catalog": "database",
    "user id": "user", "uid": "user", "user": "user",
    "password": "password", "pwd": "password",
}


@dataclass(frozen=True)
class ParsedConnection:
    dialect: Dialect
    host: str | None
    port: int | None
    database: str | None
    user: str | None
    password: str | None
    options: dict[str, str] = field(default_factory=dict)

    @property
    def network_host(self) -> str | None:
        """The machine name without a SQL Server instance (``PC\\SQLEXPRESS`` → ``PC``)."""
        return self.host.split("\\", 1)[0] if self.host else None

    @property
    def instance(self) -> str | None:
        return self.host.split("\\", 1)[1] if self.host and "\\" in self.host else None

    @property
    def windows_auth(self) -> bool:
        return self.options.get("trusted_connection") == "yes"

    def sqlalchemy_url(self, database: str | None = None) -> URL:
        db = database or self.database
        if self.dialect is Dialect.SQLITE:
            return URL.create("sqlite", database=self.database)
        if self.dialect is Dialect.POSTGRESQL:
            return URL.create(
                "postgresql+psycopg", username=self.user, password=self.password,
                host=self.host, port=self.port, database=db, query=self.options,
            )
        if self.dialect is Dialect.MYSQL:
            return URL.create(
                "mysql+pymysql", username=self.user, password=self.password,
                host=self.host, port=self.port, database=db, query=self.options,
            )
        query = {"driver": mssql_driver(), **self.options}
        return URL.create(
            "mssql+pyodbc", username=self.user, password=self.password,
            host=self.host, port=self.port, database=db, query=query,
        )


def _fail(message: str) -> ValidationFailed:
    return ValidationFailed(message, [Issue(path="connection_string", message=message)])


def _check_options(dialect: Dialect, options: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for raw_key, value in options.items():
        key = raw_key.strip().lower()
        if key in DANGEROUS_OPTIONS:
            raise _fail(f"Option '{raw_key}' is not allowed: {DANGEROUS_OPTIONS[key]}")
        if dialect is Dialect.MSSQL:
            key = _MSSQL_ALIASES.get(key, key)
        if key not in ALLOWED_OPTIONS[dialect]:
            raise _fail(f"Option '{raw_key}' is not supported for {dialect.value}")
        if dialect is Dialect.MSSQL and key in _MSSQL_BOOLEAN:
            low = value.strip().lower()
            if low in _TRUE:
                value = "yes"
            elif low in _FALSE:
                value = "no"
            elif not (key == "encrypt" and low in ("mandatory", "optional", "strict")):
                raise _fail(f"Option '{raw_key}' must be True or False")
        clean[key] = value
    return clean


def _parse_ado(text: str) -> ParsedConnection:
    values: dict[str, str] = {}
    options: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in text.split(";"))):
        if "=" not in part:
            raise _fail(f"Malformed segment '{part}'")
        key, _, value = part.partition("=")
        norm = key.strip().lower()
        if norm in _ADO_KEYS:
            values[_ADO_KEYS[norm]] = value.strip()
        else:
            options[norm] = value.strip()
    host = values.get("host", "").strip()
    host = host.removeprefix("tcp:")
    port: int | None = None
    if "," in host:
        host, _, port_text = host.partition(",")
        port = _port(port_text.strip())
    if not host:
        raise _fail("Server is required")
    machine, sep, instance = host.partition("\\")
    if machine.lower() in (".", "(local)", "(localdb)"):
        machine = "localhost"
    host = machine + sep + instance
    if port is None and not instance:
        port = DEFAULT_PORTS[Dialect.MSSQL]  # named instances find their port via SQL Browser
    options = _check_options(Dialect.MSSQL, options)
    if options.get("trusted_connection") == "yes" and values.get("user"):
        raise _fail("Use either Trusted_Connection (Windows login) or User Id/Password, not both")
    return ParsedConnection(
        dialect=Dialect.MSSQL, host=host, port=port, database=values.get("database"),
        user=values.get("user"), password=values.get("password"), options=options,
    )


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise _fail(f"Port '{text}' is not a number") from None
    if not 0 < port < 65536:
        raise _fail(f"Port {port} is out of range")
    return port


def parse_connection_string(dialect: Dialect, text: str) -> ParsedConnection:
    text = text.strip()
    if not text:
        raise _fail("Connection string is empty")
    if "://" not in text:
        if dialect is Dialect.MSSQL:
            return _parse_ado(text)
        raise _fail("Expected a URL such as postgresql://user:password@host:5432/database")

    parts = urlsplit(text)
    scheme = parts.scheme.split("+", 1)[0].lower()
    if _SCHEMES.get(scheme) is not dialect:
        raise _fail(f"Scheme '{parts.scheme}' does not match dialect {dialect.value}")

    if dialect is Dialect.SQLITE:
        path = unquote(text.split("://", 1)[1])
        path = path[1:] if path.startswith("/") else path
        if not path or path == ":memory:":
            raise _fail("SQLite needs a database file path, e.g. sqlite:///data/demo.db")
        return ParsedConnection(Dialect.SQLITE, None, None, path, None, None)

    if not parts.hostname:
        raise _fail("Host is required")
    try:
        raw_port = parts.port
    except ValueError:
        raise _fail("Port is not valid") from None
    database = unquote(parts.path.lstrip("/")) or None
    return ParsedConnection(
        dialect=dialect,
        host=parts.hostname,
        port=raw_port or DEFAULT_PORTS[dialect],
        database=database,
        user=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password else None,
        options=_check_options(dialect, dict(parse_qsl(parts.query))),
    )
