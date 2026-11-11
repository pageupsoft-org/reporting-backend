"""Validation pipeline (plan §3.2). Produces a resolved ``Plan`` the compiler can trust.

Steps: 1 shape (Pydantic, done by ``parse_spec``), 2 snapshot, 3 references, 4 access,
5 types, 6 semantics (group-by, nesting, fan-out), 7 expressions.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reporting.core import AccessContext, DataType, StaleSnapshot
from reporting.metadata import MetadataService, RelationshipDef, SchemaSnapshot, TableSchema
from reporting.query._internal.expr.ast import FuncCall, Node, walk
from reporting.query._internal.expr.checker import CalcInfo, Checker, CheckResult, _numeric_result
from reporting.query._internal.expr.parser import ExprError, parse
from reporting.query._internal.periods import LABELS, resolve_period
from reporting.query.api import (
    PERIODS,
    AggregateColumn,
    DateDiffColumn,
    DateDiffExpr,
    ExpressionColumn,
    FieldColumn,
    Filter,
    GapFill,
    OutputColumn,
    QuerySpec,
    ValidationIssue,
)

MAX_IN_VALUES = 1000
FANOUT_FNS = {"sum", "avg", "count"}
TYPE_WORD = {
    DataType.INTEGER: "a number", DataType.DECIMAL: "a number", DataType.FLOAT: "a number",
    DataType.TEXT: "text", DataType.BOOLEAN: "true/false", DataType.DATE: "a date",
    DataType.DATETIME: "a date", DataType.DATETIME_TZ: "a date", DataType.UUID: "an id",
    DataType.JSON: "JSON", DataType.OTHER: "an unsupported type",
}


@dataclass
class ResolvedJoin:
    alias: str
    from_alias: str
    table: TableSchema
    relationship: RelationshipDef
    pairs: tuple[tuple[str, str], ...]  # (from_alias column, alias column)
    cardinality: str  # from from_alias to alias
    type: str


@dataclass
class ResolvedColumn:
    index: int
    spec: FieldColumn | AggregateColumn | DateDiffColumn | ExpressionColumn
    data_type: DataType
    aggregate: bool
    arg_type: DataType | None = None
    ast: Node | None = None
    checked: CheckResult | None = None

    @property
    def id(self) -> str:
        return self.spec.id


@dataclass
class ResolvedFilter:
    path: str
    op: str
    value: Any
    data_type: DataType
    field: tuple[str, str] | None = None  # (alias, column)
    column_id: str | None = None
    period: tuple[date, date] | None = None  # resolved "in_period" days, end exclusive


@dataclass
class PeriodContext:
    """What "today" means for this request."""

    today: date
    tz: tzinfo


@dataclass
class ResolvedOrder:
    dir: str
    field: tuple[str, str] | None = None
    column_id: str | None = None


@dataclass
class Plan:
    spec: QuerySpec
    snapshot: SchemaSnapshot
    aliases: dict[str, TableSchema]
    joins: list[ResolvedJoin]
    columns: list[ResolvedColumn]  # output order
    eval_order: list[str]  # column ids in dependency order
    aggregated: bool
    group_ids: list[str]
    filters: list[ResolvedFilter]
    having: list[ResolvedFilter]
    order_by: list[ResolvedOrder]
    output: list[OutputColumn]
    warnings: list[ValidationIssue] = field(default_factory=list)
    gap_fill: GapFill | None = None
    # Extra GROUP BY fields: bare columns used beside aggregates in expressions (auto mode).
    group_fields: list[tuple[str, str]] = field(default_factory=list)

    def column(self, cid: str) -> ResolvedColumn:
        return next(c for c in self.columns if c.id == cid)


class _Issues:
    def __init__(self) -> None:
        self.items: list[ValidationIssue] = []

    def error(self, path: str, message: str, code: str = "invalid") -> None:
        self.items.append(ValidationIssue(path=path, message=message, code=code))

    def warn(self, path: str, message: str, code: str = "warning") -> None:
        self.items.append(ValidationIssue(path=path, message=message, severity="warning",
                                          code=code))

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.items)


def _split_ref(ref: str) -> tuple[str, str]:
    alias, _, column = ref.partition(".")
    return alias, column


def coerce_value(dtype: DataType, value: Any) -> Any:
    """Coerce a filter value to the column's type; raises ``ValueError`` with a user message."""
    if value is None:
        raise ValueError("a value is required")
    if dtype == DataType.INTEGER:
        if isinstance(value, bool):
            raise ValueError("expected a whole number")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{value} is not a whole number")
        try:
            return int(str(value)) if not isinstance(value, float) else int(value)
        except ValueError:
            raise ValueError(f"'{value}' is not a whole number") from None
    if dtype in (DataType.DECIMAL, DataType.FLOAT):
        if isinstance(value, bool):
            raise ValueError("expected a number")
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            raise ValueError(f"'{value}' is not a number") from None
        if not d.is_finite():
            raise ValueError("number must be finite")
        return d if dtype == DataType.DECIMAL else float(d)
    if dtype == DataType.TEXT:
        if isinstance(value, (dict, list)):
            raise ValueError("expected text")
        return str(value)
    if dtype == DataType.BOOLEAN:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        raise ValueError(f"'{value}' is not true or false")
    if dtype == DataType.DATE:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            raise ValueError(f"'{value}' is not a date (YYYY-MM-DD)") from None
    if dtype in (DataType.DATETIME, DataType.DATETIME_TZ):
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            raise ValueError(f"'{value}' is not a date/time (ISO 8601)") from None
        if dtype == DataType.DATETIME_TZ and dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    if dtype == DataType.UUID:
        try:
            return str(uuid.UUID(str(value)))
        except ValueError:
            raise ValueError(f"'{value}' is not a UUID") from None
    raise ValueError(f"cannot filter on {TYPE_WORD[dtype]} values")


