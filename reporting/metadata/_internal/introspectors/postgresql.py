DATABASES = """
SELECT datname AS name FROM pg_database
WHERE NOT datistemplate AND datallowconn
ORDER BY datname
"""

_SYSTEM = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'"

TABLES = f"""
SELECT n.nspname AS schema_name, c.relname AS table_name,
       CASE WHEN c.relkind IN ('v', 'm') THEN 'view' ELSE 'table' END AS kind,
       GREATEST(c.reltuples, 0)::bigint AS row_estimate
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm') AND {_SYSTEM}
"""

COLUMNS = f"""
SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name,
       CASE t.typtype  -- enums read as "enum(name)" (text); domains as their base type
         WHEN 'e' THEN 'enum(' || format_type(a.atttypid, NULL) || ')'
         WHEN 'd' THEN format_type(t.typbasetype, t.typtypmod)
         ELSE format_type(a.atttypid, a.atttypmod) END AS native_type,
       NOT a.attnotnull AS nullable, a.attnum AS ordinal
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'v', 'm') AND {_SYSTEM}
"""

KEYS = f"""
SELECT con.conname AS constraint_name, con.contype::text AS kind,
       n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name,
       rn.nspname AS ref_schema, rc.relname AS ref_table, ra.attname AS ref_column,
       k.ord AS position
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
LEFT JOIN pg_class rc ON rc.oid = con.confrelid
LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace
LEFT JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = con.confkey[k.ord]
WHERE con.contype IN ('p', 'f') AND {_SYSTEM}
"""
