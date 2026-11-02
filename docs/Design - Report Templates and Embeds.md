# Design: Report Templates and Embeddable Components

Status: MVP implemented (see `TASKS.md`, Phase 9). Later phases are proposals.

This design extends the existing architecture (`Project Plan Database Reporting Tool.md`):
a new `reports` module stores documents and embeds; the HTTP layer keeps orchestrating
`query`, `render` and `auth`. Data still flows only through validated query specs.

---

# Feature 1 — Report templates in the Canvas section

## 1.1 Overview and goals

A **report document** is a page laid out on a grid that combines text, section headers,
images, tables and charts. Data elements are bound to a query spec (the same JSON the builder
already produces), so every element is validated and compiled exactly like a builder report.

A **template** is a document flagged `kind = "template"`, shared as a reusable starting point.
"Use template" copies it into a **report** the user owns; "Save as template" does the reverse.

Goals:

- Build a multi-element report without leaving the app, reusing the builder for data.
- Reuse layouts across teams; one change to a template does not alter reports made from it.
- Keep documents declarative (JSON), versioned and safe to render anywhere (including embeds).

Non-goals (MVP): pixel-perfect print layout, free-form absolute positioning, nested sections,
real-time co-editing.

## 1.2 User stories

1. As an **analyst**, I start a blank template, add a title, a monthly revenue chart and a top
   customers table, arrange them side by side, and save it for my team.
2. As an **analyst**, while building a report in the canvas I click **Save to template**, choose
   "chart" or "table", and the current query lands in a new or existing template.
3. As a **viewer**, I pick "Sales overview" from the template library, click **Use template**,
   and get my own copy that I can rename and adjust without affecting the original.
4. As a **report owner**, I resize a chart to full width, change its type to line and restyle
   the card, and my changes are saved without overwriting a colleague's concurrent edit.
5. As a **template author**, I turn an existing report into a template so others can reuse it.

## 1.3 Functional requirements

1. Users can create a template from a blank canvas or from an existing report (duplicate with
   `kind = template`), and a report from a template ("Use template").
2. The builder offers **Save to template**: adds the current query as a table or chart element
   to a new or chosen existing template/report.
3. Element types: `text` (variants title, heading, body, section header), `table`, `chart`,
   `image` (HTTPS URL or small inline image).
4. Elements sit on a **12-column grid** with a fixed row height; users **drag** to move,
   **drag the corner** to resize (snapping to cells), and the layout resolves overlaps by
   pushing elements down. Keyboard: arrow keys move, Shift+arrow resizes the selected element.
5. Selecting an element opens a configuration panel: title, style (background, border,
   alignment), content (text/image) or data options (chart type and axes, table rows).
6. Data elements show live data in the editor using the viewer's own permissions.
7. Documents have document-level settings: page width and theme (accent colour, density).
8. Saving uses optimistic concurrency: a stale `version` returns **409 Conflict** and the
   editor offers reload.
9. Library lists templates (visible to all signed-in users) and reports (owner; admins see
   all), with duplicate, rename and delete.
10. Binding query specs are shape-validated on save; semantic errors (e.g. a removed column)
    appear on the element at view time instead of blocking the save.
11. Limits: 50 elements per document, 10,000 characters per text element, inline images
    ≤ 1 MB, image URLs must be `https:` (or `data:image/*`).

## 1.4 Data model

```text
reports_documents
  id               text PK          "rpt_…"
  owner_id         text             user who owns it
  kind             text             "template" | "report"
  name, description
  document         JSON             ReportDocument (below)
  version          int              optimistic concurrency, +1 per save
  source_id        text NULL        template/report it was copied from
  created_at, updated_at
```

```jsonc
// ReportDocument (schema_version 1)
{
  "schema_version": 1,
  "page":  { "columns": 12, "row_height": 40, "max_width": 1200 },
  "theme": { "accent": "#2563eb", "density": "comfortable" },
  "elements": [
    {
      "id": "el_1a2b",
      "type": "chart",                         // text | table | chart | image
      "title": "Revenue by month",
      "layout": { "x": 0, "y": 2, "w": 6, "h": 8 },   // grid cells, x+w ≤ 12
      "style":  { "background": "surface", "border": true, "align": "left" },
      "binding": {                             // table/chart only
        "query": { /* QuerySpec, snapshot_version stripped */ },
        "chart": { "type": "bar", "x": "month", "y": ["revenue"] },
        "page_size": 100
      },
      "text":  { "content": "…", "variant": "body" },   // text only
      "image": { "url": "https://…", "alt": "…", "fit": "contain" }  // image only
    }
  ]
}
```

