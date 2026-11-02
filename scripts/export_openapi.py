"""Write the OpenAPI schema to frontend/openapi.json (for `npm run gen:api`)."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reporting.app.config import Settings  # noqa: E402
from reporting.app.main import create_app  # noqa: E402
from reporting.connections import generate_master_key  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    settings = Settings(app_db_url=f"sqlite:///{tmp}/app.db", master_key=generate_master_key(),
                        jwt_secret="x" * 40, admin_password="unused-password",
                        data_dir=Path(tmp))
    schema = create_app(settings).openapi()
(ROOT / "frontend" / "openapi.json").write_text(json.dumps(schema, indent=2))
print(f"wrote {len(schema['paths'])} paths")
