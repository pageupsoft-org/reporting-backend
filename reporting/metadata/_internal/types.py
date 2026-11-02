"""Map native column types to the canonical ``DataType``."""

from __future__ import annotations

import re

from reporting.core import DataType, Dialect

_EXACT: dict[str, DataType] = {
    # integers
    "smallint": DataType.INTEGER, "integer": DataType.INTEGER, "int": DataType.INTEGER,
    "bigint": DataType.INTEGER, "tinyint": DataType.INTEGER, "mediumint": DataType.INTEGER,
    "int2": DataType.INTEGER, "int4": DataType.INTEGER, "int8": DataType.INTEGER,
    "serial": DataType.INTEGER, "bigserial": DataType.INTEGER, "year": DataType.INTEGER,
    # exact numerics
    "numeric": DataType.DECIMAL, "decimal": DataType.DECIMAL, "money": DataType.DECIMAL,
    "smallmoney": DataType.DECIMAL,
    # floats
    "real": DataType.FLOAT, "double precision": DataType.FLOAT, "double": DataType.FLOAT,
    "float": DataType.FLOAT, "float4": DataType.FLOAT, "float8": DataType.FLOAT,
    # text
    "text": DataType.TEXT, "character varying": DataType.TEXT, "varchar": DataType.TEXT,
    "character": DataType.TEXT, "char": DataType.TEXT, "nvarchar": DataType.TEXT,
    "nchar": DataType.TEXT, "ntext": DataType.TEXT, "citext": DataType.TEXT,
    "tinytext": DataType.TEXT, "mediumtext": DataType.TEXT, "longtext": DataType.TEXT,
    "enum": DataType.TEXT, "set": DataType.TEXT, "name": DataType.TEXT, "xml": DataType.TEXT,
    "sysname": DataType.TEXT, "clob": DataType.TEXT,
    # booleans
    "boolean": DataType.BOOLEAN, "bool": DataType.BOOLEAN, "bit": DataType.BOOLEAN,
    # temporal
    "date": DataType.DATE,
    "timestamp": DataType.DATETIME, "timestamp without time zone": DataType.DATETIME,
    "datetime": DataType.DATETIME, "datetime2": DataType.DATETIME,
    "smalldatetime": DataType.DATETIME,
    "timestamp with time zone": DataType.DATETIME_TZ, "timestamptz": DataType.DATETIME_TZ,
    "datetimeoffset": DataType.DATETIME_TZ,
    # misc
    "uuid": DataType.UUID, "uniqueidentifier": DataType.UUID,
    "json": DataType.JSON, "jsonb": DataType.JSON,
}

_PARAMS = re.compile(r"\(.*?\)")


def _sqlite_affinity(native: str) -> DataType:
    """SQLite type-affinity rules, refined for dates and booleans."""
    t = native.upper()
    if "BOOL" in t:
        return DataType.BOOLEAN
    if "DATETIME" in t or "TIMESTAMP" in t:
        return DataType.DATETIME
    if "DATE" in t:
        return DataType.DATE
    if "INT" in t:
        return DataType.INTEGER
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return DataType.TEXT
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return DataType.FLOAT
    if any(k in t for k in ("NUMERIC", "DECIMAL")):
        return DataType.DECIMAL
    return DataType.OTHER if t else DataType.TEXT


def map_native_type(dialect: Dialect, native: str) -> DataType:
    if dialect is Dialect.SQLITE:
        return _sqlite_affinity(native)
    base = _PARAMS.sub("", native.lower()).replace(" unsigned", "").strip()
    base = base.removesuffix("[]").strip() if not native.endswith("[]") else "array"
    if dialect is Dialect.MYSQL and native.lower().startswith("tinyint(1)"):
        return DataType.BOOLEAN
    if base.startswith("time ") or base == "time":
        return DataType.OTHER
    return _EXACT.get(base, DataType.OTHER)
