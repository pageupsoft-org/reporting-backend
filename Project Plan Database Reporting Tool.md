# Project Plan: Database Reporting Tool

Oct 28, 2026 · @Reetesh Pageup

## 1. Architecture

The tool ships as one Python service with five internal modules (`connections`, `metadata`, `query`, `render`, `auth`) whose dependencies point one way only, enforced in CI so a violation fails the build.

&#91;embedded content: module map · allowed dependencies between the five modules\]

Arrows are the only allowed calls: `auth` and `render` are reached only from the HTTP layer, and only `connections` touches customer databases.

### 1.1 Module rules

1. Each module exposes exactly one public entry point, `<module>/api.py`, re-exported from `<module>/__init__.py`. Everything else lives under `<module>/_internal/` and is private.
2. Modules exchange only immutable data objects (Pydantic models or frozen dataclasses) defined in their own `api.py` or in the shared `core` package. No ORM objects, SQLAlchemy engines, cursors or sessions cross a module boundary.
3. Dependencies are one-way: `query` may call `metadata` and `connections`; `metadata` may call `connections`; `render`, `auth` and `connections` call no other module. The HTTP layer (`app/routes`) orchestrates the modules and is the only place that calls `auth`.
4. Each module owns its own tables in the application database, prefixed by module name (`connections_*`, `metadata_*`). No module reads another module's tables.
5. Errors cross boundaries only as typed exceptions from `core.errors` (`NotFound`, `ValidationFailed`, `PermissionDenied`, `ConnectionFailed`, `QueryTimeout`), which the HTTP layer maps to status codes.

### 1.2 Package layout

```text
reporting/
  core/            shared kernel: ids, errors, Principal, DataType enum
  connections/
    __init__.py    re-exports api
    api.py         ConnectionService protocol + DTOs
    _internal/     vault, engine pool, dialect drivers
  metadata/        api.py + _internal/ (introspectors per dialect, snapshot cache)
  query/           api.py + _internal/ (spec models, validator, planner, compiler)
  render/          api.py + _internal/ (pandas shaping, chart and table builders)
  auth/            api.py + _internal/ (users, roles, policies, sessions)
  app/             FastAPI app, routes, dependency wiring
```

### 1.3 Enforcement

- **import-linter** in CI with three contracts: a *layers* contract (`app` → `query` → `metadata` → `connections`), an *independence* contract (`render`, `auth`, `connections` import no sibling modules), and a *forbidden* contract banning any import of `*._internal` from outside its own module.
- **Protocol-typed wiring:** each `api.py` defines a `typing.Protocol`; `app/wiring.py` builds the concrete implementations and injects them, so a module can be tested with a fake of any dependency.
- **Architecture tests** (pytest) assert that no public function returns or accepts a SQLAlchemy, pandas-internal or driver type except where the contract names it.

## 2. Module interface contracts

Each contract below is the complete public surface of its module, written as a `typing.Protocol`; anything not listed is private. All DTOs are frozen Pydantic v2 models, and every method raises only `core.errors` types.

### 2.1 `connections` — owns credentials and is the only module that talks to customer databases

```python
class ConnectionService(Protocol):
    def create(self, owner: UserId, req: CreateConnection) -> ConnectionInfo: ...
    def update(self, connection_id: ConnectionId, req: UpdateConnection) -> ConnectionInfo: ...
    def delete(self, connection_id: ConnectionId) -> None: ...
    def list(self, ids: set[ConnectionId] | None = None) -> list[ConnectionInfo]: ...
    def test(self, target: CreateConnection | ConnectionId) -> ConnectionTestResult: ...
    def dialect(self, connection_id: ConnectionId) -> Dialect: ...
    def execute_read(self, connection_id: ConnectionId, stmt: CompiledStatement,
                     limits: ExecutionLimits) -> ResultTable: ...
```

- `CreateConnection` holds `name`, `dialect` (`postgresql` | `mssql` | `mysql`) and the connection string as a `SecretStr`. `ConnectionInfo` never contains the password.
- `CompiledStatement` (from `core`) is SQL text with named bind placeholders plus a parameter dict. Only `query` and `metadata` construct it.
- `execute_read` always runs in a read-only transaction with a statement timeout and row cap from `ExecutionLimits`, and returns `ResultTable` (column descriptors + a pandas DataFrame + `truncated` flag + `elapsed_ms`).

