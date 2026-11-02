# Deployment guide

How to run the Database Reporting Tool in production. For local development, see
[README.md](README.md).

## 1. Architecture in production

```
 users / host websites ──HTTPS──▶ reverse proxy (IIS, nginx, Traefik, cloud LB)
                                         │  http://127.0.0.1:8000
                                         ▼
                             app: N uvicorn worker processes
                     ┌───────────────────┼────────────────────┐
                     ▼                   ▼                    ▼
           app database (PostgreSQL)   Redis (optional)   customer databases
           users, encrypted             shared result      PostgreSQL / SQL Server /
           connections, snapshots,      cache              MySQL (read-only queries)
           reports, embeds
```

- One Python service; workers share state through the app database (and Redis if set).
- TLS terminates at the reverse proxy. The app sets HSTS when `REPORTING_PUBLIC_URL` is `https://`.

## 2. Configuration

Copy `.env.example` to `.env` and fill it in. Generate secrets with
`python scripts/generate_secrets.py` and store them in your secret manager.

| Variable | Required | Notes |
|---|---|---|
| `REPORTING_ENV=production` | yes | Enables strict validation; the server refuses to start on problems and lists them. |
| `REPORTING_PUBLIC_URL` | yes | e.g. `https://reports.example.com` — used in embed snippets and for HSTS. |
| `REPORTING_APP_DB_URL` | yes | `postgresql+psycopg://user:pass@host:5432/reporting` (recommended). SQLite works for a single server. |
| `REPORTING_MASTER_KEY` | yes | Encrypts saved connection passwords. **Back it up — losing it makes saved passwords unrecoverable.** |
| `REPORTING_JWT_SECRET` | yes | Signs sign-in sessions; changing it signs everyone out. |
| `REPORTING_EMBED_KEY` | recommended | Derives embed secrets; changing it invalidates every signed embed. |
| `REPORTING_ADMIN_PASSWORD` | first start | Creates the first administrator when no users exist; remove afterwards. |
| `REDIS_URL` | optional | Shared query-result cache across workers/servers. |
| `REPORTING_WORKERS` | optional | Worker processes (default 4 / CPU count). |
| `REPORTING_SESSION_TTL`, `REPORTING_LOGIN_ATTEMPTS`, `REPORTING_MAX_BODY_BYTES`, `REPORTING_LOG_LEVEL`, `REPORTING_API_DOCS` | optional | See `.env.example`. |

Customer-database logins should be **read-only** accounts; the connection test warns when a
login can write. Queries always run read-only with a timeout and row cap regardless.

## 3. Option A — Docker (Linux servers)

```bash
cp .env.example .env              # fill in; also set POSTGRES_PASSWORD
docker compose up -d --build      # app + PostgreSQL + Redis
docker compose logs -f app
```

The image is API-only (`REPORTING_SERVE_FRONTEND=false`): host the front end separately and
proxy `/api` and `/embed` to it (section 6). It installs locked Python dependencies and the Microsoft ODBC
Driver 18 for SQL Server, runs as a non-root user, and has a health check on `/api/ready`.
Expose port 8000 only to your reverse proxy (compose binds it to 127.0.0.1).

## 4. Option B — Windows server

1. Install Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20 LTS, and (for SQL Server
   sources) Microsoft ODBC Driver 17 or 18.
2. Build (installs locked dependencies, builds the UI, runs all checks):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\build.ps1
   ```
3. Create `.env` (section 2), then start:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\run-production.ps1 -Port 8000 -Workers 4
   ```
