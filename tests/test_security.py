import pytest
from fastapi.testclient import TestClient

from reporting.app.config import Settings
from reporting.app.main import create_app
from reporting.connections import generate_master_key


def make_client(tmp_path, **kw):
    settings = Settings(app_db_url=f"sqlite:///{tmp_path / 'app.db'}", master_key=generate_master_key(),
                        jwt_secret="j" * 40, admin_password="admin-pass-1", data_dir=tmp_path, **kw)
    return TestClient(create_app(settings), raise_server_exceptions=False)


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path, max_body_bytes=2048, login_attempts_per_minute=3,
                       public_url="https://reports.example.com")


def test_security_headers(client):
    r = client.get("/api/health")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
    assert len(r.headers["x-request-id"]) >= 8
    assert client.get("/api/health", headers={"X-Request-ID": "abc12345-trace"}).headers["x-request-id"] == "abc12345-trace"
    assert client.get("/api/health", headers={"X-Request-ID": "bad id <script>"}).headers["x-request-id"] != "bad id <script>"


def test_no_hsts_without_https(tmp_path):
    assert "strict-transport-security" not in make_client(tmp_path).get("/api/health").headers


def test_body_size_limit(client):
    big = "x" * 5000
    r = client.post("/api/auth/login", json={"username": big, "password": "p"})
    assert r.status_code == 413 and r.json()["error"] == "payload_too_large"

    def chunks():  # no Content-Length: streamed body is still limited
        for _ in range(10):
            yield b"x" * 1000
    r = client.post("/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_login_rate_limit(client):
    codes = [client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code
             for _ in range(5)]
    assert codes[:3] == [403, 403, 403] and codes[3] == 429
    # even the right password is refused while limited
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-1"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


def test_unexpected_errors_do_not_leak(client):
    app = client.app

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("secret internal detail")
    # route registered after the SPA? place it explicitly first
    app.router.routes.insert(0, app.router.routes.pop())
    r = client.get("/api/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "internal_error" and "secret internal detail" not in r.text
    assert body["request_id"] == r.headers["x-request-id"]


def test_docs_toggle(tmp_path):
    assert make_client(tmp_path, api_docs=False).get("/api/openapi.json").status_code in (404, 405)


def test_ready_and_static_caching(tmp_path):
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>app</html>")
    (static / "assets" / "index-abc123.js").write_text("console.log(1);" * 200)
    (static / "embed.js").write_text("/* embed */")
    c = make_client(tmp_path, static_dir=static)
    assert c.get("/api/ready").json() == {"status": "ready"}
    asset = c.get("/assets/index-abc123.js", headers={"Accept-Encoding": "gzip"})
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.headers.get("content-encoding") == "gzip"
    assert c.get("/").headers["cache-control"] == "no-cache"
    assert c.get("/reports/anything").text == "<html>app</html>"  # SPA fallback
    assert c.get("/embed.js").headers["cache-control"] == "no-cache"