### 2.2 `metadata` — introspects schemas and answers relationship questions

```python
class MetadataService(Protocol):
    def list_databases(self, connection_id: ConnectionId) -> list[DatabaseRef]: ...
    def list_tables(self, db: DatabaseRef) -> list[TableSummary]: ...
    def get_table(self, table: TableRef) -> TableDetail: ...
    def related_tables(self, table: TableRef) -> list[Relationship]: ...
    def find_join_paths(self, source: TableRef, target: TableRef,
                        max_hops: int = 3) -> list[JoinPath]: ...
    def snapshot(self, connection_id: ConnectionId) -> SchemaSnapshot: ...
    def refresh(self, connection_id: ConnectionId) -> SnapshotVersion: ...
```

- `TableDetail` lists `ColumnInfo` (name, native type, canonical `DataType`, nullable, primary-key flag) plus outgoing and incoming `Relationship`s.
- `Relationship` carries constraint name, both tables, column pairs (composite keys supported), `direction` (`outgoing` | `incoming`), `cardinality` and `source` (`declared` | `inferred` | `user`).
- `SchemaSnapshot` is immutable and versioned; `query` validates against a specific version, so a refresh mid-request cannot change the rules.

### 2.3 `query` — validates specs and compiles and runs them

```python
class QueryService(Protocol):
    def validate(self, spec: QuerySpec) -> ValidationReport: ...
    def compile(self, spec: QuerySpec, access: AccessContext) -> CompiledQuery: ...
    def execute(self, spec: QuerySpec, access: AccessContext,
                limits: ExecutionLimits) -> QueryResult: ...
    def preview(self, spec: QuerySpec, access: AccessContext) -> QueryResult: ...
```

- `ValidationReport` lists errors with a JSON path into the spec (for example `columns[3].expr`) so the UI can point at the exact problem.
- `CompiledQuery` = `CompiledStatement` + dialect + snapshot version + output column descriptors; the SQL text is shown read-only in "View SQL".
- `QueryResult` = DataFrame + `OutputColumn` list (alias, label, `DataType`, role `dimension` | `measure`) + row count, `truncated`, `elapsed_ms`, `cache_hit`.

### 2.4 `render` — turns results into display payloads with pandas

```python
class RenderService(Protocol):
    def table(self, result: QueryResult, opts: TableOptions) -> TablePayload: ...
    def chart(self, result: QueryResult, spec: ChartSpec) -> ChartPayload: ...
    def suggest_charts(self, result: QueryResult) -> list[ChartSpec]: ...
    def export(self, result: QueryResult, fmt: ExportFormat) -> ExportFile: ...
```

- `render` imports no other module; it receives `QueryResult` from the HTTP layer. Payloads are plain JSON (a `ChartPayload` is an Apache ECharts option object).

### 2.5 `auth` — identity, permissions and column policies

```python
class AuthService(Protocol):
    def authenticate(self, token: str) -> Principal: ...
    def authorize(self, principal: Principal, action: Action, resource: ResourceRef) -> None: ...
    def visible_connections(self, principal: Principal) -> set[ConnectionId]: ...
    def access_context(self, principal: Principal, connection_id: ConnectionId) -> AccessContext: ...
```

- `AccessContext` (in `core`) lists the tables and columns the user may see. The HTTP layer passes it into `query`, which rejects any spec that references a hidden column, so `query` never calls `auth` directly.

## 3. Query model

The client sends a typed JSON spec that names tables, relationships and columns but never SQL; the server validates every reference against a versioned schema snapshot and compiles the spec with SQLAlchemy Core into parameterised SQL for the target dialect.

### 3.1 The query spec