- **Layout** is stored in grid units, so it renders identically at any width and degrades to a
  single column on narrow screens (elements stack in `y, x` order).
- **Sections** are full-width `text` elements with `variant: "section"`; they act as visual
  bands. Nested containers are a later phase.
- **Bindings** embed a full query spec. `snapshot_version` is stripped on save so templates
  validate against the *current* schema when reused.
- **Styling** is a closed set of tokens (no raw CSS), so documents cannot inject markup or
  styles and look right in light and dark themes.

## 1.5 Security and access control

- Reading a template's layout is open to signed-in users; **data** always runs with the
  *viewer's* `AccessContext`, so a template never grants access to tables or columns.
- Only the owner (or an admin) can edit or delete a document.
- Text is rendered as plain text (no HTML); image URLs are restricted to `https:`/`data:image`.
- Query specs are validated by the `query` module like any builder request; no SQL is stored.

## 1.6 Technical approach

- New module `reports` (`reports/api.py` + `_internal/`), owning `reports_documents` and
  `reports_embeds`. It imports only `core`; the HTTP layer validates bindings through `query`
  (`parse_spec`) and `render` (`ChartSpec`), preserving one-way dependencies.
- Front end: a **Reports** workspace next to the builder with a library and an editor. The
  grid is implemented with CSS grid plus pointer events (move/resize with snapping and
  collision push-down); elements fetch data via the existing `/query/run` and `/render/chart`.

---

# Feature 2 — Embeddable reporting components

## 2.1 Overview and goals

Any element of a saved report or template (table, chart, text, image) can be embedded in an
external web page with a snippet, YouTube-style:

```html
<iframe src="https://reports.example.com/embed/emb_9fK2…" width="100%" height="420"
        style="border:0" loading="lazy" title="Revenue by month"></iframe>
```

or, with automatic height and optional signed tokens:

```html
<div data-reporting-embed="emb_9fK2…" data-token="…"></div>
<script async src="https://reports.example.com/embed.js"></script>
```

Goals: one-click snippet, safe defaults, live data with a controlled refresh rate, no
exposure of credentials or query internals, and revocability.

## 2.2 User stories

1. As an **analyst**, I open a chart's menu, choose **Embed**, pick theme and refresh interval,
   and copy an iframe snippet into our intranet page.
2. As a **developer** of a customer portal, I create a **signed** embed, store its secret on our
   server, and generate short-lived tokens so only logged-in portal users can see the chart.
3. As an **admin**, I see every embed of a report, when it was last viewed, and revoke one
   immediately when a page is retired.
4. As a **page visitor**, the embedded table resizes with the page, follows dark mode when the
   host requests it, and refreshes every few minutes without reloading the page.

## 2.3 Functional requirements

1. Embeds are created per element (`document_id`, `element_id`) with options: **mode**
   (`public` | `signed`), **theme** (`light` | `dark` | `auto`), **refresh interval**
   (off, or ≥ 30 s), show title, allow CSV download, **allowed origins**, optional expiry.
2. The UI generates both snippets (iframe and script tag) and, for signed embeds, shows the
   secret **once** plus server-side token examples (Node.js, Python).
3. Embed URLs use unguessable ids (≥ 96 bits of randomness).
4. Embedded data is produced server-side from the stored element; the embed client cannot send
   queries, filters or SQL.
5. Data runs as the **embed owner** with the owner's current permissions (column policies
   apply); if the owner loses access the embed stops serving data.
6. Signed embeds require `?token=` — an HS256 JWT signed with the embed secret containing
   `embed` (the id) and `exp` (required, at most 24 h ahead).
7. Embeds can be revoked, and signed embeds' secrets rotated (old tokens stop working).
8. Embeds reflect the live element: editing the report updates every embed of it; deleting
   the element or document disables the embed with a clear message.
9. Refresh: the embed polls with `If-None-Match`; unchanged data returns **304**. Polling
   pauses while the page is hidden.
10. Responsiveness: the iframe fills its width; `embed.js` auto-sizes the height from
    `postMessage` events; charts resize with their container.
11. Rate limiting per embed and client IP; payload limited to ≤ 500 table rows.
12. View counts and last-viewed time are recorded per embed.

## 2.4 Data model

```text
reports_embeds
  id                text PK      "emb_" + 16 random bytes (base62)
  document_id       text FK      reports_documents.id
  element_id        text         element inside the document
  owner_id          text         data runs with this user's permissions
  mode              text         "public" | "signed"
  options           JSON         { theme, refresh_interval_s, show_title, allow_download }
  allowed_origins   JSON         ["https://portal.example.com", …]  ([] = any)
  secret_version    int          bumped on rotation
  expires_at        timestamp NULL
  revoked_at        timestamp NULL
  view_count        int
  last_viewed_at    timestamp NULL
  created_at
```

