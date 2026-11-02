DATABASES = "SELECT 'main' AS name"

_USER_OBJECTS = "m.type IN ('table', 'view') AND m.name NOT LIKE 'sqlite_%'"

TABLES = f"""
SELECT NULL AS schema_name, m.name AS table_name, m.type AS kind, NULL AS row_estimate
FROM sqlite_master m
WHERE {_USER_OBJECTS}
"""

COLUMNS = f"""
SELECT NULL AS schema_name, m.name AS table_name, p.name AS column_name,
       p.type AS native_type,
       CASE WHEN p."notnull" = 1 OR p.pk > 0 THEN 0 ELSE 1 END AS nullable,
       p.cid AS ordinal
FROM sqlite_master m
JOIN pragma_table_info(m.name) p
WHERE {_USER_OBJECTS}
"""

KEYS = """
SELECT 'pk_' || m.name AS constraint_name, 'p' AS kind, NULL AS schema_name,
       m.name AS table_name, p.name AS column_name,
       NULL AS ref_schema, NULL AS ref_table, NULL AS ref_column, p.pk AS position
FROM sqlite_master m
JOIN pragma_table_info(m.name) p
WHERE m.type = 'table' AND p.pk > 0
UNION ALL
SELECT 'fk' || f.id || '_' || m.name AS constraint_name, 'f', NULL, m.name, f."from",
       NULL, f."table", f."to", f.seq + 1
FROM sqlite_master m
JOIN pragma_foreign_key_list(m.name) f
WHERE m.type = 'table'
"""