```json
{
  "version": 1,
  "connection_id": "conn_7f3a",
  "snapshot_version": 42,
  "base": {"alias": "o", "table": {"database": "sales", "schema": "public", "name": "orders"}},
  "joins": [
    {"alias": "c", "from_alias": "o", "relationship_id": "fk_orders_customer_id", "type": "left"}
  ],
  "columns": [
    {"id": "country", "kind": "field", "ref": "c.country", "label": "Country"},
    {"id": "revenue", "kind": "aggregate", "fn": "sum", "arg": "o.total_amount", "label": "Revenue"},
    {"id": "orders", "kind": "aggregate", "fn": "count", "arg": "o.id", "label": "Orders"},
    {"id": "avg_ship", "kind": "aggregate", "fn": "avg",
     "arg": {"kind": "date_diff", "unit": "day", "start": "o.ordered_at", "end": "o.shipped_at"},
     "label": "Avg days to ship"},
    {"id": "aov", "kind": "expression", "expr": "[revenue] / NULLIF([orders], 0)", "label": "Avg order value"}
  ],
  "filters": [
    {"ref": "o.status", "op": "in", "value": ["paid", "shipped"]},
    {"ref": "o.ordered_at", "op": "between", "value": ["2026-01-01", "2026-09-30"]}
  ],
  "having": [{"ref": "revenue", "op": "gt", "value": 10000}],
  "group_by": "auto",
  "order_by": [{"ref": "revenue", "dir": "desc"}],
  "limit": 1000
}
```

Key design choices:

- **Joins reference a relationship id, never an ON clause.** The compiler builds the join condition from the relationship's column pairs in the snapshot, so a client cannot invent a join.
- **`group_by: "auto"`** groups by every non-aggregated column (the behaviour users expect); an explicit list is accepted and checked for consistency.
- **Field refs are `alias.column`**; calculated columns are referenced by their `id` in square brackets inside expressions and in `having` and `order_by`.
- **Columns are a discriminated union on `kind`** (`field`, `aggregate`, `date_diff`, `expression`), so Pydantic rejects unknown shapes before any custom logic runs.

### 3.2 Validation pipeline

| Step | Check | Example error |
| --- | --- | --- |
| 1. Shape | Pydantic v2 with `extra="forbid"`; size limits (≤ 50 columns, ≤ 8 joins, ≤ 30 filters, expression ≤ 500 characters) | `columns[2].fn: 'median' is not a supported aggregate` |
| 2. Snapshot | `snapshot_version` matches the current snapshot; if stale, return 409 with the schema diff | `Schema changed: column orders.total_amount was removed` |
| 3. References | Every table, column and relationship exists; aliases are unique; each join's relationship connects `from_alias` to its table | `joins[0]: relationship does not connect o to customers` |
| 4. Access | Every referenced table and column is allowed by the `AccessContext` | `c.email is not visible to your role` |
| 5. Types | Aggregate vs column type (Sum and Avg need numeric; Min and Max need orderable; Count accepts any); date diff needs two date or datetime columns; filter values are coerced to the column's type | `Sum needs a number; c.country is text` |
| 6. Semantics | Group-by consistency; nested aggregates rejected; fan-out detection when a measure from the "one" side is aggregated across a one-to-many join | `Revenue would be multiplied by joining order_items; aggregate before joining?` |
| 7. Expressions | Parsed and type-checked as described in section 4 | `aov: unknown function 'EXEC'` |

Validation runs on every builder change (debounced) so the UI shows errors inline before the user clicks Run.

### 3.3 Compilation

1. Build lightweight SQLAlchemy `table()` and `column()` constructs from the snapshot, so every identifier is quoted by the dialect.
2. Resolve joins from relationships into `join(...).onclause` expressions.
3. Translate each column into a SQLAlchemy expression: `func.sum`, `func.count`, the custom `DateDiff` construct (section 4), or the compiled expression tree.
4. Wrap every user value in `bindparam`; literals are never inlined.
5. Compile with `stmt.compile(dialect=...)` for `postgresql`, `mssql` or `mysql`, producing a `CompiledStatement` of SQL text plus parameters.

For PostgreSQL, the example spec compiles to:

```sql
SELECT c."country" AS country,
       SUM(o."total_amount") AS revenue,
       COUNT(o."id") AS orders,
       AVG(CAST(o."shipped_at" AS DATE) - CAST(o."ordered_at" AS DATE)) AS avg_ship,
       SUM(o."total_amount") / NULLIF(COUNT(o."id"), 0) AS aov
FROM "public"."orders" AS o
LEFT OUTER JOIN "public"."customers" AS c ON c."id" = o."customer_id"
WHERE o."status" IN (%(p1_1)s, %(p1_2)s)
  AND o."ordered_at" BETWEEN %(p2)s AND %(p3)s
GROUP BY c."country"
HAVING SUM(o."total_amount") > %(p4)s
ORDER BY revenue DESC
LIMIT %(p5)s
```

