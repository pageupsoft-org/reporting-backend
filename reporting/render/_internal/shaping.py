"""pandas shaping only: aggregation already happened in SQL (plan §3.4)."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from reporting.core import DataType, OutputColumn

OTHER = "Other"
GRAIN_FREQ = {"day": "D", "week": "W-MON", "month": "MS", "quarter": "QS", "year": "YS"}


def json_value(v: Any) -> Any:
    """Convert a pandas/numpy scalar to a JSON-safe value."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, np.generic):
        return json_value(v.item())
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v) if v.is_finite() else None
    if isinstance(v, bytes):
        return v.hex()
    return v


def format_value(v: Any, col: OutputColumn, null_label: str) -> str:
    v = json_value(v)
    if v is None:
        return null_label
    t = col.data_type
    if col.grain and isinstance(v, str):
        d = datetime.fromisoformat(v)
        return {"day": d.strftime("%d %b %Y"), "month": d.strftime("%b %Y"),
                "year": str(d.year)}[col.grain]
    if t == DataType.INTEGER and isinstance(v, (int, float)):
        return f"{int(v):,}"
    if t in (DataType.DECIMAL, DataType.FLOAT) and isinstance(v, (int, float)):
        return f"{v:,.2f}"
    if isinstance(v, str) and t == DataType.DECIMAL:  # exact decimal kept as text
        return v
    if t == DataType.DATE and isinstance(v, str):
        return v[:10]
    if t in (DataType.DATETIME, DataType.DATETIME_TZ) and isinstance(v, str):
        text = v.replace("T", " ")
        return text[:10] if text[11:19] == "00:00:00" else text[:19]
    if t == DataType.BOOLEAN:
        return "Yes" if v else "No"
    return str(v)


def numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64")


def label_nulls(s: pd.Series, null_label: str) -> pd.Series:
    return s.astype("object").where(s.notna(), null_label)


def percent_of_total(frame: pd.DataFrame, y: list[str]) -> pd.DataFrame:
    out = frame.copy()
    for m in y:
        values = numeric(out[m])
        total = values.sum()
        out[m] = (values / total * 100) if total else 0.0
    return out


def percent_within_x(wide: pd.DataFrame) -> pd.DataFrame:
    totals = wide.sum(axis=1).replace(0, np.nan)
    return wide.div(totals, axis=0).mul(100).fillna(0)


def lttb(xs: np.ndarray, ys: np.ndarray, threshold: int) -> np.ndarray:
    """Largest-Triangle-Three-Buckets downsampling; returns indices of kept points."""
    n = len(xs)
    if threshold >= n or threshold < 3:
        return np.arange(n)
    every = (n - 2) / (threshold - 2)
    keep = [0]
    a = 0
    for i in range(threshold - 2):
        # Average of the *next* bucket is the third triangle vertex.
        nxt_start = int(math.floor((i + 1) * every)) + 1
        nxt_end = min(int(math.floor((i + 2) * every)) + 1, n)
        avg_x, avg_y = xs[nxt_start:nxt_end].mean(), ys[nxt_start:nxt_end].mean()
        # Pick the point of the current bucket forming the largest triangle.
        start = int(math.floor(i * every)) + 1
        end = int(math.floor((i + 1) * every)) + 1
        areas = np.abs((xs[a] - avg_x) * (ys[start:end] - ys[a])
                       - (xs[a] - xs[start:end]) * (avg_y - ys[a]))
        a = start + int(np.argmax(areas))
        keep.append(a)
    keep.append(n - 1)
    return np.array(keep)
