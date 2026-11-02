# Implementation Tasks — Database Reporting Tool

Derived from `Project Plan Database Reporting Tool.md`. Each task is ticked `[x]` once implemented and its tests pass.

## Phase 1 — Foundations
- [x] **T1. Repo layout & tooling** — `reporting/` package with five modules + `core` + `app`, `pyproject.toml`, Ruff, mypy, pytest config
- [x] **T2. Core shared kernel** — ids, `core.errors`, `Principal`, `DataType`, `CompiledStatement`, `AccessContext`, `ExecutionLimits`, refs
- [x] **T3. import-linter contracts** — layers, independence, forbidden `_internal` imports; CI workflow

## Phase 2 — Connections and metadata
- [x] **T4. connections: vault** — AES-256-GCM secret encryption, `ConnectionInfo` never exposes secrets
- [x] **T5. connections: service** — connection-string parsing + option allow-list, stepwise test, lazy engine pool w/ idle disposal, read-only `execute_read` with timeout and row cap
- [x] **T6. metadata: introspection** — per-dialect catalog queries (PostgreSQL, SQL Server, MySQL, + SQLite for local), canonical `DataType` mapping
- [x] **T7. metadata: snapshots & relationships** — versioned snapshots w/ diff, `related_tables`, BFS `find_join_paths`, inferred and user relationships

## Phase 3 — Query core
- [x] **T8. query: spec models** — Pydantic v2 discriminated union, `extra="forbid"`, size limits
- [x] **T9. query: validator** — 7-step pipeline (shape, snapshot, references, access, types, semantics incl. fan-out, expressions) with JSON paths
- [x] **T10. query: compiler** — SQLAlchemy Core compile to PostgreSQL/MSSQL/MySQL/SQLite, bind params only, `DateDiff` construct, MSSQL AVG cast
- [x] **T11. query: execution** — `execute`/`preview` via `connections.execute_read`, pandas type normalisation, result cache

## Phase 4 — Calculated columns
- [x] **T12. Expression engine** — Lark LALR grammar, typed AST, limits, reference resolution + cycle detection, type checker, aggregation-level check, compile to SQLAlchemy
- [x] **T13. Fuzz tests** — Hypothesis: random strings & valid ASTs only yield clean errors or safe SQL

## Phase 5 — Render, auth, HTTP
- [x] **T14. render** — table payloads, ECharts chart payloads, chart suggestions, top-N+Other, CSV & Excel export
- [x] **T15. auth** — users, roles, JWT sessions, `authorize`, `visible_connections`, `access_context` with column policies
- [x] **T16. HTTP layer** — FastAPI routes per §7.6, `app/wiring.py`, error → status mapping, OpenAPI
- [x] **T17. Architecture tests & demo data** — boundary type tests, seeded demo SQLite database (sales schema)

## Phase 6 — Front end (React + TypeScript)
- [x] **T18. App shell & API client** — Vite, design tokens, typed API client, login
- [x] **T19. Collapsible sidebar** — connection/database switcher, search, schema groups, FK highlighting, related-only toggle, Ctrl/Cmd+B, keyboard nav
- [x] **T20. Column panel** — badges, Add to report analysis, multi-select, related-table columns with join-path resolution & ambiguity popover
- [x] **T21. Report canvas** — column cards (aggregate/sort/remove/reorder), join chips, filters, live 100-row preview, View SQL, inline validation errors
- [x] **T22. Calculated column dialog** — Aggregation / Date difference / Custom expression tabs with live validation
- [x] **T23. Table & chart views + export** — ECharts rendering, CSV/Excel download

## Phase 7 — Wrap-up
- [x] **T24. README & run scripts** — setup, run, test instructions

## Phase 8 — Date aggregation (extension)
- [x] **T25. Preset date ranges** — `in_period` filter: today, this/last week, this/last month, last 6 months, this/last year; half-open ranges in the browser's time zone
- [x] **T26. Date granularity** — group a date column by day, month or year (per-dialect truncation), combinable with any preset
- [x] **T27. Gap filling & bucket display** — empty buckets filled (0 for sum/count); "May 2026"-style labels in table and chart; chronological bucketed charts
- [x] **T28. UI** — Group by / Date range in the date card menu, "is in" period condition in filters; verified in the browser

## Phase 9 — Report templates & embeddable components
- [x] **T29. Design document** — `docs/Design - Report Templates and Embeds.md` (overview, stories, requirements, data model, security, architecture, edge cases, phases)
- [x] **T30. reports module** — document model (layout grid, elements, bindings, styling, theme), CRUD with optimistic versioning, duplicate (use template / save as template)
- [x] **T31. Reports HTTP API** — routes, ownership rules, binding validation, `Conflict` error → 409
- [x] **T32. Template editor** — 12-column grid canvas; add / move / resize / configure text, section, table, chart and image elements; live data
- [x] **T33. Template flows** — library (templates / reports), blank template, save current builder report into a template, use template, duplicate, delete
- [x] **T34. Embeds backend** — public & signed embeds, derived per-embed secrets, allowed origins (CSP `frame-ancestors`), refresh interval with cache age, ETag, rate limiting, revoke / rotate
- [x] **T35. Embed runtime & UI** — iframe page, `embed.js` (auto-resize via postMessage), theme support, embed-code dialog with snippets
- [x] **T36. Tests, docs & verification** — backend tests, browser verification, README

## Phase 10 — Report filters for templates & embeds
- [x] **T37. Rolling date presets** — Last 7 / 30 / 90 days (rolling, inclusive of today)
- [x] **T38. Filter parameters model** — document-level filters (date range / equals / one of), default, required, locked, per-element target columns; validated on save
- [x] **T39. Embed filter resolution** — values from `?p.<id>=` / `data-param-<id>` and signed-token `params`; locked = token only; required checks; embed runtime pass-through
- [x] **T40. Editor UI** — define filters and their target columns; filter bar to preview values in the editor
- [x] **T41. Embed dialog** — filter values in iframe/script snippets and token examples
- [x] **T42. Tests, docs & verification**

## Phase 11 — Production readiness
- [x] **T43. Production configuration** — `REPORTING_ENV=production`: required secrets and settings validated at startup (fail fast), `.env` support, `.env.example`, API docs toggle, no demo seeding
- [x] **T44. Security hardening** — security headers + strict CSP for the app, HSTS, login brute-force limiting, request size limit, generic 500 handler (no internals leaked) with request ids
- [x] **T45. Multi-process correctness** — schema snapshots and rotated connection credentials stay consistent across server workers
- [x] **T46. Operations** — readiness endpoint, structured access logging, gzip, long-lived caching for hashed assets, graceful shutdown
- [x] **T47. PostgreSQL app database** — run the app's own tables on PostgreSQL (verified against a real server), plus PostgreSQL introspection end to end
- [x] **T48. Packaging** — production build scripts, Dockerfile + docker-compose (app, PostgreSQL, Redis), Windows production script (multiple workers, `.env`), service setup
- [x] **T49. Front-end hardening** — error boundary, verified under the production CSP
- [x] **T50. Production verification & deployment guide** — run in production mode with multiple workers, smoke tests, `DEPLOYMENT.md`