### 3.4 Execution and data preparation with pandas

- `query.execute` sends the `CompiledStatement` to `connections.execute_read`, which enforces read-only mode, timeout and row cap, and returns a DataFrame.
- `query` normalises types on the DataFrame: `Decimal` and BIGINT to exact string or `Int64` columns, timezone-aware datetimes to UTC, and driver-specific booleans to `bool`.
- `render` uses pandas for shaping only: pivoting for crosstabs, top-N plus "Other" for pie charts, percent-of-total, time-series resampling and LTTB downsampling for line charts, null labelling and number and date formatting.
- **Rule:** aggregation happens in SQL, never in pandas. pandas works only on result sets already bounded by the row cap (default 100,000 rows), so memory stays predictable.
- Results are cached in Redis keyed by a hash of (SQL, parameters, connection, snapshot version), with a per-report TTL and a manual refresh.

## 4. Calculated and link columns

Calculated columns come in three kinds (aggregates, date differences and custom expressions), and all three compile to SQLAlchemy expression objects, never to SQL strings, so user input can only ever become a bound parameter or a reference to a column that exists in the snapshot. Link columns are fields from FK-related tables, added through an automatically resolved join (section 7.4).

### 4.1 Aggregations

| UI label | Spec `fn` | Accepts | SQL | Dialect note |
| --- | --- | --- | --- | --- |
| Avg | `avg` | numeric | `AVG(x)` | SQL Server returns an integer for `AVG(int)`, so the compiler casts the argument to `DECIMAL(38,6)` first |
| Sum / Total | `sum` | numeric | `SUM(x)` | Result type widened to avoid overflow on `INT` |
| Count | `count` | any | `COUNT(x)`; `COUNT(*)` for row count | `count_distinct` variant compiles to `COUNT(DISTINCT x)` |
| Min | `min` | number, date, text | `MIN(x)` | Text ordering follows the column's collation |
| Max | `max` | number, date, text | `MAX(x)` | As Min |

An aggregate's argument may be a field, a date difference or a row-level expression, but not another aggregate.

### 4.2 Date arithmetic: day difference

`date_diff` returns the number of calendar-day boundaries between `start` and `end` (time of day ignored), positive when `end` is later. The meaning is identical on all three engines:

| Dialect | Compiled SQL |
| --- | --- |
| PostgreSQL | `CAST(end AS DATE) - CAST(start AS DATE)` |
| SQL Server | `DATEDIFF(day, start, end)` |
| MySQL | `DATEDIFF(end, start)` (note the reversed argument order) |

It is implemented as a custom SQLAlchemy construct, `class DateDiff(ColumnElement)`, with one `@compiles(DateDiff, "<dialect>")` function per engine and a golden test asserting the same result on seeded data in all three. Units `week`, `month` and `year` are a later extension.

### 4.3 Custom expressions

Users type formulas such as `(o.total_amount - o.discount) * 1.18` or `[revenue] / NULLIF([orders], 0)`. These go through a dedicated pipeline: parse with our own grammar, type-check, then compile node by node. The expression text is never passed to the database, to `eval`, or to a general SQL parser.

**Grammar** (implemented with Lark in LALR mode; anything outside it is a parse error):

```text
expr       := or_expr
or_expr    := and_expr ("OR" and_expr)*
and_expr   := not_expr ("AND" not_expr)*
not_expr   := "NOT" not_expr | comparison
comparison := additive (("=" | "<>" | "<" | "<=" | ">" | ">=") additive)?
additive   := term (("+" | "-") term)*
term       := factor (("*" | "/") factor)*
factor     := "-" factor | primary
primary    := NUMBER | STRING | "TRUE" | "FALSE" | "NULL"
            | field_ref | calc_ref | func_call | case_expr | "(" expr ")"
field_ref  := IDENT "." IDENT          -- alias.column, e.g. o.total_amount
calc_ref   := "[" IDENT "]"             -- another calculated column, e.g. [revenue]
func_call  := FUNC_NAME "(" (expr ("," expr)*)? ")"
case_expr  := "CASE" ("WHEN" expr "THEN" expr)+ ("ELSE" expr)? "END"
```

