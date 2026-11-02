"""Public API of the query module: validates specs, compiles them and runs them."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import ConfigDict, Field, StringConstraints
from pydantic import ValidationError as PydanticValidationError

from reporting.core import (
    AccessContext,
    CompiledStatement,
    ExecutionLimits,
    Frozen,
    Issue,
    ValidationFailed,
)
from reporting.core.results import OutputColumn, QueryResult

if TYPE_CHECKING:
    from reporting.connections import ConnectionService
    from reporting.metadata import MetadataService

# -- limits (validation step 1) -------------------------------------------------------------

MAX_COLUMNS = 50
MAX_JOINS = 8
MAX_FILTERS = 30
MAX_EXPRESSION_CHARS = 500
MAX_LIMIT = 100_000

Alias = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,29}$")]
ColumnIdStr = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")]
FieldRefStr = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,29}\.[^.\s][^\n]{0,127}$")
]
Label = Annotated[str, StringConstraints(max_length=200)]
AggregateFn = Literal["avg", "sum", "count", "count_distinct", "min", "max"]
FilterOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "between", "like",
    "is_null", "is_not_null", "in_period",
]
# Preset date ranges for the "in_period" operator (resolved server-side, see periods.py).
Period = Literal["today", "last_7_days", "last_30_days", "last_90_days",
                 "this_week", "last_week", "this_month", "last_month",
                 "last_6_months", "this_year", "last_year"]
PERIODS: tuple[str, ...] = Period.__args__  # type: ignore[attr-defined]
Grain = Literal["day", "month", "year"]


class SpecModel(Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class TableName(SpecModel):
    database: str
    schema_: str | None = Field(default=None, alias="schema")
    name: str

    @property
    def key(self) -> str:
        return f"{self.schema_}.{self.name}" if self.schema_ else self.name


class BaseTable(SpecModel):
    alias: Alias
    table: TableName


class JoinSpec(SpecModel):
    alias: Alias
    from_alias: Alias
    relationship_id: str
    type: Literal["left", "inner"] = "left"
    # Only needed for self-referencing relationships, where both ends are the same table.
    direction: Literal["outgoing", "incoming"] | None = None


class DateDiffExpr(SpecModel):
    kind: Literal["date_diff"]
    unit: Literal["day"] = "day"
    start: FieldRefStr
    end: FieldRefStr


class FieldColumn(SpecModel):
    kind: Literal["field"]
    id: ColumnIdStr
    ref: FieldRefStr
    label: Label | None = None
    grain: Grain | None = None  # date columns only: group by day, month or year


class AggregateColumn(SpecModel):
    kind: Literal["aggregate"]
    id: ColumnIdStr
    fn: AggregateFn
    arg: FieldRefStr | DateDiffExpr | None = None  # None means COUNT(*)
    label: Label | None = None


class DateDiffColumn(SpecModel):
    kind: Literal["date_diff"]
    id: ColumnIdStr
    unit: Literal["day"] = "day"
    start: FieldRefStr
    end: FieldRefStr
    label: Label | None = None


class ExpressionColumn(SpecModel):
    kind: Literal["expression"]
    id: ColumnIdStr
    expr: Annotated[str, StringConstraints(min_length=1, max_length=MAX_EXPRESSION_CHARS)]
    label: Label | None = None


SpecColumn = Annotated[
    FieldColumn | AggregateColumn | DateDiffColumn | ExpressionColumn,
    Field(discriminator="kind"),
]


class Filter(SpecModel):
    ref: str = Field(max_length=200)
    op: FilterOp
    value: Any = None


class OrderBy(SpecModel):
    ref: str = Field(max_length=200)
    dir: Literal["asc", "desc"] = "asc"


class QuerySpec(SpecModel):
    version: Literal[1] = 1
    connection_id: str
    snapshot_version: int | None = None
    base: BaseTable
    joins: Annotated[list[JoinSpec], Field(max_length=MAX_JOINS)] = []
    columns: Annotated[list[SpecColumn], Field(min_length=1, max_length=MAX_COLUMNS)]
    filters: Annotated[list[Filter], Field(max_length=MAX_FILTERS)] = []
    having: Annotated[list[Filter], Field(max_length=MAX_FILTERS)] = []
    group_by: Literal["auto"] | Annotated[list[str], Field(max_length=MAX_COLUMNS)] = "auto"
    order_by: Annotated[list[OrderBy], Field(max_length=10)] = []
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] | None = 1000
    # IANA zone that defines "today" for preset periods (the browser sends its own).
    timezone: Annotated[str, Field(max_length=64)] | None = None


# -- results --------------------------------------------------------------------------------

class ValidationIssue(Frozen):
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"
    code: str = "invalid"


class ValidationReport(Frozen):
    valid: bool
    issues: tuple[ValidationIssue, ...] = ()
    output_columns: tuple[OutputColumn, ...] = ()
    snapshot_version: int | None = None

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]


class GapFill(Frozen):
    """Fill empty date buckets: one grained date dimension filtered to a known period."""

    column: str
    grain: Grain
    start: date
    end: date  # exclusive
    zero_columns: tuple[str, ...]  # sums and counts become 0 in empty buckets


class CompiledQuery(Frozen):
    statement_sql: str
    params: dict[str, Any]
    database: str
    dialect: str
    snapshot_version: int
    output_columns: tuple[OutputColumn, ...]
    warnings: tuple[ValidationIssue, ...] = ()
    gap_fill: GapFill | None = None

    @property
    def statement(self) -> CompiledStatement:
        return CompiledStatement(self.statement_sql, dict(self.params), database=self.database)


class QueryService(Protocol):
    def validate(self, spec: QuerySpec,
                 access: AccessContext | None = None) -> ValidationReport: ...
    def compile(self, spec: QuerySpec, access: AccessContext) -> CompiledQuery: ...
    def execute(self, spec: QuerySpec, access: AccessContext,
                limits: ExecutionLimits, use_cache: bool = True,
                max_cache_age_s: float | None = None) -> QueryResult: ...
    def preview(self, spec: QuerySpec, access: AccessContext) -> QueryResult: ...


def parse_spec(raw: Any) -> QuerySpec:
    """Shape validation (step 1) with JSON-path issues instead of a raw Pydantic error."""
    try:
        return QuerySpec.model_validate(raw)
    except PydanticValidationError as exc:
        issues = [Issue(path=_json_path(e["loc"]), message=e["msg"], code=e["type"])
                  for e in exc.errors(include_url=False)]
        raise ValidationFailed("The query spec is malformed", issues) from None


def _json_path(loc: tuple[int | str, ...]) -> str:
    out = ""
    kinds = {"field", "aggregate", "date_diff", "expression"}
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        elif part in kinds or part.startswith(("function-", "literal[", "list[", "str", "int")):
            continue  # discriminator / union branch tags are noise for users
        else:
            out += f".{part}" if out else part
    return out


def create_query_service(metadata: MetadataService, connections: ConnectionService,
                         cache_url: str | None = None, cache_ttl_s: int = 300,
                         clock: Callable[[], datetime] | None = None) -> QueryService:
    """``clock`` returns the current aware UTC time (tests pin it)."""
    from reporting.query._internal.service import SqlQueryService

    return SqlQueryService(metadata, connections, cache_url=cache_url, cache_ttl_s=cache_ttl_s,
                           clock=clock)


__all__ = [
    "AggregateColumn",
    "BaseTable",
    "CompiledQuery",
    "DateDiffColumn",
    "DateDiffExpr",
    "ExpressionColumn",
    "FieldColumn",
    "GapFill",
    "Grain",
    "PERIODS",
    "Period",
    "Filter",
    "JoinSpec",
    "OrderBy",
    "OutputColumn",
    "QueryResult",
    "QueryService",
    "QuerySpec",
    "TableName",
    "ValidationIssue",
    "ValidationReport",
    "create_query_service",
    "parse_spec",
]