The signing secret is **never stored**: `secret = HMAC-SHA256(server_embed_key,
"{id}:{secret_version}")`. Rotation increments `secret_version`.

## 2.5 Security and access control

| Concern | Control |
|---|---|
| Who can create | Analysts and admins; **public** embeds require an admin. |
| Public exposure | Unguessable id, explicit warning in the UI, revocable, `X-Robots-Tag: noindex`. |
| Private embeds | Signed short-lived JWT (`exp` ≤ 24 h) checked against the embed secret; the host app authenticates its own users and mints tokens server-side. |
| Data scope | Stored element only; executed with the owner's `AccessContext`, re-evaluated per request. |
| Framing | `Content-Security-Policy: frame-ancestors <allowed origins>` on embed pages; the main app sends `frame-ancestors 'none'` (clickjacking). |
| Session confusion | The embed API ignores app session tokens; only embed tokens are accepted. |
| Token leakage | `Referrer-Policy: no-referrer`, short expiry, rotation. |
| postMessage | Only height messages are sent; `embed.js` accepts messages only from the embed origin and from its own iframe. |
| Abuse | Per-embed + IP rate limit; result cache; row cap. |

Note: browsers enforce `frame-ancestors`; a server cannot verify the parent page on data
requests, so **signed mode is the real access control** for private data.

## 2.6 Technical approach

- `GET /embed/{id}` serves a small dedicated page (separate Vite entry, shared `Chart` and
  table components) with the CSP headers above.
- `GET /api/embed/{id}` (embed token in `Authorization: Bearer`) returns
  `{ config, payload }` where payload is a `TablePayload`, `ChartPayload`, text or image, plus
  an `ETag`.
- Caching: query results are cached by (SQL, params, connection, snapshot); embed requests
  accept cached results no older than the refresh interval (default 5 minutes).
- `embed.js` replaces `[data-reporting-embed]` placeholders with iframes and listens for
  `{ type: "reporting-embed:resize", height }` messages.

---

# Shared

## Edge cases

- Template references a connection the user cannot access → element shows "No access to this
  connection"; other elements still render.
- Schema change removes a bound column → element shows the validation error; embeds return a
  clear error state rather than stale data.
- Concurrent edits → 409 with reload; no silent overwrite.
- Element deleted while embedded → embed shows "This content is no longer available".
- Embed owner deleted or demoted → embed returns 403 until reassigned (reassignment: later).
- Very large tables in embeds → capped at 500 rows with a "truncated" note.
- Refresh interval shorter than query duration → requests coalesce via the cache; minimum 30 s.
- Clock skew on signed tokens → small leeway; `exp` required.
- Host page in dark mode with `theme=auto` → follows `prefers-color-scheme` inside the iframe.
- Narrow host containers → grid documents stack into one column; charts reflow.

## Open questions for the product team

1. Should templates support **parameters** (date range, connection, filters chosen at use
   time)? Recommended for phase 2.
2. Should signed tokens carry **locked filters** (e.g. `customer_id = 42`) for multi-tenant
   portals? Strongly recommended next; needs a declared list of lockable columns per embed.
3. Should a template be **linked** (reports update when the template changes) or always copied?
   MVP copies.
4. Are **public** embeds acceptable for your compliance requirements at all, or admin-gated as
   in the MVP?
5. Whole-report embeds (all elements) in addition to element embeds?
6. Should embed views count toward any licensing or usage limits?
7. Image hosting: allow uploads (needs storage and scanning) or keep URLs/inline only?
8. Which users may see the shared template library — everyone, or per team?

## Prioritized implementation phases

| Phase | Scope |
|---|---|
| **MVP (implemented)** | Documents with text/section/table/chart/image on a 12-column grid; move, resize, configure; templates ↔ reports; save builder query into a template; optimistic versioning. Element embeds: public (admin) and signed; theme; refresh with ETag/304; allowed origins via CSP; revoke and rotate; iframe and script snippets with auto-resize; rate limiting. |
| **Phase 2** | ~~Template parameters; locked filters in signed tokens~~ (implemented as *report filters*: date range / equals / one of, required, locked, set via `data-param-*`, `?p.*` or token `params`); whole-report embeds; document version history and restore; image uploads; embed owner reassignment. |
| **Phase 3** | Scheduled PDF/email delivery; white-label themes; embed analytics dashboard; JS SDK events (click-through, filter sync); real-time collaborative editing. |