**Allowed functions** (the complete list; each maps to a SQLAlchemy builder with a typed signature):

| Group | Functions |
| --- | --- |
| Aggregate | `SUM`, `AVG`, `COUNT`, `COUNT_DISTINCT`, `MIN`, `MAX` |
| Numeric | `ABS`, `ROUND(num, digits)`, `FLOOR`, `CEILING` |
| Null handling | `COALESCE`, `NULLIF` |
| Text | `UPPER`, `LOWER`, `TRIM`, `LENGTH`, `CONCAT` |
| Date | `YEAR`, `MONTH`, `DAY`, `DATE_DIFF_DAYS(start, end)` |

**Validation steps:**

1. **Limits:** at most 500 characters, 100 AST nodes and depth 10, checked before and after parsing.
2. **Parse** into a typed AST (`BinaryOp`, `FuncCall`, `FieldRef`, `CalcRef`, `Literal`, `Case`).
3. **Resolve references:** each `FieldRef` must match an alias and column in the spec and snapshot and be allowed by the `AccessContext`; each `CalcRef` must name another calculated column. Calculated-column references are topologically sorted and cycles are rejected.
4. **Type-check** bottom-up: arithmetic needs numbers, comparisons need compatible types, function arguments must match signatures, `CASE` branches must share a type. The inferred result type becomes the column's `DataType`.
5. **Aggregation level:** an expression is either row-level (no aggregates) or aggregate-level. Any bare field in an aggregate-level expression must also be a grouped column, which matches SQL's own rule and prevents dialect-specific errors.

**Compilation:** each AST node maps to one SQLAlchemy construct: `FieldRef` → the snapshot's quoted column object, `CalcRef` → the referenced column's already-compiled expression, `FuncCall` → the allow-listed builder, `Literal` → `bindparam` with the inferred type, `BinaryOp` → the Python operator on SQLAlchemy objects. Because identifiers are looked up rather than copied, and literals are parameters, there is no path for injected SQL to reach the database.

**Testing:** property-based fuzzing (Hypothesis) generates random strings and random valid ASTs; the test asserts that every input either fails validation cleanly or compiles to SQL containing only snapshot identifiers, allow-listed function names and bind placeholders.

## 5. Technology stack

FastAPI is the recommended framework because its Pydantic-native request models are the query spec's validation layer, it generates an OpenAPI contract the front end can type against, and its dependency injection fits the module wiring.

### 5.1 Framework choice

| Framework | Fit for this product | Verdict |
| --- | --- | --- |
| **FastAPI** | Pydantic v2 models double as the spec schema (discriminated unions, `extra="forbid"`); auto-generated OpenAPI lets the React client generate TypeScript types; `Depends()` injects module services and the `Principal`; async for I/O, thread pool for blocking drivers | **Recommended** |
| Django + DRF | Strong admin, auth and ORM, but the ORM is built for a known schema, and serializers duplicate the Pydantic spec | Viable if the team is Django-native |
| Litestar | Similar to FastAPI with stricter typing; smaller ecosystem and hiring pool | Credible alternative |
| Flask | Minimal; validation, OpenAPI and DI must be assembled by hand | Not recommended |

One caution: the SQL Server ODBC driver (`pyodbc`) is synchronous. Run customer queries in sync SQLAlchemy engines on a bounded thread pool for previews, and in background workers for full runs, rather than forcing an all-async stack.

### 5.2 Libraries

| Concern | Choice | Module |
| --- | --- | --- |
| SQL building and dialects | SQLAlchemy 2.x Core | `query`, `metadata` |
| Drivers | psycopg 3 (PostgreSQL), pyodbc + Microsoft ODBC Driver 18 (SQL Server), PyMySQL or mysqlclient (MySQL) | `connections` |
| Expression parser | Lark (LALR) | `query` |
| Data preparation | pandas 2.x with the PyArrow backend | `query` (type normalisation), `render` |
| Excel export | openpyxl via `DataFrame.to_excel` | `render` |
| App database and migrations | PostgreSQL, SQLAlchemy ORM, Alembic | all (own tables) |
| Cache and job queue | Redis; Dramatiq or Celery workers | `query` |
| Secrets | `cryptography` AES-256-GCM, keys wrapped by a cloud KMS or HashiCorp Vault | `connections` |
| Auth | OIDC login via Authlib; JWT sessions | `auth` |
| Quality | Ruff, mypy (strict), import-linter, pytest, Hypothesis, Testcontainers | CI |
| Front end | React + TypeScript, TanStack Query, Radix / shadcn/ui, dnd-kit, Apache ECharts | client |

