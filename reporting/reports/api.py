"""Public API of the reports module: report documents, templates and embeds.

Documents are declarative JSON: a grid layout of elements with closed-vocabulary styling.
Data elements carry a query spec as plain JSON; the HTTP layer validates it through ``query``
(this module imports only ``core``, keeping module dependencies one-way).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from reporting.core import Frozen

GRID_COLUMNS = 12
MAX_ELEMENTS = 50
MAX_TEXT = 10_000
MAX_INLINE_IMAGE = 1_400_000  # ~1 MB once base64-decoded
MIN_REFRESH_S = 30

ElementType = Literal["text", "table", "chart", "image"]
DocumentKind = Literal["template", "report"]
ElementId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")]


class Layout(Frozen):
    """Position and size in grid cells (12 columns, fixed row height)."""

    x: int = Field(ge=0, lt=GRID_COLUMNS)
    y: int = Field(ge=0, le=1000)
    w: int = Field(ge=1, le=GRID_COLUMNS)
    h: int = Field(ge=1, le=60)

    @model_validator(mode="after")
    def _fits(self) -> Layout:
        if self.x + self.w > GRID_COLUMNS:
            raise ValueError(f"x + w must be at most {GRID_COLUMNS}")
        return self


class Style(Frozen):
    background: Literal["none", "surface", "muted", "accent"] = "surface"
    border: bool = True
    align: Literal["left", "center", "right"] = "left"


class TextContent(Frozen):
    content: str = Field(default="", max_length=MAX_TEXT)
    variant: Literal["title", "heading", "body", "section"] = "body"


class ImageContent(Frozen):
    url: str = Field(max_length=MAX_INLINE_IMAGE)
    alt: str = Field(default="", max_length=300)
    fit: Literal["contain", "cover"] = "contain"

    @field_validator("url")
    @classmethod
    def _safe_url(cls, v: str) -> str:
        if v.startswith("https://") or re.match(r"^data:image/(png|jpeg|gif|webp);base64,", v):
            return v
        raise ValueError("image must be an https:// URL or a data:image (png, jpeg, gif, webp)")


class Binding(Frozen):
    """Data source of a table or chart: a query spec, plus chart options for charts."""

    query: dict[str, Any]
    chart: dict[str, Any] | None = None
    page_size: int = Field(default=100, ge=1, le=500)


class Element(Frozen):
    id: ElementId
    type: ElementType
    title: str = Field(default="", max_length=200)
    layout: Layout
    style: Style = Style()
    text: TextContent | None = None
    image: ImageContent | None = None
    binding: Binding | None = None

    @model_validator(mode="after")
    def _content_matches_type(self) -> Element:
        if self.type in ("table", "chart") and self.binding is None:
            raise ValueError(f"a {self.type} element needs a data binding")
        if self.type == "text" and self.text is None:
            raise ValueError("a text element needs text")
        if self.type == "image" and self.image is None:
            raise ValueError("an image element needs an image")
        return self


ParameterType = Literal["date_range", "equals", "one_of"]
ParamId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
MAX_PARAMETERS = 10


class ParameterTarget(Frozen):
    """Which column of which data element a report filter applies to."""

    element_id: ElementId
    ref: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,29}\.[^.\s][^\n]{0,127}$")


class Parameter(Frozen):
    """A report filter whose value is chosen when the report is viewed or embedded.

    ``date_range`` takes a preset period (e.g. ``last_30_days``); ``equals`` one value;
    ``one_of`` a list of values. ``locked`` filters can only be set by a signed embed
    token (never from the URL), so they are safe for per-customer scoping.
    """

    id: ParamId
    label: str = Field(min_length=1, max_length=100)
    type: ParameterType = "date_range"
    required: bool = False
    locked: bool = False
    default: str | list[str] | None = None
    targets: Annotated[list[ParameterTarget], Field(min_length=1, max_length=MAX_ELEMENTS)]

    @model_validator(mode="after")
    def _default_shape(self) -> Parameter:
        if self.default is not None:
            if self.type == "one_of" and not isinstance(self.default, list):
                raise ValueError("a 'one of' filter's default must be a list")
            if self.type != "one_of" and isinstance(self.default, list):
                raise ValueError(f"a '{self.type}' filter's default must be a single value")
        return self


class PageSettings(Frozen):
    columns: Literal[12] = 12
    row_height: int = Field(default=40, ge=20, le=120)
    max_width: int = Field(default=1200, ge=600, le=2400)


class Theme(Frozen):
    accent: str = Field(default="#2563eb", pattern=r"^#[0-9a-fA-F]{6}$")
    density: Literal["comfortable", "compact"] = "comfortable"


class ReportDocument(Frozen):
    schema_version: Literal[1] = 1
    page: PageSettings = PageSettings()
    theme: Theme = Theme()
    elements: Annotated[list[Element], Field(max_length=MAX_ELEMENTS)] = []
    parameters: Annotated[list[Parameter], Field(max_length=MAX_PARAMETERS)] = []

    @model_validator(mode="after")
    def _unique_ids(self) -> ReportDocument:
        ids = [e.id for e in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("element ids must be unique")
        pids = [p.id for p in self.parameters]
        if len(pids) != len(set(pids)):
            raise ValueError("filter ids must be unique")
        data_ids = {e.id for e in self.elements if e.binding is not None}
        for p in self.parameters:
            for t in p.targets:
                if t.element_id not in data_ids:
                    raise ValueError(f"filter '{p.id}' targets '{t.element_id}', which is not "
                                     f"a table or chart in this report")
        return self

    def element(self, element_id: str) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)


class SaveReport(Frozen):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    kind: DocumentKind = "report"
    document: ReportDocument = ReportDocument()
    version: int | None = None  # required on update: the version that was read


class Report(Frozen):
    id: str
    owner_id: str
    kind: DocumentKind
    name: str
    description: str
    document: ReportDocument
    version: int
    source_id: str | None
    created_at: datetime
    updated_at: datetime


class ReportSummary(Frozen):
    id: str
    owner_id: str
    kind: DocumentKind
    name: str
    description: str
    element_count: int
    version: int
    updated_at: datetime


# -- embeds ---------------------------------------------------------------------------------

EmbedMode = Literal["public", "signed"]


class EmbedOptions(Frozen):
    theme: Literal["light", "dark", "auto"] = "auto"
    refresh_interval_s: int = Field(default=0, ge=0, le=86_400)
    show_title: bool = True
    allow_download: bool = False

    @field_validator("refresh_interval_s")
    @classmethod
    def _min_refresh(cls, v: int) -> int:
        if 0 < v < MIN_REFRESH_S:
            raise ValueError(f"refresh interval must be off (0) or at least {MIN_REFRESH_S} s")
        return v


Origin = Annotated[str, Field(pattern=r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$", max_length=255)]


class CreateEmbed(Frozen):
    element_id: ElementId
    mode: EmbedMode = "signed"
    options: EmbedOptions = EmbedOptions()
    allowed_origins: Annotated[list[Origin], Field(max_length=20)] = []
    expires_at: datetime | None = None


class Embed(Frozen):
    id: str
    document_id: str
    element_id: str
    owner_id: str
    mode: EmbedMode
    options: EmbedOptions
    allowed_origins: tuple[str, ...]
    secret_version: int
    expires_at: datetime | None
    revoked_at: datetime | None
    view_count: int
    last_viewed_at: datetime | None
    created_at: datetime

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > now)


class EmbedClaims(Frozen):
    embed: str
    exp: int


class ReportService(Protocol):
    def create(self, owner_id: str, req: SaveReport, source_id: str | None = None) -> Report: ...
    def get(self, report_id: str) -> Report: ...
    def list_documents(self, kind: DocumentKind | None = None,
                       owner_id: str | None = None) -> list[ReportSummary]: ...
    def update(self, report_id: str, req: SaveReport) -> Report: ...
    def delete(self, report_id: str) -> None: ...

    def create_embed(self, report_id: str, owner_id: str, req: CreateEmbed) -> Embed: ...
    def get_embed(self, embed_id: str) -> Embed: ...
    def list_embeds(self, report_id: str) -> list[Embed]: ...
    def revoke_embed(self, embed_id: str) -> Embed: ...
    def rotate_embed_secret(self, embed_id: str) -> Embed: ...
    def embed_secret(self, embed: Embed) -> str: ...
    def verify_embed_token(self, embed: Embed, token: str | None) -> dict[str, Any]:
        """Check access; return the token's signed filter values (``params``)."""
        ...
    def record_view(self, embed_id: str, viewer: str) -> None: ...
    def flush_views(self) -> None: ...


