DATABASES = """
SELECT name FROM sys.databases
WHERE database_id > 4 AND state = 0 AND HAS_DBACCESS(name) = 1
ORDER BY name
"""

TABLES = """
SELECT s.name AS schema_name, o.name AS table_name,
       CASE o.type WHEN 'V' THEN 'view' ELSE 'table' END AS kind,
       (SELECT SUM(p.rows) FROM sys.partitions p
        WHERE p.object_id = o.object_id AND p.index_id IN (0, 1)) AS row_estimate
FROM sys.objects o
JOIN sys.schemas s ON s.schema_id = o.schema_id
WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
"""

COLUMNS = """
SELECT s.name AS schema_name, o.name AS table_name, c.name AS column_name,
       t.name + CASE
         WHEN t.name IN ('varchar', 'char', 'varbinary', 'binary')
           THEN '(' + CASE WHEN c.max_length = -1 THEN 'max'
                      ELSE CAST(c.max_length AS varchar(10)) END + ')'
         WHEN t.name IN ('nvarchar', 'nchar')
           THEN '(' + CASE WHEN c.max_length = -1 THEN 'max'
                      ELSE CAST(c.max_length / 2 AS varchar(10)) END + ')'
         WHEN t.name IN ('decimal', 'numeric')
           THEN '(' + CAST(c.precision AS varchar(5)) + ',' + CAST(c.scale AS varchar(5)) + ')'
         ELSE '' END AS native_type,
       c.is_nullable AS nullable, c.column_id AS ordinal
FROM sys.columns c
JOIN sys.objects o ON o.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = o.schema_id
JOIN sys.types t ON t.user_type_id = c.user_type_id
WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
"""

KEYS = """
SELECT kc.name AS constraint_name, 'p' AS kind, s.name AS schema_name, t.name AS table_name,
       c.name AS column_name, NULL AS ref_schema, NULL AS ref_table, NULL AS ref_column,
       ic.key_ordinal AS position
FROM sys.key_constraints kc
JOIN sys.tables t ON t.object_id = kc.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE kc.type = 'PK'
UNION ALL
SELECT fk.name, 'f', s.name, t.name, c.name, rs.name, rt.name, rc.name, fkc.constraint_column_id
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables t ON t.object_id = fk.parent_object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.columns c ON c.object_id = fkc.parent_object_id AND c.column_id = fkc.parent_column_id
JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id
                   AND rc.column_id = fkc.referenced_column_id
"""
