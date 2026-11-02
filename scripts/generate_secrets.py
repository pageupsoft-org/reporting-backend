"""Print fresh production secrets as .env lines.

Usage:  python scripts/generate_secrets.py >> .env
Store them in your secret manager; losing REPORTING_MASTER_KEY makes saved connection
passwords unrecoverable.
"""

import base64
import os
import secrets

print(f"REPORTING_MASTER_KEY={base64.b64encode(os.urandom(32)).decode()}")
print(f"REPORTING_JWT_SECRET={secrets.token_urlsafe(48)}")
print(f"REPORTING_EMBED_KEY={secrets.token_urlsafe(48)}")
