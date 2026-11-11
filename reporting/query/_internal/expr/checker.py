"""Reference resolution, bottom-up type checking and aggregation-level checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal as Lit

from reporting.core import DataType
from reporting.query._internal.expr.ast import (
    BinaryOp,
    CalcRef,
    Case,
    FieldRef,
    FuncCall,
    Literal,
    Node,
    UnaryOp,
)
from reporting.query._internal.expr.parser import ExprError

Level = Lit["constant", "row", "aggregate"]
NULL = "null"  # pseudo-type of the NULL literal: compatible with everything

AGGREGATES = {"SUM", "AVG", "COUNT", "COUNT_DISTINCT", "MIN", "MAX"}
FUNCTIONS = AGGREGATES | {
    "ABS", "ROUND", "FLOOR", "CEILING", "COALESCE", "NULLIF",
    "UPPER", "LOWER", "TRIM", "LENGTH", "CONCAT", "YEAR", "MONTH", "DAY", "DATE_DIFF_DAYS",
}


@dataclass(frozen=True)
class CalcInfo:
    """What an expression needs to know about a column it references with ``[id]``."""

    data_type: DataType
    aggregate: bool
    grouped: bool = False


@dataclass
class Checked:
    data_type: DataType | str  # DataType or NULL
    level: Level
    # Field refs and row-level calc refs that appear outside any aggregate function.
    bare_fields: set[str] = field(default_factory=set)
    bare_calcs: set[str] = field(default_factory=set)


@dataclass
class CheckResult:
    data_type: DataType
    aggregate: bool
    fields: set[str]
    calc_refs: set[str]
    # Literals whose type was coerced (e.g. a date string compared with a date column).
    coerced: dict[int, DataType]
    # Inferred type of every node, keyed by ``id(node)``; used by the compiler.
    types: dict[int, DataType | str] = field(default_factory=dict)
    # Bare field refs beside an aggregate that automatic grouping must add to GROUP BY.
    group_fields: set[str] = field(default_factory=set)


FieldResolver = Callable[[str, str], DataType]
CalcResolver = Callable[[str], CalcInfo]


def _numeric_result(*types: DataType | str) -> DataType:
    real = [t for t in types if t != NULL]
    if any(t == DataType.FLOAT for t in real):
        return DataType.FLOAT
    if any(t == DataType.DECIMAL for t in real):
        return DataType.DECIMAL
    return DataType.INTEGER


def _family(t: DataType | str) -> str:
    if t == NULL:
        return NULL
    assert isinstance(t, DataType)
    if t.is_numeric:
        return "number"
    if t.is_temporal:
        return "date"
    return t.value


def _name(t: DataType | str) -> str:
    if t == NULL:
        return "null"
    assert isinstance(t, DataType)
    return {"integer": "a number", "decimal": "a number", "float": "a number",
            "text": "text", "boolean": "true/false", "date": "a date", "datetime": "a date",
            "datetime_tz": "a date"}.get(t.value, t.value)


def _combine_levels(*levels: Level) -> Level:
    if "aggregate" in levels:
        return "aggregate"
    if "row" in levels:
        return "row"
    return "constant"


def _is_iso_date(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False


class Checker:
    def __init__(self, resolve_field: FieldResolver, resolve_calc: CalcResolver,
                 grouped_fields: set[str], auto_group: bool = False) -> None:
        self._field = resolve_field
        self._calc = resolve_calc
        self._grouped = grouped_fields
        self._auto_group = auto_group
        self.fields: set[str] = set()
        self.calcs: set[str] = set()
        self.coerced: dict[int, DataType] = {}
        self.types: dict[int, DataType | str] = {}

    def check(self, node: Node) -> CheckResult:
        result = self._visit(node, inside_agg=False)
        if result.data_type == NULL:
            raise ExprError("Expression is always NULL")
        group_fields: set[str] = set()
        if result.level == "aggregate":
            for ref in sorted(result.bare_fields):
                if ref in self._grouped:
                    continue
                if self._auto_group:  # e.g. s.total - SUM(p.amount): also group by s.total
                    group_fields.add(ref)
                else:
                    raise ExprError(
                        f"{ref} must be inside an aggregate (e.g. SUM({ref})) or be a grouped "
                        f"column, because the expression also aggregates")
            for name in sorted(result.bare_calcs):
                if not self._calc(name).grouped:
                    raise ExprError(f"[{name}] must be grouped or wrapped in an aggregate")
        assert isinstance(result.data_type, DataType)
        return CheckResult(result.data_type, result.level == "aggregate", self.fields,
                           self.calcs, self.coerced, self.types, group_fields)

    # -- visitors -------------------------------------------------------------------------

    def _visit(self, node: Node, inside_agg: bool) -> Checked:
        checked = self._visit_inner(node, inside_agg)
        self.types[id(node)] = checked.data_type
        return checked

    def _visit_inner(self, node: Node, inside_agg: bool) -> Checked:
        if isinstance(node, Literal):
            return Checked(self._literal_type(node), "constant")
        if isinstance(node, FieldRef):
            dtype = self._field(node.alias, node.column)
            self.fields.add(node.ref)
            return Checked(dtype, "row", bare_fields=set() if inside_agg else {node.ref})
        if isinstance(node, CalcRef):
            info = self._calc(node.name)
            self.calcs.add(node.name)
            if info.aggregate:
                if inside_agg:
                    raise ExprError(f"[{node.name}] is already aggregated; aggregates cannot "
                                    f"be nested")
                return Checked(info.data_type, "aggregate")
            return Checked(info.data_type, "row",
                           bare_calcs=set() if inside_agg else {node.name})
        if isinstance(node, UnaryOp):
            inner = self._visit(node.operand, inside_agg)
            if node.op == "NEG":
                self._require(inner.data_type, "number", "Negation")
                return Checked(inner.data_type, inner.level, inner.bare_fields, inner.bare_calcs)
            self._require(inner.data_type, "boolean", "NOT")
            return Checked(DataType.BOOLEAN, inner.level, inner.bare_fields, inner.bare_calcs)
        if isinstance(node, BinaryOp):
            return self._binary(node, inside_agg)
        if isinstance(node, Case):
            return self._case(node, inside_agg)
        if isinstance(node, FuncCall):
            return self._func(node, inside_agg)
        raise ExprError("Unsupported expression")  # pragma: no cover

    @staticmethod
    def _literal_type(node: Literal) -> DataType | str:
        v = node.value
        if v is None:
            return NULL
        if isinstance(v, bool):
            return DataType.BOOLEAN
        if isinstance(v, int):
            return DataType.INTEGER
        if isinstance(v, Decimal):
            return DataType.DECIMAL
        return DataType.TEXT

    @staticmethod
    def _require(t: DataType | str, family: str, what: str) -> None:
        if t == NULL:
            return
        if _family(t) != family:
            wanted = {"number": "a number", "boolean": "true/false", "text": "text",
                      "date": "a date"}[family]
            raise ExprError(f"{what} needs {wanted}, got {_name(t)}")

    def _merge(self, *parts: Checked) -> tuple[Level, set[str], set[str]]:
        return (
            _combine_levels(*(p.level for p in parts)),
            set().union(*(p.bare_fields for p in parts)),
            set().union(*(p.bare_calcs for p in parts)),
        )

    def _binary(self, node: BinaryOp, inside_agg: bool) -> Checked:
        left = self._visit(node.left, inside_agg)
        right = self._visit(node.right, inside_agg)
        level, bf, bc = self._merge(left, right)
        op = node.op
        if op in ("AND", "OR"):
            self._require(left.data_type, "boolean", op)
            self._require(right.data_type, "boolean", op)
            return Checked(DataType.BOOLEAN, level, bf, bc)
        if op in ("+", "-", "*", "/"):
            self._require(left.data_type, "number", f"'{op}'")
            self._require(right.data_type, "number", f"'{op}'")
            dtype = _numeric_result(left.data_type, right.data_type)
            if op == "/" and dtype == DataType.INTEGER:
                dtype = DataType.DECIMAL  # true division, never integer division
            return Checked(dtype, level, bf, bc)
        # comparison
        lf, rf = _family(left.data_type), _family(right.data_type)
        if NULL in (lf, rf):
            raise ExprError("Compare with NULL using COALESCE or a CASE; '= NULL' is never true")
        if lf != rf:
            # Allow a date column compared with an ISO date string literal.
            for side, other, fam in ((node.right, lf, rf), (node.left, rf, lf)):
                if other == "date" and fam == "text" and isinstance(side, Literal) \
                        and isinstance(side.value, str) and _is_iso_date(side.value):
                    self.coerced[id(side)] = DataType.DATETIME
                    return Checked(DataType.BOOLEAN, level, bf, bc)
            raise ExprError(f"Cannot compare {_name(left.data_type)} with "
                            f"{_name(right.data_type)}")
        if op not in ("=", "<>") and lf in ("boolean", "json"):
            raise ExprError(f"'{op}' cannot order {_name(left.data_type)} values")
        return Checked(DataType.BOOLEAN, level, bf, bc)

    def _case(self, node: Case, inside_agg: bool) -> Checked:
        parts: list[Checked] = []
        results: list[DataType | str] = []
        for cond, res in node.whens:
            c = self._visit(cond, inside_agg)
            self._require(c.data_type, "boolean", "CASE WHEN condition")
            r = self._visit(res, inside_agg)
            parts += [c, r]
            results.append(r.data_type)
        if node.else_ is not None:
            e = self._visit(node.else_, inside_agg)
            parts.append(e)
            results.append(e.data_type)
        level, bf, bc = self._merge(*parts)
        branches = [r for _, r in node.whens] + ([node.else_] if node.else_ is not None else [])
        return Checked(self._common(results, "CASE branches", branches), level, bf, bc)

    def _common(self, types: list[DataType | str], what: str,
                nodes: tuple[Node, ...] | list[Node] = ()) -> DataType | str:
        real = [t for t in types if t != NULL]
        if not real:
            return NULL
        families = {_family(t) for t in real}
        if families == {"date", "text"} and nodes:
            # ISO date string literals alongside date values are coerced to dates.
            texts = [n for n, t in zip(nodes, types, strict=True) if _family(t) == "text"]
            if all(isinstance(n, Literal) and isinstance(n.value, str)
                   and _is_iso_date(n.value) for n in texts):
                for n in texts:
                    self.coerced[id(n)] = DataType.DATETIME
                real = [t for t in real if _family(t) == "date"]
                families = {"date"}
        if len(families) > 1:
            raise ExprError(f"{what} must share a type, got "
                            + " and ".join(sorted({_name(t) for t in real})))
        family = families.pop()
        if family == "number":
            return _numeric_result(*real)
        if family == "date":
            return DataType.DATETIME if len(set(real)) > 1 else real[0]
        return real[0]

    def _func(self, node: FuncCall, inside_agg: bool) -> Checked:
        name, args = node.name, node.args
        if name not in FUNCTIONS:
            raise ExprError(f"unknown function '{name}'")

        def arity(lo: int, hi: int | None = None) -> None:
            hi_ = lo if hi is None else hi
            if not lo <= len(args) <= (hi_ if hi_ >= 0 else 10**6):
                expected = str(lo) if lo == hi_ else (f"{lo}+" if hi_ < 0 else f"{lo}–{hi_}")
                raise ExprError(f"{name} takes {expected} argument(s), got {len(args)}")

        if name in AGGREGATES:
            if inside_agg:
                raise ExprError(f"{name} cannot be nested inside another aggregate")
            arity(1)
            inner = self._visit(args[0], inside_agg=True)
            t = inner.data_type
            if name in ("SUM", "AVG"):
                self._require(t, "number", name)
                dtype: DataType | str = (
                    _numeric_result(t) if name == "SUM"
                    else (DataType.FLOAT if t == DataType.FLOAT else DataType.DECIMAL))
            elif name in ("MIN", "MAX"):
                if t != NULL and not (isinstance(t, DataType) and t.is_orderable):
                    raise ExprError(f"{name} needs a number, date or text, got {_name(t)}")
                dtype = t
            else:
                dtype = DataType.INTEGER
            return Checked(dtype, "aggregate")

        checked = [self._visit(a, inside_agg) for a in args]
        types = [c.data_type for c in checked]
        level, bf, bc = self._merge(*checked) if checked else ("constant", set(), set())

        if name in ("ABS", "FLOOR", "CEILING"):
            arity(1)
            self._require(types[0], "number", name)
            return Checked(_numeric_result(types[0]), level, bf, bc)
        if name == "ROUND":
            arity(1, 2)
            self._require(types[0], "number", name)
            if len(args) == 2 and not (isinstance(args[1], Literal)
                                       and isinstance(args[1].value, int)
                                       and not isinstance(args[1].value, bool)):
                raise ExprError("ROUND digits must be a whole number, e.g. ROUND(x, 2)")
            base = _numeric_result(types[0])
            return Checked(DataType.DECIMAL if base == DataType.INTEGER else base, level, bf, bc)
        if name == "COALESCE":
            arity(2, -1)
            return Checked(self._common(types, "COALESCE arguments", args), level, bf, bc)
        if name == "NULLIF":
            arity(2)
            self._common(types, "NULLIF arguments", args)
            return Checked(types[0], level, bf, bc)
        if name in ("UPPER", "LOWER", "TRIM"):
            arity(1)
            self._require(types[0], "text", name)
            return Checked(DataType.TEXT, level, bf, bc)
        if name == "LENGTH":
            arity(1)
            self._require(types[0], "text", name)
            return Checked(DataType.INTEGER, level, bf, bc)
        if name == "CONCAT":
            arity(2, -1)
            return Checked(DataType.TEXT, level, bf, bc)
        if name in ("YEAR", "MONTH", "DAY"):
            arity(1)
            self._require(types[0], "date", name)
            return Checked(DataType.INTEGER, level, bf, bc)
        # DATE_DIFF_DAYS
        arity(2)
        self._require(types[0], "date", "DATE_DIFF_DAYS start")
        self._require(types[1], "date", "DATE_DIFF_DAYS end")
        return Checked(DataType.INTEGER, level, bf, bc)
