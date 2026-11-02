# syntax=docker/dockerfile:1.7
# Database Reporting Tool — production image (API only; the front end is hosted separately,
# e.g. on Vercel, and proxies /api and /embed here — see DEPLOYMENT.md).
#   docker build -t reporting .
#   docker compose -f docker-compose.prod.yml up -d

# --- 1. Python dependencies ----------------------------------------------------------------
FROM python:3.12-slim-bookworm AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY reporting/ reporting/
RUN uv sync --frozen --no-dev --no-editable

# --- 2. runtime ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
# Microsoft ODBC Driver 18 for SQL Server connections (EULA accepted for the driver).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gnupg ca-certificates unixodbc \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
    > /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
 && apt-get purge -y curl gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --uid 10001 reporting
COPY --from=python-deps /opt/venv /opt/venv
WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    REPORTING_ENV=production REPORTING_SERVE_FRONTEND=false \
    REPORTING_DATA_DIR=/data REPORTING_WORKERS=4
RUN mkdir -p /data && chown reporting /data
USER reporting
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=4).status == 200 else 1)"
# Validate configuration with readable errors, then start the workers.
CMD ["sh", "-c", "python -c 'from reporting.app.config import Settings; Settings.from_env()' && exec uvicorn reporting.app.main:app --host 0.0.0.0 --port 8000 --workers ${REPORTING_WORKERS} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\" --no-access-log --timeout-graceful-shutdown 30"]
