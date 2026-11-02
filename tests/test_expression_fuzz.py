"""Property-based fuzzing (plan §4.3): every input either fails validation cleanly or
compiles to SQL containing only snapshot identifiers, allow-listed function names and bind
placeholders.

Set ``REPORTING_FUZZ_EXAMPLES=10000`` for the milestone-sized run.
"""

import os
import re

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from reporting.core import Dialect
from reporting.query._internal.expr.checker import FUNCTIONS
from reporting.query._internal.expr.parser import ExprError
from tests.expr_helpers import CALCS, FIELDS, IDENTIFIERS, compile_sql

EXAMPLES = int(os.environ.get("REPORTING_FUZZ_EXAMPLES", "1500"))
FUZZ = settings(max_examples=EXAMPLES, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])

# Words SQLAlchemy itself may emit around allow-listed constructs.
SQL_VOCAB = {
    "CAST", "AS", "NUMERIC", "FLOAT", "INTEGER", "VARCHAR", "DATE", "CASE", "WHEN", "THEN",
    "ELSE", "END", "AND", "OR", "NOT", "NULL", "IS", "DISTINCT", "EXTRACT", "FROM", "TRUE",
    "FALSE", "YEAR", "MONTH", "DAY", "DATEDIFF", "DATEPART", "CHAR_LENGTH", "LEN", "LENGTH",
    "JULIANDAY", "STRFTIME", "FLOOR", "CEILING", "COUNT", "CONCAT", "BIGINT", "REAL", "CHAR",
    "NVARCHAR", "TEXT", "DECIMAL", "DOUBLE", "SIGNED",
} | FUNCTIONS
# String literals SQLAlchemy itself emits (SQLite date-part formats); never user text.
COMPILER_LITERALS = ("'%Y'", "'%m'", "'%d'")
ALLOWED_WORDS = {w.upper() for w in SQL_VOCAB} | {f"CALC_{n}".upper() for n in CALCS} | \
    {a.upper() for a in FIELDS}


def assert_safe_sql(sql: str) -> None:
    for lit in COMPILER_LITERALS:
        sql = sql.replace(lit, " ")
    assert "'" not in sql, f"string literal inlined: {sql}"
    assert ";" not in sql and "--" not in sql and "/*" not in sql, sql
    rest = sql
    for quoted in re.findall(r'"((?:[^"]|"")*)"|\[([^\]]*)\]|`([^`]*)`', sql):
        name = next(q for q in quoted if q)
        assert name.replace('""', '"') in IDENTIFIERS, f"unknown identifier {name!r} in {sql}"
    rest = re.sub(r'"(?:[^"]|"")*"|\[[^\]]*\]|`[^`]*`', " ", rest)
    rest = re.sub(r":p\d+(?:_\d+)?", " ", rest)  # bind placeholders
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", rest):
        assert word.upper() in ALLOWED_WORDS, f"unexpected word {word!r} in {sql}"


def try_compile(text: str, dialect: Dialect) -> None:
    try:
        sql, params, _ = compile_sql(text, dialect)
    except ExprError:
        return  # clean rejection
    assert_safe_sql(sql)
    for value in params.values():
        assert not isinstance(value, (list, dict))


# -- 1. arbitrary strings -----------------------------------------------------------------

ALPHABET = st.sampled_from(list(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.,()[]'\"+-*/<>=;:!@#$%^&|\\`\n"
))
TOKENS = st.sampled_from(
    ["o.id", "o.total_amount", "c.country", "o.status", "[revenue]", "[orders]", "SUM(", "AVG(",
     "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "NULL", "'x'", "'a''b'", "1",
     "2.5", "(", ")", ",", "+", "-", "*", "/", "=", "<>", ";", "--", "DROP", "SELECT", "EXEC(",
     "\"col\"", "o.\"x\"\"y\"", "o.ordered_at", "DATE_DIFF_DAYS(", "COALESCE(", "/*", "UNION"]
)


@FUZZ
@given(st.text(alphabet=ALPHABET, max_size=120), st.sampled_from(list(Dialect)))
def test_random_strings(text, dialect):
    try_compile(text, dialect)


@FUZZ
@given(st.lists(TOKENS, max_size=25).map(" ".join), st.sampled_from(list(Dialect)))
def test_random_token_soup(text, dialect):
    try_compile(text, dialect)


# -- 2. random valid ASTs (rendered back to text) -------------------------------------------

field_refs = st.sampled_from([f"{a}.{c}" for a, cols in FIELDS.items() for c in cols])
literals = st.one_of(
    st.integers(-10**6, 10**6).map(str),
    st.decimals(-10**4, 10**4, places=3, allow_nan=False, allow_infinity=False).map(
        lambda d: f"{d:f}"),
    st.text(max_size=12).map(lambda s: "'" + s.replace("'", "''") + "'"),
    st.sampled_from(["TRUE", "FALSE", "NULL", "'2026-01-01'"]),
)
calc_refs = st.sampled_from([f"[{n}]" for n in CALCS])
leaves = st.one_of(field_refs, literals, calc_refs)


