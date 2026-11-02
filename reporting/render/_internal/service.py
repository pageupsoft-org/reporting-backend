"""pandas-based implementation of ``RenderService``."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from reporting.core import DataType, Issue, OutputColumn, QueryResult, ValidationFailed
from reporting.render._internal.shaping import (
    GRAIN_FREQ,
    OTHER,
    format_value,
    json_value,
    label_nulls,
    lttb,
    numeric,
    percent_of_total,
    percent_within_x,
)
from reporting.render.api import (
    ChartPayload,
    ChartSpec,
    ExportFile,
    ExportFormat,
    TableOptions,
    TablePayload,
)

NULL_LABEL = "(blank)"
PIE_SUGGEST_MAX = 8


def _columns(result: QueryResult) -> dict[str, OutputColumn]:
    return {c.id: c for c in result.columns}


class PandasRenderService:
    # -- table ----------------------------------------------------------------------------

    def table(self, result: QueryResult, opts: TableOptions) -> TablePayload:
        frame = result.frame
        start = (opts.page - 1) * opts.page_size
        page = frame.iloc[start:start + opts.page_size]
        cols = [c for c in result.columns if c.id in frame.columns]
        rows = [[json_value(v) for v in rec] for rec in page[[c.id for c in cols]].itertuples(
            index=False, name=None)]
        display = [[format_value(v, c, opts.null_label) for v, c in zip(r, cols, strict=True)]
                   for r in rows]
        return TablePayload(columns=tuple(cols), rows=rows, display=display,
                            total_rows=len(frame), page=opts.page, page_size=opts.page_size,
                            truncated=result.truncated)

    # -- charts ---------------------------------------------------------------------------

    def chart(self, result: QueryResult, spec: ChartSpec) -> ChartPayload:
        cols = _columns(result)
        issues = [Issue(path, f"Unknown column '{cid}'") for path, cid in
                  [("x", spec.x), *[(f"y[{i}]", y) for i, y in enumerate(spec.y)],
                   *([("series", spec.series)] if spec.series else [])] if cid not in cols]
        if not spec.y:
            issues.append(Issue("y", "Pick at least one measure"))
        if spec.type == "pie" and (spec.series or len(spec.y) > 1):
            issues.append(Issue("type", "A pie chart shows one measure without series"))
        if issues:
            raise ValidationFailed("Invalid chart", issues)

        frame = result.frame
        x_col = cols[spec.x]
        if spec.type == "scatter":
            return self._scatter(frame, spec, cols)

        # Date buckets (day/month/year) are ordered categories: one bar or point per bucket.
        bucketed = x_col.grain is not None
        temporal = x_col.data_type.is_temporal and not bucketed
        if spec.series:
            wide = frame.assign(
                **{spec.series: label_nulls(frame[spec.series], NULL_LABEL).astype(str)}
            ).pivot_table(index=spec.x, columns=spec.series, values=spec.y[0], aggfunc="sum",
                          dropna=False, observed=True, sort=False)
            wide = wide.apply(numeric)
            names = [str(c) for c in wide.columns]
            wide.columns = names
        else:
            wide = frame.set_index(spec.x)[list(spec.y)].apply(numeric)
            names = [cols[y].label for y in spec.y]

        if bucketed:
            wide.index = pd.to_datetime(wide.index, errors="coerce")
            wide = wide[wide.index.notna()].sort_index()
        elif temporal:
            wide.index = pd.to_datetime(wide.index, errors="coerce")
            wide = wide[wide.index.notna()].sort_index()
            if spec.time_grain:
                wide = wide.resample(GRAIN_FREQ[spec.time_grain]).sum(min_count=1)
        elif spec.top_n and len(wide) > spec.top_n:
            order = wide.sum(axis=1).sort_values(ascending=False).index
            head, tail = wide.loc[order[: spec.top_n]], wide.loc[order[spec.top_n:]]
            other = pd.DataFrame([tail.sum(min_count=1)], index=[OTHER])
            wide = pd.concat([head, other])

        if spec.percent:
            if spec.series or (spec.stacked and wide.shape[1] > 1):
                wide = percent_within_x(wide)
            else:
                wide = percent_of_total(wide.reset_index(drop=True), list(wide.columns)).set_index(
                    wide.index)

        downsampled = False
        if temporal and spec.type in ("line", "area") and len(wide) > spec.max_points:
            xs = wide.index.astype("int64").to_numpy(dtype="float64")
            ys = wide.iloc[:, 0].fillna(0).to_numpy(dtype="float64")
            wide = wide.iloc[lttb(xs, ys, spec.max_points)]
            downsampled = True

        option = (self._pie_option(wide, spec, x_col) if spec.type == "pie"
                  else self._cartesian_option(wide, names, spec, x_col, temporal))
        if spec.title:
            option["title"] = {"text": spec.title, "left": "center"}
        return ChartPayload(spec=spec, option=option, points=len(wide), downsampled=downsampled)

    @staticmethod
    def _category_labels(index: pd.Index, x_col: OutputColumn) -> list[str]:
        return [OTHER if v == OTHER else format_value(v, x_col, NULL_LABEL) for v in index]

    def _cartesian_option(self, wide: pd.DataFrame, names: list[str], spec: ChartSpec,
                          x_col: OutputColumn, temporal: bool) -> dict[str, Any]:
        series = []
        for name, col in zip(names, wide.columns, strict=True):
            values = [json_value(v) for v in wide[col]]
            data: list[Any] = (
                [[ts.isoformat(), v] for ts, v in zip(wide.index, values, strict=True)]
                if temporal else values
            )
            s: dict[str, Any] = {
                "name": name, "type": "line" if spec.type == "area" else spec.type,
                "data": data, "emphasis": {"focus": "series"},
            }
            if spec.type == "area":
                s["areaStyle"] = {}
            if spec.stacked or spec.percent and spec.series:
                s["stack"] = "total"
            if spec.type in ("line", "area"):
                s["showSymbol"] = len(wide) <= 60
            series.append(s)
        x_axis: dict[str, Any] = (
            {"type": "time"} if temporal
            else {"type": "category", "data": self._category_labels(wide.index, x_col),
                  "axisLabel": {"interval": 0 if len(wide) <= 24 else "auto", "hideOverlap": True,
                                "rotate": 30 if len(wide) > 8 else 0}}
        )
        x_axis.update(name=x_col.label, nameLocation="middle", nameGap=30)
        option: dict[str, Any] = {
            "tooltip": {"trigger": "axis"},
            "grid": {"left": 48, "right": 24, "top": 40, "bottom": 48, "containLabel": True},
            "xAxis": x_axis,
            "yAxis": {"type": "value", **({"max": 100, "axisLabel": {"formatter": "{value}%"}}
                                         if spec.percent else {})},
            "series": series,
        }
        if len(series) > 1:
            option["legend"] = {"type": "scroll", "top": 0}
        return option

    def _pie_option(self, wide: pd.DataFrame, spec: ChartSpec,
                    x_col: OutputColumn) -> dict[str, Any]:
        labels = self._category_labels(wide.index, x_col)
        values = [json_value(v) for v in wide.iloc[:, 0]]
        return {
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "legend": {"type": "scroll", "orient": "vertical", "left": "left"},
            "series": [{
                "type": "pie", "radius": ["35%", "70%"], "avoidLabelOverlap": True,
                "data": [{"name": n, "value": v} for n, v in zip(labels, values, strict=True)
                         if v is not None],
            }],
        }

    def _scatter(self, frame: pd.DataFrame, spec: ChartSpec,
                 cols: dict[str, OutputColumn]) -> ChartPayload:
        xs, ys = numeric(frame[spec.x]), numeric(frame[spec.y[0]])
        points = [[json_value(a), json_value(b)] for a, b in zip(xs, ys, strict=True)
                  if pd.notna(a) and pd.notna(b)]
        downsampled = False
        if len(points) > spec.max_points:
            idx = np.linspace(0, len(points) - 1, spec.max_points).astype(int)
            points, downsampled = [points[i] for i in idx], True
        option = {
            "tooltip": {"trigger": "item"},
            "xAxis": {"type": "value", "name": cols[spec.x].label, "scale": True},
            "yAxis": {"type": "value", "name": cols[spec.y[0]].label, "scale": True},
            "series": [{"type": "scatter", "data": points, "symbolSize": 8}],
        }
        return ChartPayload(spec=spec, option=option, points=len(points), downsampled=downsampled)

    # -- suggestions ----------------------------------------------------------------------

    def suggest_charts(self, result: QueryResult) -> list[ChartSpec]:
        frame = result.frame
        dims = [c for c in result.columns if c.role == "dimension"]
        measures = [c for c in result.columns if c.role == "measure"
                    and (c.data_type.is_numeric or c.data_type == DataType.DECIMAL)]
        if not measures or len(frame) < 2:
            return []
        # One measure per suggestion: measures of different scale never share an axis.
        y = (measures[0].id,)
        out: list[ChartSpec] = []
        temporal = [d for d in dims if d.data_type.is_temporal]
        categorical = [d for d in dims if not d.data_type.is_temporal]
        if len(dims) == 1 and dims[0].grain in ("month", "year"):
            out.append(ChartSpec(type="bar", x=dims[0].id, y=y))
            out.append(ChartSpec(type="line", x=dims[0].id, y=y))
        elif len(dims) == 1 and temporal:
            out.append(ChartSpec(type="line", x=temporal[0].id, y=y))
            out.append(ChartSpec(type="bar", x=temporal[0].id, y=y))
        elif len(dims) == 1:
            d = categorical[0]
            n = frame[d.id].nunique(dropna=False)
            out.append(ChartSpec(type="bar", x=d.id, y=y, top_n=20 if n > 20 else None))
            if len(measures) == 1:
                out.append(ChartSpec(type="pie", x=d.id, y=y[:1],
                                     top_n=PIE_SUGGEST_MAX if n > PIE_SUGGEST_MAX else None))
        elif len(dims) == 2:
            a, b = dims
            if b.data_type.is_temporal and not a.data_type.is_temporal:
                a, b = b, a
            if frame[b.id].nunique() > frame[a.id].nunique() and not a.data_type.is_temporal:
                a, b = b, a
            kind = "line" if a.data_type.is_temporal else "bar"
            out.append(ChartSpec(type=kind, x=a.id, y=y[:1], series=b.id,
                                 stacked=kind == "bar"))
        if len(measures) >= 2 and len(frame) >= 5:
            out.append(ChartSpec(type="scatter", x=measures[0].id, y=(measures[1].id,)))
        return out

    # -- export ---------------------------------------------------------------------------

    def export(self, result: QueryResult, fmt: ExportFormat) -> ExportFile:
        cols = [c for c in result.columns if c.id in result.frame.columns]
        frame = result.frame[[c.id for c in cols]].copy()
        frame.columns = [c.label for c in cols]
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
        if fmt == "csv":
            data = frame.to_csv(index=False, date_format="%Y-%m-%d %H:%M:%S").encode("utf-8-sig")
            return ExportFile(filename=f"report-{stamp}.csv", content_type="text/csv", data=data)
        for c in frame.columns:  # Excel cannot store timezone-aware datetimes
            if isinstance(frame[c].dtype, pd.DatetimeTZDtype):
                frame[c] = frame[c].dt.tz_convert("UTC").dt.tz_localize(None)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            frame.to_excel(writer, index=False, sheet_name="Report")
            sheet = writer.sheets["Report"]
            sheet.freeze_panes = "A2"
            for i, c in enumerate(frame.columns, start=1):
                width = max(len(str(c)), *(len(str(v)) for v in frame[c].head(200))) \
                    if len(frame) else len(str(c))
                sheet.column_dimensions[sheet.cell(1, i).column_letter].width = min(width + 2, 60)
        return ExportFile(
            filename=f"report-{stamp}.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            data=buf.getvalue(),
        )
