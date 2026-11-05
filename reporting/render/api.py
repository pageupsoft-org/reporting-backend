"""Public API of the render module: turns query results into display payloads with pandas.

``render`` imports no other module; the HTTP layer hands it a ``core.QueryResult``.
Payloads are plain JSON (a ``ChartPayload.option`` is an Apache ECharts option object).
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import Field

from reporting.core import Frozen, OutputColumn, QueryResult

ChartType = Literal["bar", "line", "area", "pie", "scatter"]
TimeGrain = Literal["day", "week", "month", "quarter", "year"]
ExportFormat = Literal["csv", "xlsx"]


class TableOptions(Frozen):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=10_000)
    null_label: str = "(blank)"


class TablePayload(Frozen):
    columns: tuple[OutputColumn, ...]
    rows: list[list[Any]]  # JSON-safe raw values
    display: list[list[str]]  # formatted strings, nulls labelled
    total_rows: int
    page: int
    page_size: int
    truncated: bool


class ChartSpec(Frozen):
    type: ChartType
    x: str  # dimension column id (or measure for scatter)
    y: tuple[str, ...]  # measure column ids
    series: str | None = None  # optional second dimension, pivoted into series
    stacked: bool = False
    percent: bool = False  # percent of total per x
    top_n: int | None = Field(default=None, ge=1, le=100)  # pie / bar: top N + "Other"
    time_grain: TimeGrain | None = None  # resample a temporal x axis
    max_points: int = Field(default=1000, ge=10, le=20_000)  # LTTB downsampling
    title: str | None = None


class ChartPayload(Frozen):
    spec: ChartSpec
    option: dict[str, Any]
    points: int
    downsampled: bool = False


class ExportFile(Frozen):
    filename: str
    content_type: str
    data: bytes


class RenderService(Protocol):
    def table(self, result: QueryResult, opts: TableOptions) -> TablePayload: ...
    def chart(self, result: QueryResult, spec: ChartSpec) -> ChartPayload: ...
    def suggest_charts(self, result: QueryResult) -> list[ChartSpec]: ...
    def export(self, result: QueryResult, fmt: ExportFormat) -> ExportFile: ...
    def export_workbook(self, sheets: list[tuple[str, QueryResult]],
                        filename: str) -> ExportFile: ...


def create_render_service() -> RenderService:
    from reporting.render._internal.service import PandasRenderService

    return PandasRenderService()


__all__ = [
    "ChartPayload",
    "ChartSpec",
    "ChartType",
    "ExportFile",
    "ExportFormat",
    "RenderService",
    "TableOptions",
    "TablePayload",
    "TimeGrain",
    "create_render_service",
]