def extend(children):
    binop = st.tuples(children, st.sampled_from(["+", "-", "*", "/", "=", "<>", "<", "<=", ">",
                                                 ">=", "AND", "OR"]), children).map(
        lambda t: f"({t[0]} {t[1]} {t[2]})")
    unary = st.one_of(children.map(lambda c: f"NOT ({c})"), children.map(lambda c: f"-({c})"))
    func1 = st.tuples(st.sampled_from(sorted(FUNCTIONS)), st.lists(children, min_size=1,
                                                                    max_size=3)).map(
        lambda t: f"{t[0]}({', '.join(t[1])})")
    case = st.tuples(st.lists(st.tuples(children, children), min_size=1, max_size=2),
                     st.none() | children).map(
        lambda t: "CASE " + " ".join(f"WHEN {w} THEN {r}" for w, r in t[0])
        + (f" ELSE {t[1]}" if t[1] else "") + " END")
    return st.one_of(binop, unary, func1, case)


valid_asts = st.recursive(leaves, extend, max_leaves=12)


@FUZZ
@given(valid_asts, st.sampled_from(list(Dialect)))
def test_random_valid_asts(text, dialect):
    try_compile(text, dialect)


# -- 3. well-typed ASTs (type-directed generation, so most reach the compiler) -------------

num_leaf = st.one_of(
    st.sampled_from(["o.id", "o.total_amount", "o.discount", "o.rate", "[days]"]),
    st.integers(0, 10**6).map(str),
    st.decimals(0, 10**4, places=2, allow_nan=False, allow_infinity=False).map(lambda d: f"{d:f}"),
)
text_leaf = st.one_of(
    st.sampled_from(["c.country", "o.status"]),
    st.text(max_size=10).map(lambda s: "'" + s.replace("'", "''") + "'"),
)
date_leaf = st.sampled_from(["o.ordered_at", "o.shipped_at"])


def _num(children):
    return st.one_of(
        st.tuples(children, st.sampled_from("+-*/"), children).map(lambda t: f"({t[0]} {t[1]} {t[2]})"),
        children.map(lambda c: f"ABS({c})"), children.map(lambda c: f"FLOOR({c})"),
        children.map(lambda c: f"CEILING({c})"), children.map(lambda c: f"ROUND({c}, 2)"),
        st.tuples(children, children).map(lambda t: f"COALESCE({t[0]}, {t[1]})"),
        st.tuples(children, children).map(lambda t: f"NULLIF({t[0]}, {t[1]})"),
        text_leaf.map(lambda t: f"LENGTH(UPPER({t}))"),
        date_leaf.map(lambda d: f"YEAR({d})"),
        st.tuples(date_leaf, date_leaf).map(lambda t: f"DATE_DIFF_DAYS({t[0]}, {t[1]})"),
        st.tuples(children, children, children).map(
            lambda t: f"CASE WHEN {t[0]} > {t[1]} THEN {t[2]} ELSE NULL END"),
    )


row_num = st.recursive(num_leaf, _num, max_leaves=8)
agg_num = st.one_of(
    st.tuples(st.sampled_from(["SUM", "AVG", "MIN", "MAX", "COUNT"]), row_num).map(
        lambda t: f"{t[0]}({t[1]})"),
    st.sampled_from(["[revenue]", "[orders]", "COUNT_DISTINCT(c.country)"]),
)
bool_expr = st.one_of(
    st.tuples(row_num, st.sampled_from(["=", "<>", "<", ">="]), row_num).map(" ".join),
    st.tuples(text_leaf, text_leaf).map(lambda t: f"{t[0]} = {t[1]}"),
    date_leaf.map(lambda d: f"{d} >= '2026-01-01'"),
)
well_typed = st.one_of(
    row_num,
    bool_expr,
    st.tuples(bool_expr, bool_expr).map(lambda t: f"NOT ({t[0]}) OR ({t[1]})"),
    st.tuples(text_leaf, row_num).map(lambda t: f"CONCAT({t[0]}, {t[1]})"),
    st.tuples(agg_num, st.sampled_from("+-*/"), agg_num).map(" ".join),
    st.tuples(agg_num, agg_num).map(lambda t: f"{t[0]} / NULLIF({t[1]}, 0)"),
)


@FUZZ
@given(well_typed, st.sampled_from(list(Dialect)))
def test_well_typed_asts_compile_safely(text, dialect):
    sql, params, _ = compile_sql(text, dialect)  # must not raise: these are well typed
    assert_safe_sql(sql)