def _period_bound(d: date, dtype: DataType, tz: tzinfo) -> date | datetime:
    """A period boundary in the column's own type (midnight in ``tz`` for aware columns)."""
    if dtype == DataType.DATE:
        return d
    midnight = datetime(d.year, d.month, d.day)
    return midnight.replace(tzinfo=tz) if dtype == DataType.DATETIME_TZ else midnight


class Validator:
    def __init__(self, metadata: MetadataService,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._metadata = metadata
        self._clock = clock or (lambda: datetime.now(UTC))

    def _period_context(self, spec: QuerySpec, issues: _Issues) -> PeriodContext:
        tz: tzinfo
        try:
            tz = ZoneInfo(spec.timezone) if spec.timezone else \
                datetime.now().astimezone().tzinfo or UTC
        except (ZoneInfoNotFoundError, ValueError):
            issues.error("timezone", f"Unknown time zone '{spec.timezone}'")
            tz = UTC
        return PeriodContext(today=self._clock().astimezone(tz).date(), tz=tz)

    def run(self, spec: QuerySpec, access: AccessContext | None) -> tuple[Plan | None,
                                                                           list[ValidationIssue]]:
        issues = _Issues()
        period_ctx = self._period_context(spec, issues)

        # -- step 2: snapshot -------------------------------------------------------------
        db = spec.base.table.database
        current = self._metadata.snapshot(spec.connection_id, db)
        if spec.snapshot_version is not None and spec.snapshot_version != current.version:
            diff = self._metadata.diff(spec.connection_id, db, spec.snapshot_version,
                                       current.version)
            lines = diff.describe() or ["the schema was refreshed"]
            raise StaleSnapshot(
                "Schema changed: " + "; ".join(lines),
                details={"current_version": current.version, "diff": diff.model_dump()},
            )
        snap = current

        # -- step 3: references -----------------------------------------------------------
        aliases: dict[str, TableSchema] = {}
        base_table = snap.table(spec.base.table.key)
        if base_table is None:
            issues.error("base.table", f"Table {spec.base.table.key} does not exist")
            return None, issues.items
        aliases[spec.base.alias] = base_table

        joins: list[ResolvedJoin] = []
        for i, j in enumerate(spec.joins):
            path = f"joins[{i}]"
            if j.alias in aliases:
                issues.error(f"{path}.alias", f"Alias '{j.alias}' is used twice")
                continue
            if j.from_alias not in aliases:
                issues.error(f"{path}.from_alias",
                             f"Alias '{j.from_alias}' is not defined before this join")
                continue
            rel = snap.relationship(j.relationship_id)
            if rel is None:
                issues.error(f"{path}.relationship_id",
                             f"Relationship '{j.relationship_id}' does not exist")
                continue
            src = aliases[j.from_alias]
            forward = rel.from_table == src.key
            backward = rel.to_table == src.key
            if forward and backward:  # self-reference
                if j.direction is None:
                    issues.error(f"{path}.direction",
                                 "Self-referencing relationship needs a direction")
                    continue
                forward = j.direction == "outgoing"
            if forward:
                target_key, pairs = rel.to_table, rel.column_pairs
                card = "one_to_one" if rel.cardinality == "one_to_one" else "many_to_one"
            elif backward:
                target_key = rel.from_table
                pairs = tuple((b, a) for a, b in rel.column_pairs)
                card = "one_to_one" if rel.cardinality == "one_to_one" else "one_to_many"
            else:
                other = rel.to_table if rel.from_table != src.key else rel.from_table
                issues.error(path, f"relationship does not connect {j.from_alias} to {other}")
                continue
            target = snap.table(target_key)
            assert target is not None
            aliases[j.alias] = target
            joins.append(ResolvedJoin(j.alias, j.from_alias, target, rel, pairs, card, j.type))

        def resolve_field(path: str, ref: str) -> tuple[str, str, DataType] | None:
            alias, column = _split_ref(ref)
            table = aliases.get(alias)
            if table is None:
                issues.error(path, f"Unknown alias '{alias}' in {ref}")
                return None
            col = table.column(column)
            if col is None:
                issues.error(path, f"Column {table.name}.{column} does not exist")
                return None
            # -- step 4: access --
            if access is not None and not access.column_visible(table.key, column):
                issues.error(path, f"{ref} is not visible to your role", code="forbidden")
                return None
            return alias, column, col.data_type

        if access is not None:
            for alias, table in aliases.items():
                if not access.table_visible(table.key):
                    issues.error("base.table" if alias == spec.base.alias else "joins",
                                 f"Table {table.key} is not visible to your role",
                                 code="forbidden")

        ids: dict[str, int] = {}
        for i, col in enumerate(spec.columns):
            if col.id in ids:
                issues.error(f"columns[{i}].id", f"Column id '{col.id}' is used twice")
            ids[col.id] = i

        # -- step 5: types (non-expression columns) ---------------------------------------
        resolved: dict[str, ResolvedColumn] = {}
        expr_cols: list[tuple[int, ExpressionColumn]] = []
        for i, col in enumerate(spec.columns):
            path = f"columns[{i}]"
            if isinstance(col, FieldColumn):
                r = resolve_field(f"{path}.ref", col.ref)
                if r and col.grain and not r[2].is_temporal:
                    issues.error(f"{path}.grain", f"Grouping by {col.grain} needs a date; "
                                                  f"{col.ref} is {TYPE_WORD[r[2]]}")
                elif r:
                    dtype = DataType.DATE if col.grain else r[2]
                    resolved[col.id] = ResolvedColumn(i, col, dtype, aggregate=False)
            elif isinstance(col, DateDiffColumn):
                if self._check_date_diff(path, col.start, col.end, resolve_field, issues):
                    resolved[col.id] = ResolvedColumn(i, col, DataType.INTEGER, aggregate=False)
            elif isinstance(col, AggregateColumn):
                rc = self._check_aggregate(path, col, resolve_field, issues)
                if rc:
                    resolved[col.id] = rc
            else:
                expr_cols.append((i, col))

        # -- step 7: expressions (parse, references, cycles, types) -----------------------
        explicit_group = None if spec.group_by == "auto" else list(spec.group_by)

        def grouped(rc: ResolvedColumn) -> bool:
            return not rc.aggregate and (explicit_group is None or rc.id in explicit_group)

        grouped_fields = {
            c.ref for c in spec.columns
            if isinstance(c, FieldColumn) and (explicit_group is None or c.id in explicit_group)
        }
        parsed: dict[str, Node] = {}
        for i, col in expr_cols:
            try:
                parsed[col.id] = parse(col.expr)
            except ExprError as exc:
                issues.error(f"columns[{i}].expr", f"{col.id}: {exc.message}", code="expression")
        order = self._topo_order(spec, parsed, ids, issues)
        for cid in order:
            i = ids[cid]
            col = spec.columns[i]
            assert isinstance(col, ExpressionColumn)
            path = f"columns[{i}].expr"

            def field_type(alias: str, column: str, _path: str = path) -> DataType:
                r = resolve_field(_path, f"{alias}.{column}")
                if r is None:
                    issues.items.pop()  # re-raised below with the expression's id
                    raise ExprError(self._field_problem(aliases, access, alias, column))
                return r[2]

            def calc_info(name: str) -> CalcInfo:
                rc = resolved.get(name)
                if rc is None:
                    raise ExprError(f"[{name}] has errors; fix it first")
                return CalcInfo(rc.data_type, rc.aggregate, grouped(rc))

            try:
                checked = Checker(field_type, calc_info, grouped_fields,
                                  auto_group=explicit_group is None).check(parsed[cid])
            except ExprError as exc:
                issues.error(path, f"{cid}: {exc.message}", code="expression")
                continue
            resolved[cid] = ResolvedColumn(i, col, checked.data_type, checked.aggregate,
                                           ast=parsed[cid], checked=checked)

        if issues.has_errors:
            return None, issues.items

        columns = [resolved[c.id] for c in spec.columns]
        aggregated = any(c.aggregate for c in columns)

        # -- step 6: semantics ------------------------------------------------------------
        if explicit_group is None:
            group_ids = [c.id for c in columns if not c.aggregate] if aggregated else []
        else:
            group_ids = []
            for k, gid in enumerate(explicit_group):
                rc = resolved.get(gid)
                if rc is None:
                    issues.error(f"group_by[{k}]", f"Unknown column id '{gid}'")
                elif rc.aggregate:
                    issues.error(f"group_by[{k}]", f"Cannot group by aggregated column '{gid}'")
                else:
                    group_ids.append(gid)
            if aggregated:
                for c in columns:
                    if not c.aggregate and c.id not in group_ids:
                        issues.error(f"columns[{c.index}]",
                                     f"'{c.id}' must be grouped or aggregated")

        self._check_fanout(spec, columns, joins, issues)

        filters = [f for f in (self._resolve_filter(f"filters[{k}]", flt, resolved,
                                                    resolve_field, issues, where=True,
                                                    period_ctx=period_ctx)
                               for k, flt in enumerate(spec.filters)) if f]
        having: list[ResolvedFilter] = []
        for k, flt in enumerate(spec.having):
            if flt.ref not in resolved:
                issues.error(f"having[{k}].ref",
                             f"having must reference a column id, e.g. [revenue]; got {flt.ref}")
                continue
            if not aggregated:
                issues.error(f"having[{k}]", "having needs at least one aggregated column")
                continue
            hf = self._resolve_filter(f"having[{k}]", flt, resolved, resolve_field, issues,
                                      where=False, period_ctx=period_ctx)
            if hf:
                having.append(hf)

        order_by: list[ResolvedOrder] = []
        group_field_refs = {resolved[g].spec.ref for g in group_ids  # type: ignore[union-attr]
                            if isinstance(resolved[g].spec, FieldColumn)}
        for k, ob in enumerate(spec.order_by):
            if ob.ref in resolved:
                order_by.append(ResolvedOrder(ob.dir, column_id=ob.ref))
                continue
            if "." not in ob.ref:
                issues.error(f"order_by[{k}].ref", f"Unknown column id '{ob.ref}'")
                continue
            r = resolve_field(f"order_by[{k}].ref", ob.ref)
            if r is None:
                continue
            if aggregated and ob.ref not in group_field_refs:
                issues.error(f"order_by[{k}].ref",
                             f"{ob.ref} is not grouped; sort by an output column instead")
                continue
            order_by.append(ResolvedOrder(ob.dir, field=(r[0], r[1])))

        if issues.has_errors:
            return None, issues.items

        output = [self._output(c, aliases) for c in columns]
        eval_order = [c.id for c in columns if not isinstance(c.spec, ExpressionColumn)] + order
        plan = Plan(spec, snap, aliases, joins, columns, eval_order, aggregated, group_ids,
                    filters, having, order_by, output,
                    warnings=[i for i in issues.items if i.severity == "warning"],
                    gap_fill=self._gap_fill(columns, group_ids, filters),
                    group_fields=self._extra_group_fields(columns, group_ids, aliases))
        return plan, issues.items

    # -- helpers --------------------------------------------------------------------------

    @staticmethod
    def _extra_group_fields(columns: list[ResolvedColumn], group_ids: list[str],
                            aliases: dict[str, TableSchema]) -> list[tuple[str, str]]:
        """Fields to add to GROUP BY for expressions like ``s.total - SUM(p.amount)``,
        skipping ones already grouped through a plain field column."""
        grouped = {c.spec.ref for c in columns
                   if c.id in group_ids and isinstance(c.spec, FieldColumn)}
        refs = sorted({ref for c in columns if c.checked is not None
                       for ref in c.checked.group_fields} - grouped)
        out: list[tuple[str, str]] = []
        for ref in refs:
            alias, column = _split_ref(ref)
            col = aliases[alias].column(column)
            assert col is not None
            out.append((alias, col.name))
        return out

    @staticmethod
    def _field_problem(aliases: dict[str, TableSchema], access: AccessContext | None,
                       alias: str, column: str) -> str:
        table = aliases.get(alias)
        if table is None:
            return f"unknown alias '{alias}'"
        if table.column(column) is None:
            return f"column {table.name}.{column} does not exist"
        return f"{alias}.{column} is not visible to your role"

    @staticmethod
    def _check_date_diff(path: str, start: str, end: str, resolve_field: Any,
                         issues: _Issues) -> bool:
        ok = True
        for part, ref in (("start", start), ("end", end)):
            r = resolve_field(f"{path}.{part}", ref)
            if r is None:
                ok = False
            elif not r[2].is_temporal:
                issues.error(f"{path}.{part}",
                             f"Date difference needs a date; {ref} is {TYPE_WORD[r[2]]}")
                ok = False
        return ok

    def _check_aggregate(self, path: str, col: AggregateColumn, resolve_field: Any,
                         issues: _Issues) -> ResolvedColumn | None:
        idx = int(path.split("[")[1].rstrip("]"))
        if col.arg is None:
            if col.fn != "count":
                issues.error(f"{path}.arg", f"{col.fn} needs a column")
                return None
            return ResolvedColumn(idx, col, DataType.INTEGER, aggregate=True)
        if isinstance(col.arg, DateDiffExpr):
            if not self._check_date_diff(f"{path}.arg", col.arg.start, col.arg.end,
                                         resolve_field, issues):
                return None
            arg_type, arg_desc = DataType.INTEGER, "the date difference"
        else:
            r = resolve_field(f"{path}.arg", col.arg)
            if r is None:
                return None
            arg_type, arg_desc = r[2], col.arg
        label = {"sum": "Sum", "avg": "Avg", "min": "Min", "max": "Max"}.get(col.fn, "Count")
        if col.fn in ("sum", "avg") and not arg_type.is_numeric:
            issues.error(f"{path}.fn",
                         f"{label} needs a number; {arg_desc} is {TYPE_WORD[arg_type]}")
            return None
        if col.fn in ("min", "max") and not arg_type.is_orderable:
            issues.error(f"{path}.fn", f"{label} needs a number, date or text; "
                                       f"{arg_desc} is {TYPE_WORD[arg_type]}")
            return None
        if col.fn in ("count", "count_distinct"):
            result = DataType.INTEGER
        elif col.fn == "sum":
            result = _numeric_result(arg_type)
        elif col.fn == "avg":
            result = DataType.FLOAT if arg_type == DataType.FLOAT else DataType.DECIMAL
        else:
            result = arg_type
        return ResolvedColumn(idx, col, result, aggregate=True, arg_type=arg_type)

    @staticmethod
    def _topo_order(spec: QuerySpec, parsed: dict[str, Node], ids: dict[str, int],
                    issues: _Issues) -> list[str]:
        from reporting.query._internal.expr.ast import CalcRef

        deps: dict[str, set[str]] = {}
        for cid, node in parsed.items():
            refs = {n.name for n in walk(node) if isinstance(n, CalcRef)}
            missing = sorted(r for r in refs if r not in ids)
            if missing:
                issues.error(f"columns[{ids[cid]}].expr",
                             f"{cid}: unknown column [{missing[0]}]", code="expression")
                continue
            if cid in refs:
                issues.error(f"columns[{ids[cid]}].expr", f"{cid}: refers to itself",
                             code="expression")
                continue
            deps[cid] = {r for r in refs if r in parsed}
        order: list[str] = []
        remaining = dict(deps)
        while remaining:
            ready = sorted(c for c, d in remaining.items() if not (d - set(order)))
            if not ready:
                for c in sorted(remaining):
                    issues.error(f"columns[{ids[c]}].expr",
                                 f"{c}: circular reference between calculated columns",
                                 code="expression")
                break
            order.extend(ready)
            for c in ready:
                del remaining[c]
        return order

    @staticmethod
    def _check_fanout(spec: QuerySpec, columns: list[ResolvedColumn],
                      joins: list[ResolvedJoin], issues: _Issues) -> None:
        if not joins:
            return
        parent = {j.alias: j for j in joins}

        def path_to(alias: str) -> set[str]:
            out: set[str] = set()
            while alias in parent:
                out.add(alias)
                alias = parent[alias].from_alias
            return out

        def culprit(alias: str) -> ResolvedJoin | None:
            on_path = path_to(alias)
            for j in joins:
                if j.alias in on_path and j.cardinality == "many_to_one":
                    return j  # measure lives on the "one" side of this join
                if j.alias not in on_path and j.cardinality == "one_to_many":
                    return j
            return None

        for c in columns:
            measured: list[str] = []  # aliases whose rows are aggregated by sum/avg/count
            if isinstance(c.spec, AggregateColumn) and c.spec.fn in FANOUT_FNS \
                    and c.spec.arg is not None:
                arg = c.spec.arg
                refs = [arg] if isinstance(arg, str) else [arg.start, arg.end]
                measured = [_split_ref(r)[0] for r in refs]
            elif c.ast is not None:
                for n in walk(c.ast):
                    if isinstance(n, FuncCall) and n.name in ("SUM", "AVG", "COUNT"):
                        from reporting.query._internal.expr.ast import FieldRef

                        measured += [f.alias for a in n.args for f in walk(a)
                                     if isinstance(f, FieldRef)]
            for alias in dict.fromkeys(measured):
                j = culprit(alias)
                if j is not None:
                    label = c.spec.label or c.id
                    issues.warn(
                        f"columns[{c.index}]",
                        f"{label} would be multiplied by joining {j.table.name}; "
                        f"aggregate before joining, or use Count distinct / Min / Max",
                        code="fan_out",
                    )
                    break

    @staticmethod
    def _gap_fill(columns: list[ResolvedColumn], group_ids: list[str],
                  filters: list[ResolvedFilter]) -> GapFill | None:
        """Empty buckets can be filled when the only dimension is a date bucket and a preset
        period bounds that same date."""
        if len(group_ids) != 1:
            return None
        dim = next(c for c in columns if c.id == group_ids[0])
        spec = dim.spec
        if not isinstance(spec, FieldColumn) or spec.grain is None:
            return None
        ref = _split_ref(spec.ref)
        period = next((f.period for f in filters if f.period and
                       (f.field == ref or f.column_id == dim.id)), None)
        if period is None:
            return None
        zero = tuple(c.id for c in columns if isinstance(c.spec, AggregateColumn)
                     and c.spec.fn in ("sum", "count", "count_distinct"))
        return GapFill(column=dim.id, grain=spec.grain, start=period[0], end=period[1],
                       zero_columns=zero)

    def _resolve_filter(self, path: str, flt: Filter, resolved: dict[str, ResolvedColumn],
                        resolve_field: Any, issues: _Issues, where: bool,
                        period_ctx: PeriodContext) -> ResolvedFilter | None:
        target_field: tuple[str, str] | None = None
        column_id: str | None = None
        if flt.ref in resolved:
            rc = resolved[flt.ref]
            if where and rc.aggregate:
                issues.error(f"{path}.ref",
                             f"'{flt.ref}' is aggregated; filter it under having instead")
                return None
            dtype, column_id = rc.data_type, flt.ref
        elif "." in flt.ref:
            r = resolve_field(f"{path}.ref", flt.ref)
            if r is None:
                return None
            target_field, dtype = (r[0], r[1]), r[2]
        else:
            issues.error(f"{path}.ref", f"Unknown column '{flt.ref}'")
            return None

        op, value = flt.op, flt.value
        period: tuple[date, date] | None = None
        try:
            if op in ("is_null", "is_not_null"):
                value = None
            elif op == "in_period":
                if not dtype.is_temporal:
                    raise ValueError(f"a date range needs a date column, not {TYPE_WORD[dtype]}")
                if value not in PERIODS:
                    raise ValueError("choose one of " + ", ".join(LABELS.values()))
                period = resolve_period(value, period_ctx.today)
                value = [_period_bound(d, dtype, period_ctx.tz) for d in period]
            elif op in ("in", "not_in"):
                if not isinstance(value, list) or not value:
                    raise ValueError("expected a non-empty list of values")
                if len(value) > MAX_IN_VALUES:
                    raise ValueError(f"at most {MAX_IN_VALUES} values")
                value = [coerce_value(dtype, v) for v in value]
            elif op == "between":
                if not isinstance(value, list) or len(value) != 2:
                    raise ValueError("expected [from, to]")
                if not dtype.is_orderable:
                    raise ValueError(f"cannot range-filter {TYPE_WORD[dtype]}")
                value = [coerce_value(dtype, v) for v in value]
            elif op == "like":
                if dtype != DataType.TEXT:
                    raise ValueError("'like' needs a text column")
                value = coerce_value(DataType.TEXT, value)
            else:
                if op in ("gt", "gte", "lt", "lte") and not dtype.is_orderable:
                    raise ValueError(f"cannot compare {TYPE_WORD[dtype]} with '{op}'")
                value = coerce_value(dtype, value)
        except ValueError as exc:
            issues.error(f"{path}.value", f"{flt.ref}: {exc}")
            return None
        return ResolvedFilter(path, op, value, dtype, field=target_field, column_id=column_id,
                              period=period)

    @staticmethod
    def _output(c: ResolvedColumn, aliases: dict[str, TableSchema]) -> OutputColumn:
        spec = c.spec

        def src(ref: str) -> str:
            alias, column = _split_ref(ref)
            return f"{aliases[alias].name}.{column}"

        if isinstance(spec, FieldColumn):
            source, label = src(spec.ref), spec.label or _split_ref(spec.ref)[1]
        elif isinstance(spec, AggregateColumn):
            if spec.arg is None:
                source = "count(*)"
            elif isinstance(spec.arg, str):
                source = f"{spec.fn}({src(spec.arg)})"
            else:
                source = f"{spec.fn}(days {src(spec.arg.start)} → {src(spec.arg.end)})"
            label = spec.label or spec.id
        elif isinstance(spec, DateDiffColumn):
            source = f"days {src(spec.start)} → {src(spec.end)}"
            label = spec.label or spec.id
        else:
            source, label = spec.expr, spec.label or spec.id
        grain = spec.grain if isinstance(spec, FieldColumn) else None
        if grain:
            source = f"{source} by {grain}"
        return OutputColumn(id=c.id, label=label, data_type=c.data_type,
                            role="measure" if c.aggregate else "dimension", source=source,
                            grain=grain)
