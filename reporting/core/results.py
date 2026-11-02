"""Query results: produced by ``query``, consumed by ``render`` via the HTTP layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

from reporting.core.types import DataType, Frozen

Role = Literal["dimension", "measure"]


class OutputColumn(Frozen):
    id: str
    label: str
    data_type: DataType
    role: Role
    source: str = ""  # e.g. "customers.country"
    grain: Literal["day", "month", "year"] | None = None  # date buckets


class ResultWarning(Frozen):
    path: str
    message: str
    severity: Literal["error", "warning"] = "warning"
    code: str = "warning"


@dataclass(frozen=True)
class QueryResult:
    frame: pd.DataFrame
    columns: tuple[OutputColumn, ...]
    row_count: int
    truncated: bool
    elapsed_ms: int
    cache_hit: bool = False
    sql: str = ""
    warnings: tuple[Frozen, ...] = field(default_factory=tuple)