## 6. Metadata and connections

Users can register any number of connections, and each connection can expose several databases; `metadata` turns each into a versioned snapshot of tables, columns, types and foreign keys that the UI browses and `query` validates against.

### 6.1 Connection lifecycle (`connections`)

| Stage | What happens |
| --- | --- |
| Create | User picks PostgreSQL, SQL Server or MySQL and pastes a connection string. It is parsed into host, port, database, user, password and an allow-list of options; unknown or dangerous options (for example MySQL `local_infile`) are rejected. |
| Test | Stepwise check with a specific message for each failure: DNS, TCP, TLS, login, database access, and whether the login can write (warn if so). |
| Store | Password encrypted with AES-256-GCM under a per-tenant data key wrapped by KMS; only `ConnectionInfo` (no secret) is ever returned. |
| Pool | One SQLAlchemy engine per (connection, database), created lazily, small pool (default 5), idle engines disposed after 15 minutes. |
| Rotate / update | New credentials are tested before replacing the old ones; engines are disposed and rebuilt. |
| Delete | Secret destroyed, engines disposed, `metadata` snapshots and dependent saved reports flagged. |

**Multiple databases:** SQL Server and MySQL list several databases on one server, so a connection can expose more than one; PostgreSQL needs a separate engine per database, created on demand from the same credentials. Joins across databases are out of scope for v1, so a report targets one database.

### 6.2 Introspection (`metadata`)

`metadata` runs fixed, parameterised catalog queries (written per dialect inside `metadata/_internal/introspectors/`) through `connections.execute_read`, one batched query per object kind per database, so a 2,000-table schema takes a handful of round trips rather than thousands.

| Object | PostgreSQL | SQL Server | MySQL |
| --- | --- | --- | --- |
| Databases | `pg_database` | `sys.databases` | `information_schema.SCHEMATA` |
| Tables and views | `pg_class` + `pg_namespace` | `sys.tables`, `sys.views`, `sys.schemas` | `information_schema.TABLES` |
| Columns and types | `pg_attribute` + `format_type()` | `sys.columns` + `sys.types` | `information_schema.COLUMNS` |
| Primary and foreign keys | `pg_constraint` | `sys.key_constraints`, `sys.foreign_keys`, `sys.foreign_key_columns` | `KEY_COLUMN_USAGE` + `REFERENTIAL_CONSTRAINTS` |
| Row estimates | `pg_class.reltuples` | `sys.partitions` | `TABLES.TABLE_ROWS` |

Native types map to a canonical `DataType` (`integer`, `decimal`, `float`, `text`, `boolean`, `date`, `datetime`, `datetime_tz`, `uuid`, `json`, `other`), which drives which aggregates, operators and expressions the builder offers.

### 6.3 Snapshots and relationships

- **Snapshots** are stored as versioned JSON in `metadata_snapshots`, refreshed manually, nightly, or automatically when a query hits a missing object. Each refresh is diffed against the previous version, and saved reports that reference removed or retyped columns are flagged.
- **Relationship graph:** foreign keys become an undirected graph with typed edges. `related_tables` returns one hop in both directions (for sidebar highlighting); `find_join_paths` runs breadth-first search up to 3 hops and returns all shortest paths, so the UI can ask the user when there is more than one.
- **Inferred relationships:** where no FK is declared, columns named `<table>_id` or `<table>id` whose type matches a primary key become `source: inferred` candidates, shown dashed in the UI until an admin confirms them. Admins can also add manual relationships (`source: user`).

## 7. UI requirements

The screen has three regions: a collapsible sidebar for picking a database and table, a column panel for the selected table and its related tables, and the report canvas where columns and calculated columns are assembled and previewed.

&#91;embedded content: wireframe · sidebar, column panel and report canvas\]

