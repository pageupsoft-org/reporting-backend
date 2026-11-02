import os
import subprocess
import sys
from pathlib import Path

from reporting.app.config import Settings
from reporting.app.wiring import build_services
from reporting.core import AccessContext, ExecutionLimits
from reporting.query import parse_spec

ROOT = Path(__file__).resolve().parents[1]


def test_seed_demo_and_self_join(tmp_path):
    env = {**os.environ, "REPORTING_DATA_DIR": str(tmp_path), "REPORTING_ADMIN_PASSWORD": "admin123"}
    out = subprocess.run([sys.executable, "scripts/seed_demo.py"], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True).stdout
    assert "Demo ready" in out
    services = build_services(Settings.from_env(tmp_path))
    conn = services.connections.list()[0]
    rel = "fk_employees_manager_id"
    base = {"connection_id": conn.id,
            "base": {"alias": "e", "table": {"database": "main", "name": "employees"}},
            "columns": [{"id": "emp", "kind": "field", "ref": "e.name"},
                        {"id": "boss", "kind": "field", "ref": "m.name"}],
            "order_by": [{"ref": "emp"}]}
    ambiguous = {**base, "joins": [{"alias": "m", "from_alias": "e", "relationship_id": rel}]}
    rep = services.query.validate(parse_spec(ambiguous))
    assert not rep.valid and "needs a direction" in rep.issues[0].message
    spec = {**base, "joins": [{"alias": "m", "from_alias": "e", "relationship_id": rel,
                               "direction": "outgoing"}]}
    r = services.query.execute(parse_spec(spec), AccessContext.unrestricted(conn.id),
                               ExecutionLimits())
    rows = dict(zip(r.frame["emp"], r.frame["boss"], strict=True))
    assert rows["Ken"] == "Grace" and rows["Grace"] == "Ada"
    assert len(services.auth.list_users()) == 3