4. Run it as a Windows service, e.g. with [NSSM](https://nssm.cc/):
   ```powershell
   nssm install ReportingTool powershell.exe "-ExecutionPolicy Bypass -File `"D:\Apps\Reporting\scripts\run-production.ps1`" -Port 8000"
   nssm set ReportingTool AppDirectory "D:\Apps\Reporting"
   nssm set ReportingTool AppStdout "D:\Apps\Reporting\logs\service.log"
   nssm set ReportingTool AppStderr "D:\Apps\Reporting\logs\service.log"
   nssm start ReportingTool
   ```
   For SQL Server sources using **Windows authentication** (`Trusted_Connection=True`), run the
   service as a domain account that has read access to those databases.
5. Put IIS (with URL Rewrite + Application Request Routing) or nginx in front for HTTPS,
   forwarding to `http://127.0.0.1:8000`.

## 5. Option C — Linux without Docker

```bash
scripts/build.sh
PORT=8000 REPORTING_WORKERS=4 scripts/run-production.sh     # run under systemd in practice
```

## 6. Option D — Vercel (front end) + Hostinger VPS (backend container)

```
browser ──▶ https://<your-app>.vercel.app        (REPORTING_PUBLIC_URL)
              │  static UI, embed.js
              │  /api/*  and  /embed/*  rewritten (proxied) by Vercel
              ▼
            https://API_DOMAIN ──▶ VPS reverse proxy (TLS) ──▶ 127.0.0.1:8081 ──▶ app container
                                                                     ├▶ redis container
                                                                     └▶ external PostgreSQL
```

Users only ever see the Vercel origin, so no CORS is needed. The backend serves `/embed/{id}`
itself (with that embed's `frame-ancestors`), using `embed.html` fetched from the Vercel site.

**One-time setup on the VPS**

1. Point a DNS name at the VPS (`API_DOMAIN`, e.g. `api.example.com`) and add a site for it to
   the reverse proxy that already serves ports 80/443 on the VPS, forwarding to
   `http://127.0.0.1:8081` with `Host`, `X-Forwarded-For` and `X-Forwarded-Proto`, and with
   an 8 MB request-body limit. nginx example:
   ```nginx
   server {
       server_name api.example.com;            # plus your usual listen 443 ssl / certificate lines
       client_max_body_size 8m;
       location / {
           proxy_pass http://127.0.0.1:8081;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_read_timeout 120s;
       }
   }
   ```
   If that proxy itself runs in Docker, `127.0.0.1` is the proxy container, not the VPS: change the
   port mapping to `"172.17.0.1:8081:8000"` (the Docker bridge address of the host) or put both on
   a shared Docker network.
2. Create `/opt/report-tool-secret/prod.env` (`chmod 600`):
   ```ini
   REPORTING_PUBLIC_URL=https://<your-app>.vercel.app
   REPORTING_APP_DB_URL=postgresql://USER:PASSWORD@HOST:5432/reporting_tool
   REPORTING_MASTER_KEY=...        # from: python scripts/generate_secrets.py
   REPORTING_JWT_SECRET=...
   REPORTING_EMBED_KEY=...
   REPORTING_ADMIN_USER=admin
   REPORTING_ADMIN_PASSWORD=...    # first start only; remove afterwards
   ```
   The PostgreSQL server must accept connections from the VPS IP (e.g. an AWS security-group
   rule for port 5432); add `?sslmode=require` to the URL if the server supports TLS.
3. Let the VPS pull the private image once:
   `docker login ghcr.io -u ritesh-pageup` with a classic personal access token that has
   only `read:packages`.

**GitHub (reporting-backend → Settings → Secrets and variables → Actions)**

- Secret `HOSTINGER_API_KEY` (hPanel → API), variable `HOSTINGER_VM_ID`.
- `.github/workflows/deploy.yml` builds the image, pushes it to
  `ghcr.io/ritesh-pageup/reporting-backend:<commit sha>`, then asks Hostinger to deploy
  `docker-compose.prod.yml` with that tag.

**Vercel (reporting-frontend)**

1. Replace `API_DOMAIN` in `vercel.json` with the backend's DNS name and push.
2. Import the repository in Vercel (framework: Vite; settings come from `vercel.json`).
3. Put the Vercel URL (or your custom domain) in `REPORTING_PUBLIC_URL` and redeploy the backend.

Check: `https://<your-app>.vercel.app/api/ready` returns `{"status":"ready"}`.

## 7. Reverse proxy notes

- Forward `Host`, `X-Forwarded-For` and `X-Forwarded-Proto`. The run scripts start uvicorn with
  `--proxy-headers` trusting `127.0.0.1`; if the proxy runs on another host, set
  `FORWARDED_ALLOW_IPS` (Linux/Docker) so rate limits see real client addresses.
- Allow request bodies of at least `REPORTING_MAX_BODY_BYTES` (8 MB default).
- Do not add your own `X-Frame-Options` / CSP for `/embed/*`: embed pages set
  `frame-ancestors` from each embed's allowed websites. Everything else sends
  `frame-ancestors 'none'`.
- Health checks: liveness `GET /api/health`, readiness `GET /api/ready` (checks the app DB).

## 8. Operations

- **Logs**: one access line per request with a request id (`X-Request-ID` header); unexpected
  errors are logged with that id and users see only the id.
- **Backups**: back up the app database and keep `REPORTING_MASTER_KEY` / `REPORTING_EMBED_KEY`
  safe; nothing else is stateful (Redis is a cache).
- **Upgrades**: build the new version, restart the service. The app creates missing tables at
  startup (safe with several workers starting at once). Take a database backup before upgrading;
  future releases that change existing tables will ship explicit migration steps.
- **Scaling**: add workers or servers behind the load balancer; use PostgreSQL for the app
  database and `REDIS_URL` for a shared cache. Per-process state (rate-limit buckets, buffered
  embed view counts) is intentionally local.
- **Security checklist**: HTTPS only; read-only database logins; `REPORTING_API_DOCS` off;
  rotate `REPORTING_ADMIN_PASSWORD` out of `.env` after first start; review public embeds
  (admins only) and revoke unused ones.

## 9. What has been verified

- Production mode with 4 workers on a PostgreSQL app database (clean concurrent start-up),
  strict CSP with no violations across the builder, report editor and embeds.
- PostgreSQL as a reporting source (introspection incl. multiple schemas and cross-schema keys)
  and identical results to SQLite for the golden reports (`tests/test_postgres_live.py`, also run
  in CI).
- SQL Server with Windows authentication and named instances (manually, against SQL Express).
- The Windows production script with multiple workers.
- Not verified here: building the Docker image (no Docker in the development environment) and
  MySQL against a live server. Run `docker compose up --build` once in your environment before
  relying on it.
