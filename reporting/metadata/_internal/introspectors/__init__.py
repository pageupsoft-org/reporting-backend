"""Fixed, parameterised catalog queries per dialect.

Every dialect returns the same column shapes so one builder can assemble the snapshot:

* databases: ``name``
* tables:    ``schema_name, table_name, kind, row_estimate``
* columns:   ``schema_name, table_name, column_name, native_type, nullable, ordinal``
* keys:      ``constraint_name, kind ('p'|'f'), schema_name, table_name, column_name,
               ref_schema, ref_table, ref_column, position``

Queries that need the current database take a ``:db`` bind parameter.
"""

from __future__ import annotations

from dataclasses import dataclass

from reporting.core import Dialect
from reporting.metadata._internal.introspectors import mssql, mysql, postgresql, sqlite


@dataclass(frozen=True)
class CatalogQueries:
    databases: str
    tables: str
    columns: str
    keys: str
    row_counts: bool = False  # compute exact counts (SQLite has no estimate)


QUERIES: dict[Dialect, CatalogQueries] = {
    Dialect.POSTGRESQL: CatalogQueries(
        postgresql.DATABASES, postgresql.TABLES, postgresql.COLUMNS, postgresql.KEYS),
    Dialect.MSSQL: CatalogQueries(mssql.DATABASES, mssql.TABLES, mssql.COLUMNS, mssql.KEYS),
    Dialect.MYSQL: CatalogQueries(mysql.DATABASES, mysql.TABLES, mysql.COLUMNS, mysql.KEYS),
    Dialect.SQLITE: CatalogQueries(
        sqlite.DATABASES, sqlite.TABLES, sqlite.COLUMNS, sqlite.KEYS, row_counts=True),
}
