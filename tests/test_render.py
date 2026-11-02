import io

import numpy as np
import pandas as pd
import pytest

from reporting.core import DataType, OutputColumn, QueryResult, ValidationFailed
from reporting.render import ChartSpec, TableOptions, create_render_service
from reporting.render._internal.shaping import lttb

render = create_render_service()


def col(cid, dtype, role, label=None):
    return OutputColumn(id=cid, label=label or cid.title(), data_type=dtype, role=role)


def result(frame, *cols):
    return QueryResult(frame=frame, columns=cols, row_count=len(frame), truncated=False,
                       elapsed_ms=1)


@pytest.fixture
def by_country():
    f = pd.DataFrame({"country": ["Germany", "France", None, "USA", "Spain"],
                      "revenue": [350.0, 300.0, 10.0, 75.5, 20.0],
                      "orders": pd.array([2, 1, 1, 1, 1], dtype="Int64")})
    return result(f, col("country", DataType.TEXT, "dimension"),
                  col("revenue", DataType.DECIMAL, "measure"),
                  col("orders", DataType.INTEGER, "measure"))


def test_table_payload(by_country):
    t = render.table(by_country, TableOptions(page=1, page_size=3))
    assert t.total_rows == 5 and len(t.rows) == 3
    assert t.rows[2] == [None, 10.0, 1]
    assert t.display[0] == ["Germany", "350.00", "2"] and t.display[2][0] == "(blank)"
    page2 = render.table(by_country, TableOptions(page=2, page_size=3))
    assert [r[0] for r in page2.rows] == ["USA", "Spain"]


def test_bar_chart(by_country):
    c = render.chart(by_country, ChartSpec(type="bar", x="country", y=("revenue", "orders")))
    o = c.option
    assert o["xAxis"]["data"] == ["Germany", "France", "(blank)", "USA", "Spain"]
    assert [s["name"] for s in o["series"]] == ["Revenue", "Orders"]
    assert o["series"][0]["data"] == [350.0, 300.0, 10.0, 75.5, 20.0]
    assert "legend" in o


def test_pie_top_n_other(by_country):
    c = render.chart(by_country, ChartSpec(type="pie", x="country", y=("revenue",), top_n=2))
    data = c.option["series"][0]["data"]
    assert data == [{"name": "Germany", "value": 350.0}, {"name": "France", "value": 300.0},
                    {"name": "Other", "value": 105.5}]


def test_percent_of_total(by_country):
    c = render.chart(by_country, ChartSpec(type="bar", x="country", y=("revenue",), percent=True))
    assert sum(c.option["series"][0]["data"]) == pytest.approx(100)


def test_series_pivot_and_stack():
    f = pd.DataFrame({"month": ["Jan", "Jan", "Feb"], "status": ["paid", "shipped", "paid"],
                      "n": [3, 2, 5]})
    r = result(f, col("month", DataType.TEXT, "dimension"), col("status", DataType.TEXT, "dimension"),
               col("n", DataType.INTEGER, "measure"))
    c = render.chart(r, ChartSpec(type="bar", x="month", y=("n",), series="status", stacked=True))
    series = {s["name"]: s["data"] for s in c.option["series"]}
    assert c.option["xAxis"]["data"] == ["Jan", "Feb"]
    assert series == {"paid": [3.0, 5.0], "shipped": [2.0, None]}
    assert all(s["stack"] == "total" for s in c.option["series"])


def test_time_series_resample_and_lttb():
    days = pd.date_range("2026-01-01", periods=400, freq="D")
    f = pd.DataFrame({"day": days, "v": np.sin(np.arange(400) / 10) * 100})
    r = result(f, col("day", DataType.DATE, "dimension"), col("v", DataType.FLOAT, "measure"))
    monthly = render.chart(r, ChartSpec(type="line", x="day", y=("v",), time_grain="month"))
    assert monthly.points == 14 and monthly.option["xAxis"]["type"] == "time"
    assert monthly.option["series"][0]["data"][0][0].startswith("2026-01-01")
    down = render.chart(r, ChartSpec(type="line", x="day", y=("v",), max_points=50))
    assert down.downsampled and down.points == 50


def test_lttb_keeps_extremes():
    xs = np.arange(1000, dtype=float)
    ys = np.zeros(1000)
    ys[500] = 99
    idx = lttb(xs, ys, 20)
    assert 0 in idx and 999 in idx and 500 in idx and len(idx) == 20


def test_chart_validation(by_country):
    with pytest.raises(ValidationFailed) as exc:
        render.chart(by_country, ChartSpec(type="pie", x="nope", y=("revenue", "orders")))
    paths = {i.path for i in exc.value.issues}
    assert paths == {"x", "type"}


def test_suggestions(by_country):
    kinds = [s.type for s in render.suggest_charts(by_country)]
    assert kinds == ["bar", "scatter"]
    one = result(by_country.frame[["country", "revenue"]], *by_country.columns[:2])
    assert [s.type for s in render.suggest_charts(one)] == ["bar", "pie"]
    days = pd.DataFrame({"d": pd.date_range("2026-01-01", periods=3), "v": [1.0, 2.0, 3.0]})
    ts = result(days, col("d", DataType.DATE, "dimension"), col("v", DataType.FLOAT, "measure"))
    assert render.suggest_charts(ts)[0].type == "line"


def test_export_csv_and_excel(by_country):
    csv = render.export(by_country, "csv")
    text = csv.data.decode("utf-8-sig")
    assert text.splitlines()[0] == "Country,Revenue,Orders" and csv.filename.endswith(".csv")
    xlsx = render.export(by_country, "xlsx")
    back = pd.read_excel(io.BytesIO(xlsx.data))
    assert list(back.columns) == ["Country", "Revenue", "Orders"] and len(back) == 5
