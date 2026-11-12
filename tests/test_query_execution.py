import pandas as pd
import pytest

from reporting.core import AccessContext, ExecutionLimits, ValidationFailed
from reporting.query import parse_spec
from tests.spec_fixtures import example_spec


def run(query, conn, raw, **kw):
    return query.execute(parse_spec(raw), AccessContext.unrestricted(conn.id),
                         ExecutionLimits(**kw))


def test_execute_example(query, sales_conn):
    r = run(query, sales_conn, example_spec(sales_conn.id))
    f = r.frame
    assert list(f.columns) == ["country", "revenue", "orders", "avg_ship", "aov"]
    assert list(f["country"]) == ["Germany", "France", "USA"]
    assert list(f["revenue"]) == [350.0, 300.0, 75.5]
    assert list(f["orders"]) == [2, 1, 1] and str(f["orders"].dtype) == "Int64"
    assert list(f["avg_ship"]) == [2.0, 2.0, 5.0]  # calendar days, time of day ignored
    assert list(f["aov"]) == [175.0, 300.0, 75.5]
    assert not r.cache_hit and r.row_count == 3 and "SELECT" in r.sql


def test_cache_hit(query, sales_conn):
    run(query, sales_conn, example_spec(sales_conn.id))
    again = run(query, sales_conn, example_spec(sales_conn.id))
    assert again.cache_hit and list(again.frame["revenue"]) == [350.0, 300.0, 75.5]


def test_preview_caps_rows(query, sales_conn):
    raw = example_spec(sales_conn.id, columns=[{"id": "id", "kind": "field", "ref": "o.id"}],
                       filters=[], having=[], order_by=[{"ref": "id"}], limit=5000, joins=[])
    r = query.preview(parse_spec(raw), AccessContext.unrestricted(sales_conn.id))
    assert r.row_count == 5 and not r.truncated


def test_row_cap_truncates(query, sales_conn):
    raw = example_spec(sales_conn.id, columns=[{"id": "id", "kind": "field", "ref": "o.id"}],
                       filters=[], having=[], order_by=[], joins=[])
    r = run(query, sales_conn, raw, max_rows=3)
    assert r.row_count == 3 and r.truncated


def test_types_are_normalised(query, sales_conn):
    raw = example_spec(sales_conn.id, columns=[
        {"id": "id", "kind": "field", "ref": "o.id"},
        {"id": "at", "kind": "field", "ref": "o.ordered_at"},
        {"id": "status", "kind": "field", "ref": "o.status"},
        {"id": "amt", "kind": "field", "ref": "o.total_amount"},
        {"id": "big", "kind": "expression", "expr": "o.total_amount > 100"},
    ], filters=[], having=[], order_by=[{"ref": "id"}], joins=[])
    f = run(query, sales_conn, raw).frame
    assert pd.api.types.is_datetime64_any_dtype(f["at"])
    assert f["at"][0] == pd.Timestamp("2026-01-05 10:00:00")
    assert str(f["status"].dtype) == "string" and str(f["big"].dtype) == "boolean"
    assert list(f["big"]) == [False, True, False, False, True]


def test_expression_functions_execute(query, sales_conn):
    raw = example_spec(sales_conn.id, columns=[
        {"id": "id", "kind": "field", "ref": "o.id"},
        {"id": "net", "kind": "expression", "expr": "(o.total_amount - o.discount) * 1.18"},
        {"id": "half", "kind": "expression", "expr": "o.id / 2"},
        {"id": "up", "kind": "expression", "expr": "UPPER(CONCAT(c.country, '-', o.status))"},
        {"id": "yr", "kind": "expression", "expr": "YEAR(o.ordered_at) * 100 + MONTH(o.ordered_at)"},
        {"id": "d", "kind": "expression", "expr": "DATE_DIFF_DAYS(o.ordered_at, o.shipped_at)"},
        {"id": "fl", "kind": "expression", "expr": "FLOOR(-1.5) + CEILING(1.2) + ROUND(2.567, 2)"},
        {"id": "bucket", "kind": "expression",
         "expr": "CASE WHEN o.total_amount >= 250 THEN 'big' WHEN o.total_amount >= 100 "
                 "THEN 'mid' ELSE 'small' END"},
        {"id": "len", "kind": "expression", "expr": "LENGTH(TRIM(c.name)) + ABS(-1)"},
        {"id": "co", "kind": "expression", "expr": "COALESCE(o.shipped_at, '2030-01-01') > '2026-03-01'"},
    ], filters=[], having=[], order_by=[{"ref": "id"}])
    f = run(query, sales_conn, raw).frame
    row = f.iloc[1]
    assert row["net"] == pytest.approx((250 - 10) * 1.18)
    assert f["half"].iloc[0] == 0.5  # true division, not integer division
    assert row["up"] == "GERMANY-SHIPPED"
    assert row["yr"] == 202602 and row["d"] == 1
    assert row["fl"] == pytest.approx(-2 + 2 + 2.57)
    assert list(f["bucket"]) == ["mid", "big", "small", "small", "big"]
    assert row["len"] == 5


def test_bare_column_beside_aggregate_is_grouped(query, sales_conn):
    # Outstanding per order: the order total minus the sum of its items.
    raw = example_spec(sales_conn.id, joins=[
        {"alias": "i", "from_alias": "o", "relationship_id": "fk_order_items_order_id",
         "type": "left"}], columns=[
        {"id": "id", "kind": "field", "ref": "o.id"},
        {"id": "gap", "kind": "expression", "expr": "o.total_amount - SUM(i.quantity * i.unit_price)"},
    ], filters=[], having=[], order_by=[{"ref": "id"}])
    r = run(query, sales_conn, raw)
    assert "GROUP BY" in r.sql and "total_amount" in r.sql.split("GROUP BY")[1]
    f = r.frame
    assert list(f["id"]) == [1, 2, 3, 4, 5]
    assert [round(float(v), 2) if pd.notna(v) else None for v in f["gap"]] == \
        [0.0, 0.0, -0.01, None, 0.0]



