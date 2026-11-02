"""Secret vault: AES-256-GCM envelope encryption.

Each tenant has a random 256-bit data key. The data key is stored only in wrapped form,
encrypted under a master key (stand-in for a cloud KMS / HashiCorp Vault key). Secrets are
encrypted under the data key with a fresh 96-bit nonce and the connection id as associated
data, so a ciphertext cannot be swapped onto another connection row.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Column, LargeBinary, MetaData, String, Table, insert, select
from sqlalchemy.engine import Engine

from reporting.core import ReportingError

NONCE_BYTES = 12


def load_master_key(encoded: str | None) -> bytes:
    """Decode a base64 master key; must be exactly 32 bytes."""
    if not encoded:
        raise ReportingError("REPORTING_MASTER_KEY is not configured")
    key = base64.b64decode(encoded)
    if len(key) != 32:
        raise ReportingError("REPORTING_MASTER_KEY must decode to 32 bytes")
    return key


def generate_master_key() -> str:
    return base64.b64encode(AESGCM.generate_key(bit_length=256)).decode()


@dataclass(frozen=True)
class Sealed:
    nonce: bytes
    ciphertext: bytes


def _seal(key: bytes, plaintext: bytes, aad: bytes) -> Sealed:
    nonce = os.urandom(NONCE_BYTES)
    return Sealed(nonce=nonce, ciphertext=AESGCM(key).encrypt(nonce, plaintext, aad))


def _open(key: bytes, sealed: Sealed, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad)
    except InvalidTag as exc:
        raise ReportingError("secret could not be decrypted (wrong key or tampered data)") from exc


class Vault:
    def __init__(self, engine: Engine, metadata: MetaData, master_key: bytes) -> None:
        self._engine = engine
        self._master = master_key
        self._keys = Table(
            "connections_data_keys",
            metadata,
            Column("tenant", String(64), primary_key=True),
            Column("nonce", LargeBinary, nullable=False),
            Column("wrapped_key", LargeBinary, nullable=False),
        )
        self._cache: dict[str, bytes] = {}

    def _data_key(self, tenant: str) -> bytes:
        if tenant in self._cache:
            return self._cache[tenant]
        aad = f"tenant:{tenant}".encode()
        with self._engine.begin() as conn:
            row = conn.execute(select(self._keys).where(self._keys.c.tenant == tenant)).first()
            if row is None:
                key = AESGCM.generate_key(bit_length=256)
                wrapped = _seal(self._master, key, aad)
                conn.execute(
                    insert(self._keys).values(
                        tenant=tenant, nonce=wrapped.nonce, wrapped_key=wrapped.ciphertext
                    )
                )
            else:
                key = _open(self._master, Sealed(row.nonce, row.wrapped_key), aad)
        self._cache[tenant] = key
        return key

    def encrypt(self, tenant: str, subject: str, secret: str) -> Sealed:
        return _seal(self._data_key(tenant), secret.encode(), subject.encode())

    def decrypt(self, tenant: str, subject: str, sealed: Sealed) -> str:
        return _open(self._data_key(tenant), sealed, subject.encode()).decode()