def resolve_parameters(document: ReportDocument, element_id: str, *,
                       url_values: dict[str, str] | None = None,
                       token_values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Filters (query-spec ``Filter`` dicts) that the report's parameters add to an element.

    Precedence: signed token > URL (unlocked filters only) > default. Raises
    ``ValidationFailed`` for a required filter without a value, an unknown filter name, or an
    attempt to set a locked filter from the URL.
    """
    from reporting.core import Issue, ValidationFailed

    url_values = url_values or {}
    token_values = token_values or {}
    known = {p.id: p for p in document.parameters}
    issues: list[Issue] = []
    for name in (*url_values, *token_values):
        if name not in known:
            issues.append(Issue(f"params.{name}", f"Unknown filter '{name}'"))
    filters: list[dict[str, Any]] = []
    for p in document.parameters:
        if p.locked and p.id in url_values:
            issues.append(Issue(f"params.{p.id}",
                                f"'{p.label}' can only be set by a signed embed token"))
            continue
        raw: Any = token_values.get(p.id, url_values.get(p.id, p.default))
        if raw in (None, "", []):
            if p.required:
                issues.append(Issue(f"params.{p.id}", f"This report needs the filter '{p.label}'"))
            continue
        if p.type == "one_of":
            value: Any = [str(v).strip() for v in (raw if isinstance(raw, list) else
                                                   str(raw).split(",")) if str(v).strip()]
        elif isinstance(raw, list):
            issues.append(Issue(f"params.{p.id}", f"'{p.label}' takes a single value"))
            continue
        else:
            value = str(raw)
        op = {"date_range": "in_period", "equals": "eq", "one_of": "in"}[p.type]
        filters += [{"ref": t.ref, "op": op, "value": value}
                    for t in p.targets if t.element_id == element_id]
    if issues:
        raise ValidationFailed(issues[0].message, issues)
    return filters


def create_report_service(app_db_url: str, embed_key: str) -> ReportService:
    from reporting.reports._internal.service import SqlReportService

    return SqlReportService(app_db_url, embed_key)


__all__ = [
    "GRID_COLUMNS",
    "MAX_ELEMENTS",
    "Binding",
    "CreateEmbed",
    "DocumentKind",
    "Element",
    "ElementType",
    "Embed",
    "EmbedClaims",
    "EmbedMode",
    "EmbedOptions",
    "ImageContent",
    "Parameter",
    "ParameterTarget",
    "ParameterType",
    "Layout",
    "PageSettings",
    "Report",
    "ReportDocument",
    "ReportService",
    "ReportSummary",
    "SaveReport",
    "Style",
    "TextContent",
    "Theme",
    "create_report_service",
    "resolve_parameters",
]
