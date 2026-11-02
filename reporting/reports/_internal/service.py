"""SQLAlchemy-backed ``ReportService``."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import jwt
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
    update,
)

from reporting.core import Conflict, NotFound, PermissionDenied, ValidationFailed, new_id
from reporting.core.db import create_app_engine, create_tables
from reporting.reports.api import (
    CreateEmbed,
    DocumentKind,
    Embed,
    EmbedOptions,
    Report,
    ReportDocument,
    ReportSummary,
    SaveReport,
)

TOKEN_MAX_LIFETIME_S = 24 * 3600
CLOCK_SKEW_S = 60
VIEW_DEDUP_S = 30 * 60  # one view per viewer per embed per 30 minutes
VIEW_FLUSH_S = 60  # view counts reach the database at most once a minute


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(d: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; values are stored in UTC."""
    return d.replace(tzinfo=UTC) if d is not None and d.tzinfo is None else d


class SqlReportService:
    def __init__(self, app_db_url: str, embed_key: str) -> None:
        if len(embed_key) < 32:
            raise ValueError("embed key must be at least 32 characters")
        self._key = embed_key.encode()
        self._db = create_app_engine(app_db_url)
        md = MetaData()
        self._docs = Table(
            "reports_documents", md,
            Column("id", String(32), primary_key=True),
            Column("owner_id", String(32), nullable=False, index=True),
            Column("kind", String(16), nullable=False),
            Column("name", String(200), nullable=False),
            Column("description", Text, nullable=False, default=""),
            Column("document", Text, nullable=False),
            Column("version", Integer, nullable=False),
            Column("source_id", String(32)),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("updated_at", DateTime(timezone=True), nullable=False),
        )
        self._embeds = Table(
            "reports_embeds", md,
            Column("id", String(40), primary_key=True),
            Column("document_id", String(32), nullable=False, index=True),
            Column("element_id", String(40), nullable=False),
            Column("owner_id", String(32), nullable=False),
            Column("mode", String(10), nullable=False),
            Column("options", Text, nullable=False),
            Column("allowed_origins", Text, nullable=False),
            Column("secret_version", Integer, nullable=False, default=1),
            Column("expires_at", DateTime(timezone=True)),
            Column("revoked_at", DateTime(timezone=True)),
            Column("view_count", Integer, nullable=False, default=0),
            Column("last_viewed_at", DateTime(timezone=True)),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
        create_tables(self._db, md)
        # View counting is buffered: a page view must never cost a database write.
        self._views_lock = threading.Lock()
        self._pending_views: Counter[str] = Counter()
        self._last_view: dict[tuple[str, str], float] = {}
        self._last_flush = time.monotonic()

    # -- documents ------------------------------------------------------------------------

    def create(self, owner_id: str, req: SaveReport, source_id: str | None = None) -> Report:
        rid = new_id("rpt")
        now = _now()
        with self._db.begin() as conn:
            conn.execute(insert(self._docs).values(
                id=rid, owner_id=owner_id, kind=req.kind, name=req.name.strip(),
                description=req.description, document=req.document.model_dump_json(),
                version=1, source_id=source_id, created_at=now, updated_at=now,
            ))
        return self.get(rid)

    def get(self, report_id: str) -> Report:
        with self._db.connect() as conn:
            row = conn.execute(select(self._docs).where(self._docs.c.id == report_id)).first()
        if row is None:
            raise NotFound(f"Report {report_id} not found")
        return Report(
            id=row.id, owner_id=row.owner_id, kind=row.kind, name=row.name,
            description=row.description, document=ReportDocument.model_validate_json(row.document),
            version=row.version, source_id=row.source_id,
            created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        )

    def list_documents(self, kind: DocumentKind | None = None,
                       owner_id: str | None = None) -> list[ReportSummary]:
        stmt = select(self._docs).order_by(self._docs.c.updated_at.desc())
        if kind:
            stmt = stmt.where(self._docs.c.kind == kind)
        if owner_id:
            stmt = stmt.where(self._docs.c.owner_id == owner_id)
        with self._db.connect() as conn:
            rows = conn.execute(stmt).all()
        return [ReportSummary(
            id=r.id, owner_id=r.owner_id, kind=r.kind, name=r.name, description=r.description,
            element_count=len(json.loads(r.document).get("elements", [])), version=r.version,
            updated_at=_aware(r.updated_at),
        ) for r in rows]

    def update(self, report_id: str, req: SaveReport) -> Report:
        if req.version is None:
            raise ValidationFailed("version is required when saving changes")
        with self._db.begin() as conn:
            result = conn.execute(
                update(self._docs)
                .where(self._docs.c.id == report_id, self._docs.c.version == req.version)
                .values(name=req.name.strip(), description=req.description, kind=req.kind,
                        document=req.document.model_dump_json(),
                        version=self._docs.c.version + 1, updated_at=_now()))
            if result.rowcount == 0:
                exists = conn.execute(select(self._docs.c.version)
                                      .where(self._docs.c.id == report_id)).scalar()
                if exists is None:
                    raise NotFound(f"Report {report_id} not found")
                raise Conflict("Someone else saved this report since you opened it",
                               details={"current_version": exists})
        return self.get(report_id)

    def delete(self, report_id: str) -> None:
        self.get(report_id)
        with self._db.begin() as conn:
            conn.execute(update(self._embeds).where(
                self._embeds.c.document_id == report_id, self._embeds.c.revoked_at.is_(None),
            ).values(revoked_at=_now()))
            conn.execute(delete(self._docs).where(self._docs.c.id == report_id))

    # -- embeds ---------------------------------------------------------------------------

    def create_embed(self, report_id: str, owner_id: str, req: CreateEmbed) -> Embed:
        report = self.get(report_id)
        if report.document.element(req.element_id) is None:
            raise ValidationFailed(f"Element {req.element_id} is not in this report")
        if req.expires_at is not None and _aware(req.expires_at) <= _now():  # type: ignore[operator]
            raise ValidationFailed("Expiry must be in the future")
        eid = "emb_" + secrets.token_urlsafe(16)
        with self._db.begin() as conn:
            conn.execute(insert(self._embeds).values(
                id=eid, document_id=report_id, element_id=req.element_id, owner_id=owner_id,
                mode=req.mode, options=req.options.model_dump_json(),
                allowed_origins=json.dumps(sorted(set(req.allowed_origins))),
                secret_version=1, expires_at=_aware(req.expires_at), view_count=0,
                created_at=_now(),
            ))
        return self.get_embed(eid)

    def get_embed(self, embed_id: str) -> Embed:
        with self._db.connect() as conn:
            row = conn.execute(select(self._embeds).where(self._embeds.c.id == embed_id)).first()
        if row is None:
            raise NotFound("This embed does not exist")
        return self._embed(row)

    @staticmethod
    def _embed(row) -> Embed:  # type: ignore[no-untyped-def]
        return Embed(
            id=row.id, document_id=row.document_id, element_id=row.element_id,
            owner_id=row.owner_id, mode=row.mode,
            options=EmbedOptions.model_validate_json(row.options),
            allowed_origins=tuple(json.loads(row.allowed_origins)),
            secret_version=row.secret_version, expires_at=_aware(row.expires_at),
            revoked_at=_aware(row.revoked_at), view_count=row.view_count,
            last_viewed_at=_aware(row.last_viewed_at), created_at=_aware(row.created_at),
        )

    def list_embeds(self, report_id: str) -> list[Embed]:
        self.flush_views()
        with self._db.connect() as conn:
            rows = conn.execute(select(self._embeds)
                                .where(self._embeds.c.document_id == report_id)
                                .order_by(self._embeds.c.created_at.desc())).all()
        return [self._embed(r) for r in rows]

    def revoke_embed(self, embed_id: str) -> Embed:
        self.get_embed(embed_id)
        with self._db.begin() as conn:
            conn.execute(update(self._embeds).where(self._embeds.c.id == embed_id,
                                                    self._embeds.c.revoked_at.is_(None))
                         .values(revoked_at=_now()))
        return self.get_embed(embed_id)

    def rotate_embed_secret(self, embed_id: str) -> Embed:
        embed = self.get_embed(embed_id)
        if embed.mode != "signed":
            raise ValidationFailed("Only signed embeds have a secret")
        with self._db.begin() as conn:
            conn.execute(update(self._embeds).where(self._embeds.c.id == embed_id)
                         .values(secret_version=self._embeds.c.secret_version + 1))
        return self.get_embed(embed_id)

    def embed_secret(self, embed: Embed) -> str:
        """Derived, never stored: HMAC(server key, "id:version"). Rotation bumps the version."""
        mac = hmac.new(self._key, f"{embed.id}:{embed.secret_version}".encode(), hashlib.sha256)
        return base64.urlsafe_b64encode(mac.digest()).rstrip(b"=").decode()

    def verify_embed_token(self, embed: Embed, token: str | None) -> dict[str, Any]:
        if not embed.active(_now()):
            raise PermissionDenied("This embed has been revoked or has expired")
        if embed.mode == "public":
            return {}  # public embeds cannot carry signed (locked) filter values
        if not token:
            raise PermissionDenied("This embed needs a signed token")
        try:
            claims = jwt.decode(token, self.embed_secret(embed), algorithms=["HS256"],
                                leeway=CLOCK_SKEW_S,
                                options={"require": ["exp", "embed"], "verify_iat": False})
        except jwt.ExpiredSignatureError:
            raise PermissionDenied("The embed token has expired") from None
        except jwt.PyJWTError:
            raise PermissionDenied("The embed token is invalid") from None
        if claims.get("embed") != embed.id:
            raise PermissionDenied("The embed token is for a different embed")
        if int(claims["exp"]) > time.time() + TOKEN_MAX_LIFETIME_S + CLOCK_SKEW_S:
            raise PermissionDenied("Embed tokens may be valid for at most 24 hours")
        params = claims.get("params", {})
        if not isinstance(params, dict):
            raise PermissionDenied("The embed token's params must be an object")
        return params

    def record_view(self, embed_id: str, viewer: str) -> None:
        now = time.monotonic()
        with self._views_lock:
            key = (embed_id, viewer)
            if now - self._last_view.get(key, -VIEW_DEDUP_S) >= VIEW_DEDUP_S:
                self._last_view[key] = now
                self._pending_views[embed_id] += 1
            if len(self._last_view) > 50_000:
                self._last_view = {k: t for k, t in self._last_view.items()
                                   if now - t < VIEW_DEDUP_S}
            due = now - self._last_flush >= VIEW_FLUSH_S
        if due:
            self.flush_views()

    def flush_views(self) -> None:
        with self._views_lock:
            pending, self._pending_views = self._pending_views, Counter()
            self._last_flush = time.monotonic()
        if not pending:
            return
        stamp = _now()
        with self._db.begin() as conn:
            for embed_id, n in pending.items():
                conn.execute(update(self._embeds).where(self._embeds.c.id == embed_id).values(
                    view_count=self._embeds.c.view_count + n, last_viewed_at=stamp))