With `orders` selected, `customers` (which it references) and `order_items` (which references it) are highlighted, and a selected column offers "Add to report analysis".

### 7.1 Collapsible sidebar

- A connection switcher at the top, then a database selector, a table search box, and tables grouped by schema with row-count hints.
- Collapses to a 56 px icon rail with a toggle button or `Ctrl/Cmd + B`; the state is remembered per user.
- Long lists are virtualised and fully keyboard-navigable (arrow keys, type-ahead, `Enter` to select).

### 7.2 FK highlighting

When a table is selected, the sidebar calls `metadata.related_tables` and highlights every table one FK hop away, in both directions:

| State | Visual treatment | Meaning |
| --- | --- | --- |
| Selected | Accent background and bold name | The current table |
| Outgoing | Accent outline + "→ references" icon, tooltip naming the FK column (for example `orders.customer_id → customers.id`) | The selected table points to this table |
| Incoming | Accent outline + "← referenced by" icon and tooltip | This table points to the selected table |
| Inferred | Same as above with a dashed outline and "inferred" label | Relationship guessed from naming, not declared |

Related tables are also pinned in a "Related" group at the top of the list, and a "Show related only" toggle filters the rest out. Meaning is never carried by colour alone: icons and text labels are always present, and screen readers announce "customers, referenced by orders".

### 7.3 Column selection

- Selecting a table opens the column panel: name, type icon, PK and FK badges, nullability.
- Selecting a column reveals an **Add to report analysis** button on that row; the same action is in the right-click menu, on `Enter`, and by dragging the column onto the canvas. `Shift`/`Ctrl` multi-select adds several at once.
- Columns already in the report show a check mark; hidden or masked columns (per `AccessContext`) do not appear.

### 7.4 Related-table (link) columns

- Below the selected table's own columns, the panel lists **Columns from related tables**, grouped as expandable sections such as "customers (via customer\_id)".
- Adding one calls `metadata.find_join_paths`. With a single shortest path the join is added silently and a toast confirms it ("Joined customers via orders.customer\_id"); with several, a popover asks which path to use.
- Tables further away can be added from search; the path appears as a breadcrumb ("orders → customers → regions").
- The canvas header shows the active joins as chips; clicking one shows its path and lets the user switch to an alternative or change inner/left join. Removing the last column from a joined table removes the join.
- If a measure would be multiplied by a one-to-many join (fan-out), the column card shows a warning with a suggested fix.

### 7.5 Report canvas and calculated columns

- Added columns appear as cards in a horizontal strip, reorderable by drag or keyboard. Each card shows its editable label, source path (`customers.country`), type and role (dimension or measure), and a menu for **aggregate** (Avg, Sum / Total, Count, Min, Max), sort, filter and remove.
- **Add calculated column** opens a dialog with three tabs:
  - **Aggregation:** pick a function and a column; only type-compatible choices are enabled.
  - **Date difference:** pick start and end date columns; shows a sample result.
  - **Custom expression:** a code editor with autocomplete for `alias.column`, `[calculated]` names and allowed functions; errors from `/validate` are underlined in place, and the inferred result type is shown live.
- Below the strip: a filter bar, a 100-row live preview that refreshes on each change, a **View SQL** toggle (read-only), **Run**, and Table / Chart tabs.

### 7.6 UI actions and the modules behind them

| UI action | Endpoint | Modules called by the HTTP layer |
| --- | --- | --- |
| Open sidebar | `GET /connections`, `GET /connections/{id}/databases` | `auth`, `connections`, `metadata` |
| Select table | `GET /tables/{ref}` (columns + relationships) | `auth`, `metadata` |
| Add related column | `GET /join-paths?from=&to=` | `metadata` |
| Edit report | `POST /query/validate` | `auth`, `query` |
| Preview / Run | `POST /query/preview`, `POST /query/run` | `auth`, `query`, then `render` |
| Chart | `POST /render/chart` | `render` |
| Export | `POST /render/export` | `auth`, `query`, `render` |

## 8. Delivery plan

A team of five delivers a beta in 20 weeks (10 two-week sprints), with a working end-to-end report by week 12 and the riskiest pieces (join resolution and the expression compiler) built and tested before the polish work.

