"""Compile a validated ``Plan`` into dialect-specific, parameterised SQL (plan §3.3)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Numeric,
    String,
    and_,
    cast,
    column,
    not_,
    select,
    table,
)
from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite
from sqlalchemy.engine.interfaces import BindTyping
from sqlalchemy.engine.interfaces import Dialect as SADialect
from sqlalchemy.sql.elements import ColumnElement, quoted_name
from sqlalchemy.sql.selectable import FromClause

from reporting.core import DataType, Dialect
from reporting.metadata import TableSchema
from reporting.query._internal.constructs import DateDiff, DateTrunc
from reporting.query._internal.expr.compiler import Binder, ExprCompiler, aggregate
from reporting.query._internal.validator import Plan, ResolvedFilter
from reporting.query.api import (
    AggregateColumn,
    CompiledQuery,
    DateDiffColumn,
    ExpressionColumn,
    FieldColumn,
)

Expr = ColumnElement[Any]


def sa_dialect(d: Dialect) -> SADialect:
    """SQLAlchemy dialect configured for ``:name`` placeholders (``CompiledStatement`` form)."""
    module = {Dialect.POSTGRESQL: postgresql, Dialect.MSSQL: mssql, Dialect.MYSQL: mysql,
              Dialect.SQLITE: sqlite}[d]
    dialect = module.dialect(paramstyle="named")
    # No "::TYPE" casts after placeholders: they would break ":name" parsing downstream.
    dialect.bind_typing = BindTyping.NONE
    return dialect  # type: ignore[no-any-return]


def column_type(dtype: DataType) -> Any:
    return {
        DataType.INTEGER: Integer(), DataType.DECIMAL: Numeric(), DataType.FLOAT: Float(),
        DataType.TEXT: String(), DataType.BOOLEAN: Boolean(), DataType.DATE: Date(),
        DataType.DATETIME: DateTime(), DataType.DATETIME_TZ: DateTime(timezone=True),
    }.get(dtype, String())


def driver_value(dialect: Dialect, value: Any) -> Any:
    """Make a bound value acceptable to the target driver (SQLite lacks Decimal/datetime)."""
    if dialect is not Dialect.SQLITE:
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds" if not value.microsecond
                               else "microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [driver_value(dialect, v) for v in value]
    return value


class _DialectBinder(Binder):
    def __init__(self, dialect: Dialect) -> None:
        super().__init__()
        self._dialect = dialect

    def bind(self, value: Any, type_: Any = None, expanding: bool = False) -> Expr:
        return super().bind(driver_value(self._dialect, value), type_, expanding)


def _table(t: TableSchema, alias: str, dialect: Dialect) -> FromClause:
    schema = t.schema_name if dialect in (Dialect.POSTGRESQL, Dialect.MSSQL) else None
    cols = [column(quoted_name(c.name, True), column_type(c.data_type)) for c in t.columns]
    return table(quoted_name(t.name, True), *cols,
                 schema=quoted_name(schema, True) if schema else None).alias(alias)


def compile_plan(plan: Plan, dialect: Dialect) -> CompiledQuery:
    binder = _DialectBinder(dialect)
    froms = {alias: _table(t, alias, dialect) for alias, t in plan.aliases.items()}

    def field(alias: str, col: str) -> Expr:
        return froms[alias].c[col]

    def ref(text: str) -> Expr:
        alias, _, col = text.partition(".")
        return field(alias, col)

    # FROM + joins, with ON clauses built from the snapshot's column pairs only.
    source = froms[plan.spec.base.alias]
    for j in plan.joins:
        left, right = froms[j.from_alias], froms[j.alias]
        on = and_(*[left.c[a] == right.c[b] for a, b in j.pairs])
        source = source.join(right, on, isouter=j.type == "left")

    exprs: dict[str, Expr] = {}
    for cid in plan.eval_order:
        rc = plan.column(cid)
        spec = rc.spec
        if isinstance(spec, FieldColumn):
            exprs[cid] = DateTrunc(spec.grain, ref(spec.ref)) if spec.grain else ref(spec.ref)
        elif isinstance(spec, DateDiffColumn):
            exprs[cid] = DateDiff(ref(spec.start), ref(spec.end))
        elif isinstance(spec, AggregateColumn):
            arg: Expr | None
            if spec.arg is None:
                arg = None
            elif isinstance(spec.arg, str):
                arg = ref(spec.arg)
            else:
                arg = DateDiff(ref(spec.arg.start), ref(spec.arg.end))
            exprs[cid] = aggregate(spec.fn, arg, rc.arg_type, dialect)
        else:
            assert isinstance(spec, ExpressionColumn) and rc.ast is not None and rc.checked
            exprs[cid] = ExprCompiler(rc.checked, dialect, binder, field,
                                      lambda name: exprs[name]).compile(rc.ast)

    labels = {c.id: exprs[c.id].label(c.id) for c in plan.columns}
    stmt = select(*[labels[c.id] for c in plan.columns]).select_from(source)

    def condition(f: ResolvedFilter) -> Expr:
        target = field(*f.field) if f.field else exprs[f.column_id]  # type: ignore[index]
        t = column_type(f.data_type)
        op, v = f.op, f.value
        if op == "is_null":
            return target.is_(None)
        if op == "is_not_null":
            return target.is_not(None)
        if op in ("in", "not_in"):
            clause = target.in_(binder.bind(v, t, expanding=True))
            return not_(clause) if op == "not_in" else clause
        if op == "in_period":  # half-open, so late times on the last day are included
            return and_(target >= binder.bind(v[0], t), target < binder.bind(v[1], t))
        if op == "between":
            return target.between(binder.bind(v[0], t), binder.bind(v[1], t))
        if op == "like":
            if dialect is Dialect.POSTGRESQL:  # enum columns have no LIKE operator
                target = cast(target, String())
            return target.like(binder.bind(v, String()))
        b = binder.bind(v, t)
        return {"eq": target == b, "ne": target != b, "gt": target > b, "gte": target >= b,
                "lt": target < b, "lte": target <= b}[op]

    if plan.filters:
        stmt = stmt.where(and_(*[condition(f) for f in plan.filters]))
    if plan.group_ids or plan.group_fields:
        stmt = stmt.group_by(*[exprs[g] for g in plan.group_ids],
                             *[field(a, c) for a, c in plan.group_fields])
    if plan.having:
        stmt = stmt.having(and_(*[condition(f) for f in plan.having]))
    for ob in plan.order_by:
        target = labels[ob.column_id] if ob.column_id else field(*ob.field)  # type: ignore[misc]
        stmt = stmt.order_by(target.desc() if ob.dir == "desc" else target.asc())
    if plan.spec.limit is not None:
        if dialect is Dialect.MSSQL:
            # A validated int (1..100000), rendered as TOP n; never user text.
            stmt = stmt.limit(int(plan.spec.limit))
        else:
            stmt = stmt.limit(binder.bind(plan.spec.limit, Integer()))

    compiled = stmt.compile(dialect=sa_dialect(dialect),
                            compile_kwargs={"render_postcompile": True})
    params = dict(compiled.params)
    return CompiledQuery(
        statement_sql=str(compiled), params=params, database=plan.snapshot.database,
        dialect=dialect.value, snapshot_version=plan.snapshot.version,
        output_columns=tuple(plan.output), warnings=tuple(plan.warnings),
        gap_fill=plan.gap_fill,
    )
