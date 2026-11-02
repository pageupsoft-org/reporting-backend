DATABASES = """
SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA
WHERE SCHEMA_NAME NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')
ORDER BY SCHEMA_NAME
"""

TABLES = """
SELECT NULL AS schema_name, TABLE_NAME AS table_name,
       CASE TABLE_TYPE WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind,
       TABLE_ROWS AS row_estimate
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = :db
"""

COLUMNS = """
SELECT NULL AS schema_name, TABLE_NAME AS table_name, COLUMN_NAME AS column_name,
       COLUMN_TYPE AS native_type, IS_NULLABLE = 'YES' AS nullable,
       ORDINAL_POSITION AS ordinal
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = :db
"""

KEYS = """
SELECT k.CONSTRAINT_NAME AS constraint_name,
       CASE WHEN k.CONSTRAINT_NAME = 'PRIMARY' THEN 'p' ELSE 'f' END AS kind,
       NULL AS schema_name, k.TABLE_NAME AS table_name, k.COLUMN_NAME AS column_name,
       NULL AS ref_schema, k.REFERENCED_TABLE_NAME AS ref_table,
       k.REFERENCED_COLUMN_NAME AS ref_column, k.ORDINAL_POSITION AS position
FROM information_schema.KEY_COLUMN_USAGE k
LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r
       ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
      AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
      AND r.TABLE_NAME = k.TABLE_NAME
WHERE k.TABLE_SCHEMA = :db
  AND (k.CONSTRAINT_NAME = 'PRIMARY'
       OR (r.CONSTRAINT_NAME IS NOT NULL AND k.REFERENCED_TABLE_SCHEMA = :db))
"""