&#91;embedded content: delivery timeline · 5 phases, 4 milestones over 20 weeks\]

| Milestone | Week | Exit criteria |
| --- | --- | --- |
| M1 Browse schemas | 6 | Connect to PostgreSQL, SQL Server and MySQL; sidebar lists databases and tables; FK highlighting correct on the seeded test schemas; credentials encrypted and never returned |
| M2 First report | 12 | Add own and related-table columns; joins resolved automatically with ambiguity prompts; spec validated and compiled on all three engines; 100-row preview and View SQL work |
| M3 Formulas done | 16 | Avg, Sum, Count, Min, Max, day difference and custom expressions validated inline; 10,000+ fuzz cases pass; identical results across engines on golden data |
| M4 Beta | 20 | Table and chart views, CSV and Excel export, caching, roles; external security review closed; p95 preview under 2 s on the benchmark schema |

### 8.1 Scope by phase

| Phase (weeks) | Back end (Python) | Front end (React) |
| --- | --- | --- |
| Foundations (1–2) | Repo layout, import-linter contracts, CI with Testcontainers for all three engines, `auth` skeleton | Design tokens, app shell, API client generated from OpenAPI |
| Connections and metadata (3–6) | `connections` vault, test and pools; `metadata` introspectors, snapshots, relationship graph | Collapsible sidebar, FK highlighting, column panel |
| Query core and builder (7–12) | Spec models, validator, join-path search, SQLAlchemy compiler, preview endpoint | Add to report analysis, related-table columns, canvas, join chips, preview grid |
| Calculated columns (13–16) | Aggregates, `DateDiff` construct, Lark grammar, type checker, fuzz tests | Calculated-column dialog, expression editor with autocomplete and inline errors |
| Render and hardening (17–20) | `render` pandas shaping, exports, Redis cache, background workers, security fixes | Table and chart views, export dialog, accessibility pass, empty and error states |

### 8.2 Team

| Role | Count | Focus |
| --- | --- | --- |
| Senior Python engineer (tech lead) | 1 | Architecture, `query` compiler, expression engine |
| Python engineer | 1 | `connections`, `metadata`, `render`, workers |
| Senior front-end engineer | 1 | Report canvas, expression editor, state management |
| Front-end engineer | 1 | Sidebar, column panel, charts, accessibility |
| Product designer / QA (shared) | 1 | Interaction design, usability tests, cross-engine test data |

**Definition of done for every story:** module contracts unchanged or versioned, import-linter green, unit and integration tests on all three engines, keyboard-accessible UI, and an updated OpenAPI schema.

## 9. Risks and open questions

The two risks that most threaten the plan are wrong numbers from automatic joins and the custom-expression compiler, so both get golden tests on all three engines from the first sprint.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Auto-joins produce wrong totals (ambiguous paths, fan-out) | Users trust and act on wrong numbers | Ambiguity prompts, fan-out warnings, join chips and View SQL always visible, golden-dataset tests |
| Expression compiler bug lets unsafe SQL through | Data exposure | Grammar allow-list, identifier lookup instead of copying, bind parameters, Hypothesis fuzzing, external security review before v1 |
| Dialect drift (date diff, `AVG` on integers, collations) | Same report gives different results per engine | One seeded test database per engine in CI via Testcontainers; cross-engine result comparison |
| Module boundaries erode under deadline pressure | Monolith becomes hard to change | import-linter contracts block merges; architecture review each milestone |
| Large result sets exhaust pandas memory | Worker crashes | Row cap (default 100,000), aggregation pushed to SQL, PyArrow-backed frames |
| Synchronous SQL Server driver blocks the event loop | Slow API under load | Bounded thread pool for previews; background workers for full runs |

### Open questions

- [ ] Does "link column" mean a field pulled from an FK-related table (as assumed here), or a clickable hyperlink to another report or record?
- [ ] Which identity provider will `auth` integrate with (Entra ID, Okta, Google), or is local login enough for the first release?
- [ ] Hosted SaaS, customer self-hosted, or both? This decides tenancy and network access to customer databases.
- [ ] Is the default row cap of 100,000 rows acceptable for exports, or do exports need streaming beyond it?
- [ ] Are cross-database joins (two databases on one SQL Server) needed in the first release?