def _items_spec(conn, columns, **kw):
    return example_spec(conn.id, joins=[
        {"alias": "c", "from_alias": "o", "relationship_id": "fk_orders_customer_id",
         "type": "left"},
        {"alias": "i", "from_alias": "o", "relationship_id": "fk_order_items_order_id",
         "type": "left"}], columns=columns, filters=[], having=[], **kw)


def test_nested_rollup_runs_per_row_then_rolls_up(query, sales_conn):
    # Per order: total minus the item quantities; then summed per customer.
    raw = _items_spec(sales_conn, [
        {"id": "name", "kind": "field", "ref": "c.name"},
        {"id": "out", "kind": "expression", "expr": "o.total_amount - SUM(i.quantity)",
         "rollup": "sum"},
        {"id": "items", "kind": "aggregate", "fn": "count", "arg": "i.id"},
        {"id": "per", "kind": "expression", "expr": "[out] / NULLIF([items], 0)"},
    ], order_by=[{"ref": "name"}])
    r = run(query, sales_conn, raw)
    assert "detail" in r.sql
    f = r.frame
    assert list(f["name"]) == ["Acme", "Globex", "Initech", "Umbrella"]
    assert [None if pd.isna(v) else float(v) for v in f["out"]] == [327.0, 72.5, None, 270.0]
    assert list(f["items"]) == [3, 1, 0, 1]
    assert [None if pd.isna(v) else float(v) for v in f["per"]] == [109.0, 72.5, None, 270.0]

    raw["having"] = [{"ref": "out", "op": "gt", "value": 100}]
    raw["order_by"] = [{"ref": "out", "dir": "desc"}]
    assert list(run(query, sales_conn, raw).frame["name"]) == ["Acme", "Umbrella"]


def test_rollup_of_row_level_expression(query, sales_conn):
    raw = example_spec(sales_conn.id, columns=[
        {"id": "country", "kind": "field", "ref": "c.country"},
        {"id": "net", "kind": "expression", "expr": "o.total_amount - o.discount",
         "rollup": "sum"},
    ], filters=[], having=[], order_by=[{"ref": "country"}])
    r = run(query, sales_conn, raw)
    assert "detail" not in r.sql
    assert [float(v) for v in r.frame["net"]] == [275.0, 340.0, 115.5]


@pytest.mark.parametrize("extra,fragment", [
    ({"id": "a", "kind": "aggregate", "fn": "avg", "arg": "o.discount"},
     "avg can't be combined with a nested roll-up"),
    ({"id": "b", "kind": "expression", "expr": "SUM(i.quantity) * 2"}, "'b' needs a roll-up"),
    ({"id": "t", "kind": "expression", "expr": "c.name", "rollup": "sum"}, "Sum needs a number"),
])
def test_rollup_validation(query, sales_conn, extra, fragment):
    raw = _items_spec(sales_conn, [
        {"id": "name", "kind": "field", "ref": "c.name"},
        {"id": "out", "kind": "expression", "expr": "o.total_amount - SUM(i.quantity)",
         "rollup": "sum"}, extra])
    with pytest.raises(ValidationFailed) as exc:
        run(query, sales_conn, raw)
    assert any(fragment in i.message for i in exc.value.issues), exc.value.issues

def test_execute_rejects_invalid(query, sales_conn):
    raw = example_spec(sales_conn.id)
    raw["columns"][0]["ref"] = "c.nope"
    with pytest.raises(ValidationFailed) as exc:
        run(query, sales_conn, raw)
    assert exc.value.issues[0].path == "columns[0].ref"


def test_exact_decimals_become_strings():
    from decimal import Decimal

    from reporting.core import DataType
    from reporting.query import OutputColumn
    from reporting.query._internal.normalize import normalize
    cols = (OutputColumn(id="a", label="a", data_type=DataType.DECIMAL, role="measure"),)
    small = normalize(pd.DataFrame({"a": [Decimal("1.25"), None]}), cols)
    assert str(small["a"].dtype) == "float64"
    big = normalize(pd.DataFrame({"a": [Decimal("12345678901234567.89")]}), cols)
    assert big["a"][0] == "12345678901234567.89"
    # long fractions (PostgreSQL AVG) are still numbers
    avg = normalize(pd.DataFrame({"a": [Decimal("3.0813008130081301")]}), cols)
    assert str(avg["a"].dtype) == "float64" and round(avg["a"][0], 6) == 3.081301


def test_cache_max_age():
    import time as _t

    from reporting.query._internal.cache import MemoryCache
    c = MemoryCache()
    c.set("k", pd.DataFrame({"a": [1]}), False, ttl_s=300)
    assert c.get("k") is not None and c.get("k", max_age_s=60) is not None
    _t.sleep(0.05)
    assert c.get("k", max_age_s=0.01) is None and c.get("k") is not None


def test_redis_cache_roundtrip():
    import time as _t

    import fakeredis

    from reporting.query._internal.cache import RedisCache
    c = RedisCache("redis://unused", client=fakeredis.FakeRedis())
    frame = pd.DataFrame({"country": ["DE", "FR"], "revenue": [1.5, 2.25]})
    c.set("k", frame, True, ttl_s=60)
    got, truncated = c.get("k")
    assert truncated is True and got.equals(frame)
    assert c.get("missing") is None
    _t.sleep(0.05)
    assert c.get("k", max_age_s=0.01) is None and c.get("k", max_age_s=60) is not None
