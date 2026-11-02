# Database Reporting Tool

Browse PostgreSQL, SQL Server, MySQL (and local SQLite) schemas, follow foreign-key
relationships, and build reports by adding columns, aggregates, date differences and custom
formulas to a canvas. The browser sends a typed JSON **query spec**, never SQL; the server
validates every reference against a versioned schema snapshot and compiles parameterised SQL
with SQLAlchemy Core.

Implemented from [`Project Plan Database Reporting Tool.md`](Project%20Plan%20Database%20Reporting%20Tool.md);
progress is tracked task by task in [`TASKS.md`](TASKS.md).

> **Deploying?** See [DEPLOYMENT.md](DEPLOYMENT.md) — production configuration, Docker,
> Windows service, reverse proxy and operations.

## Repositories

The project is split into two repositories that sit in one folder:

| Repository | Contents |
|---|---|
| [reporting-backend](https://github.com/ritesh-pageup/reporting-backend) | Python service (`reporting/`), tests, scripts, Docker and deployment docs |
| [reporting-frontend](https://github.com/ritesh-pageup/reporting-frontend) | React + TypeScript UI and the embed runtime |

The build, serve and Docker scripts expect the front end in `frontend/` inside the backend
folder:

```bash
git clone https://github.com/ritesh-pageup/reporting-backend.git
cd reporting-backend
git clone https://github.com/ritesh-pageup/reporting-frontend.git frontend
```

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 18.17+, and the front end
cloned into `frontend/` (see [Repositories](#repositories)).

```bash
uv sync                               # Python dependencies (+ dev tools)
uv run python scripts/seed_demo.py    # demo "sales" database, users and connection
scripts/serve.sh                      # build the UI and serve it on http://127.0.0.1:8000
```

Sign in as **admin / admin123**, **analyst / analyst123** or **viewer / viewer123**
(viewers cannot see `customers.email`: a column policy hides it).

**Windows (PowerShell):** `powershell -ExecutionPolicy Bypass -File scripts\serve.ps1`
(add `-Build` to rebuild the UI, `-Reset` to re-create the demo data; `scripts\dev.ps1` for
hot reload). If you also work from WSL in the same folder, give WSL its own environment
(`export UV_PROJECT_ENVIRONMENT=.venv-wsl`) — a Linux `.venv` cannot be reused by Windows `uv`.

For development with hot reload, run `scripts/dev.sh` and open http://localhost:5173
(Vite proxies `/api` to the API on port 8000). API docs: http://127.0.0.1:8000/api/docs.

Database drivers are optional extras: `uv sync --extra postgresql --extra mysql --extra mssql`
(SQL Server also needs Microsoft ODBC Driver 18).

## Using it

1. Pick a connection and database in the sidebar (`Ctrl/Cmd + B` collapses it). Selecting a
   table highlights every table one FK hop away: **→** it references, **←** references it,
   dashed outline = relationship inferred from column names.
2. Select columns (Shift/Ctrl for several) and choose **Add to report analysis** — or press
   Enter, right-click, double-click, or drag them onto the canvas. Columns from related tables
   join automatically; if there are several equally short paths you are asked which to use.
3. On each card choose an aggregate (Avg, Sum / Total, Count, Count distinct, Min, Max),
   sort, filter or remove. Join chips in the canvas header show each path and switch between
   inner and left joins or alternative paths.
4. **Add calculated column** offers aggregations, day differences and custom expressions such
   as `[revenue] / NULLIF([orders], 0)` or `(o.total_amount - o.discount) * 1.18`.
5. On a date column's card, **Group by** Day / Month / Year and pick a **Date range** (Today,
   This/Last week, This/Last month, Last 6 months, This/Last year) — e.g. Last 6 months by
   Month. Weeks start on Monday; "today" follows your browser's time zone; empty days or months
   show as 0 for sums and counts.
6. The preview (first 100 rows) refreshes on every change; errors appear on the card that
   causes them, and fan-out warnings flag measures a one-to-many join would multiply.
   **View SQL**, **Run**, the **Chart** tab and **CSV / Excel** export sit below.

## Report templates and embeds

Design: [`docs/Design - Report Templates and Embeds.md`](docs/Design%20-%20Report%20Templates%20and%20Embeds.md).

- **Reports & templates** tab: a library of shared templates and your reports. Start blank,
  or use **Save to report…** in the builder to add the current query as a chart or table.
  In the editor, **Add element** (title, section, text, image, table, chart), drag the ⠿
  handle to move, drag the corner to resize, and configure the selection in the side panel
  (arrow keys move, Shift+arrows resize). **Use template** makes your own copy;
  **Save as template** shares a report's layout.
- **Embed…** on any element creates an embed: *private (signed)* — your website's server
  signs a short-lived token with the embed secret — or *public* (admins only). Options: theme,
  refresh interval, allowed websites (enforced with CSP `frame-ancestors`), title, CSV download.
  Paste the script-tag snippet (auto height) or the iframe snippet into any page.
  Embeds run with the creator's current permissions and can be revoked or have their secret
  rotated at any time.
- **Report filters**: in the editor's document panel, **+ Filter** adds a filter such as
  *Date range* (default *Last 30 days*, optionally *required*) and picks the column it filters in
  each table/chart. The filter bar above the canvas previews values. Embeds supply values with
  `data-param-date_range="last_30_days"` (script tag) or `?p.date_range=last_30_days` (iframe);
  **locked** filters (e.g. a customer id) can only be set in the signed token's `params`, so
  visitors cannot change them:
  ```js
  jwt.sign({ embed: "emb_…", params: { customer: user.customerId } }, SECRET, { expiresIn: "10m" })
  ```

## Tech stack

### Backend (Python)

| Area | Technology | Version | Used for |
|---|---|---|---|
| Language | Python | 3.12+ | The whole server |
| Web framework | FastAPI | 0.162 | The web API, request validation, interactive API docs |
| Web server | Uvicorn | 0.54 | Runs the app with several worker processes |
| Data validation | Pydantic v2 | 2.14 | Report definitions, report documents, embed settings |
| SQL toolkit | SQLAlchemy 2 (Core) | 2.1 | Turns reports into SQL for each database type, and stores the app's own data |
| Formula parser | Lark | 1.3 | Reads custom formulas safely, using its own grammar |
| Data processing | pandas + PyArrow | 3.1 / 26 | Cleaning up result types, chart data, gap filling |
| Encryption | cryptography | 50 | Encrypting saved database passwords (AES-256-GCM) |
| Sign-in | PyJWT + bcrypt | 2.15 / 5.0 | Sign-in sessions, signed embed tokens, password hashing |
| Excel export | openpyxl | 3.1 | Excel downloads |
| Timezones | tzdata | 2026.6 | "Today" and date ranges in the viewer's timezone (needed on Windows) |
| Config | python-dotenv | 1.2 | Reading settings from `.env` |

### Database drivers

| Database | Driver | Version |
|---|---|---|
| PostgreSQL | psycopg 3 | 3.3 |
| SQL Server | pyodbc + Microsoft ODBC Driver 17/18 | 5.3 |
| MySQL | PyMySQL | 1.2 |
| SQLite (demo, local) | built into Python | — |

### Frontend ([separate repository](#repositories))

| Area | Technology | Version | Used for |
|---|---|---|---|
| Language | TypeScript | 5.8 | All UI code |
| UI library | React | 18.3 | Builder, report editor, embed page |
| Build tool | Vite | 5.4 | Builds the app page and the separate embed page |
| Server data | TanStack Query | 5.80 | Loading, caching and refreshing data from the server |
| Long lists | TanStack Virtual | 3.13 | The sidebar's table list stays fast with thousands of tables |
| UI building blocks | Radix UI | 1.x / 2.x | Accessible dialogs, menus, popovers, tabs, tooltips |
| Drag and drop | dnd-kit | 6.3 | Reordering column cards |
| Charts | Apache ECharts | 5.6 | Bar, line, area, pie and scatter charts |
| API types | openapi-typescript | 7.8 | Generates the UI's API types from the server's API description |

### App database and caching

| Component | Technology |
|---|---|
| App database (users, saved connections, schema snapshots, reports, embeds) | PostgreSQL in production; SQLite for development or a single server |
| Shared result cache | Redis (optional), with a built-in in-memory cache otherwise |

### Quality and testing

| Tool | Version | Purpose |
|---|---|---|
| pytest | 9.2 | 276 tests, including live PostgreSQL tests |
| Hypothesis | 6.170 | Fuzz-testing the formula engine |
| import-linter | 2.15 | Enforces the one-way dependencies between modules (12 rules) |
| mypy (strict) | 2.4 | Python type checking |
| Ruff | 0.17 | Python linting |
| fakeredis | 2.40 | Testing the Redis cache without a Redis server |

### Packaging and deployment

| Tool | Purpose |
|---|---|
| uv | Python package and environment manager, with a lockfile (`uv.lock`) |
| npm | Front-end packages, with a lockfile (`package-lock.json`) |
| Docker + docker-compose | Image with the app, PostgreSQL and Redis |
| PowerShell / Bash scripts | `build`, `run-production`, `serve`, `dev`, demo data seeding |
| GitHub Actions | CI: lint, module boundaries, types, tests against live PostgreSQL |
| NSSM (recommended) | Running the app as a Windows service |
| IIS / nginx (recommended) | HTTPS in front of the app |

## Architecture

One Python service, five modules with one-way dependencies, enforced by import-linter:

```
reporting/
  core/         shared kernel: ids, errors, Principal, DataType, AccessContext, QueryResult
  connections/  secret vault (AES-256-GCM envelope), engine pools, read-only execution
  metadata/     per-dialect catalog introspection, versioned snapshots, join-path search
  query/        spec models, 7-step validator, Lark expression engine, SQL compiler
  render/       pandas shaping, ECharts payloads, CSV/Excel export
  auth/         users, roles, JWT sessions, connection grants, column policies
  reports/      report documents/templates (grid layout, elements, bindings) and embeds
  app/          FastAPI routes and wiring (the only caller of auth and render)
frontend/       React + TypeScript (Vite, TanStack Query, Radix, dnd-kit, ECharts) — separate repo
```

Each module's public surface is its `api.py` (a `typing.Protocol` plus frozen DTOs);
everything else lives in `_internal/`. `app/wiring.py` builds the implementations through each
module's factory function.

### Safety properties

- Joins reference a relationship id from the snapshot; the client can never write an ON clause.
- Custom expressions go through an allow-listed grammar; identifiers are looked up in the
  snapshot and every literal is a bind parameter. Hypothesis fuzzing (`tests/test_expression_fuzz.py`)
  checks that any input either fails cleanly or compiles to SQL containing only snapshot
  identifiers, allow-listed functions and placeholders.
- Customer queries run in a read-only transaction (SQLite: `query_only`) with a statement
  timeout and row cap, and are always rolled back.
- Connection strings are encrypted at rest; dangerous driver options (`local_infile`,
  `init_command`, …) are rejected; the API never returns a secret.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `REPORTING_DATA_DIR` | `./data` | App database, generated dev keys, demo data |
| `REPORTING_APP_DB_URL` | `sqlite:///data/app.db` | App database (PostgreSQL recommended in production) |
| `REPORTING_MASTER_KEY` | generated into `data/master.key` | Base64 32-byte key wrapping per-tenant data keys (use KMS/Vault in production) |
| `REPORTING_JWT_SECRET` | generated into `data/jwt.secret` | Session signing secret (≥ 32 chars) |
| `REPORTING_ADMIN_USER` / `REPORTING_ADMIN_PASSWORD` | `admin` / random (logged) | First administrator, created when there are no users |
| `REDIS_URL` | unset (in-process cache) | Result cache |
| `REPORTING_CACHE_TTL` | `300` | Result cache TTL (seconds) |
| `REPORTING_CORS_ORIGINS` | unset | Comma-separated origins if the UI is served elsewhere |
| `REPORTING_PUBLIC_URL` | request URL | Public origin used in embed snippets, e.g. `https://reports.example.com` |
| `REPORTING_EMBED_KEY` | derived from the JWT secret | Key from which per-embed secrets are derived (changing it invalidates all embed secrets) |

## Tests and checks

```bash
scripts/check.sh                                   # ruff, import-linter, mypy --strict, pytest, tsc
REPORTING_FUZZ_EXAMPLES=10000 uv run pytest tests/test_expression_fuzz.py   # milestone-size fuzz run
uv run python scripts/export_openapi.py && (cd frontend && npm run gen:api) # refresh API types
```

## Status and known gaps

- Verified end to end: SQLite; PostgreSQL (as app database and as a source, with golden
  results identical to SQLite — `tests/test_postgres_live.py`, run in CI); SQL Server with
  Windows authentication (manually). MySQL is covered by SQL-compilation tests only.
- Local username/password login is implemented; OIDC (open question in the plan) plugs into
  `auth` by issuing the same JWT session after IdP login.
- App-database tables are created on startup (safe with concurrent workers); explicit migrations
  will be needed for future changes to existing tables. Background workers for long runs and
  cross-database joins are not implemented yet.
