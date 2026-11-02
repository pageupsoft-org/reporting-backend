"""Normalise driver-specific result types on the DataFrame (plan §3.4)."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from reporting.core import DataType
from reporting.query._internal.periods import bucket_starts
from reporting.query.api import GapFill, OutputColumn

EXACT_FLOAT_DIGITS = 15


def _decimal_column(s: pd.Series) -> pd.Series:
    """Decimals become float64 unless a whole-number part is too large for a float to hold
    exactly (≥ 10^15), in which case they stay exact strings.

    Extra fractional digits (PostgreSQL returns AVG as e.g. 3.0813008130081301) are rounded,
    so the same report gives numbers on every engine.
    """
    limit = Decimal(10) ** EXACT_FLOAT_DIGITS
    fits = all(not v.is_finite() or abs(v) < limit for v in s if isinstance(v, Decimal))
    if fits:
        return pd.to_numeric(s, errors="coerce").astype("float64")
    return s.map(lambda v: None if v is None else str(v)).astype("string")


def _to_bool(v: object) -> bool | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, bytes):  # MySQL BIT(1)
        return v not in (b"", b"\x00")
    if isinstance(v, str):
        return v.strip().lower() in ("true", "t", "1", "yes", "y")
    return bool(v)


def normalize(frame: pd.DataFrame, columns: tuple[OutputColumn, ...]) -> pd.DataFrame:
    out = frame.copy()
    for col in columns:
        if col.id not in out.columns:
            continue
        s = out[col.id]
        t = col.data_type
        if t == DataType.INTEGER:
            num = pd.to_numeric(s, errors="coerce")
            out[col.id] = num.round().astype("Int64") if num.notna().any() or len(num) else num
        elif t == DataType.DECIMAL:
            out[col.id] = _decimal_column(s)
        elif t == DataType.FLOAT:
            out[col.id] = pd.to_numeric(s, errors="coerce").astype("float64")
        elif t == DataType.BOOLEAN:
            out[col.id] = s.map(_to_bool).astype("boolean")
        elif t in (DataType.DATE, DataType.DATETIME):
            parsed = pd.to_datetime(s, errors="coerce", utc=False, format="mixed")
            if getattr(parsed.dt, "tz", None) is not None:
                parsed = parsed.dt.tz_convert("UTC").dt.tz_localize(None)
            out[col.id] = parsed
        elif t == DataType.DATETIME_TZ:
            out[col.id] = pd.to_datetime(s, errors="coerce", utc=True, format="mixed")
        elif t in (DataType.TEXT, DataType.UUID):
            out[col.id] = s.map(lambda v: None if v is None else str(v)).astype("string")
    return out


def fill_gaps(frame: pd.DataFrame, gap: GapFill,
              order: list[tuple[str, bool]]) -> pd.DataFrame:
    """Add a row for every empty date bucket in the period; sums and counts there are 0.

    ``order`` is the spec's ordering as (column id, descending); the date ascending otherwise.
    """
    if gap.column not in frame.columns:
        return frame
    buckets = pd.DatetimeIndex(pd.to_datetime(bucket_starts(gap.grain, gap.start, gap.end)))
    data = frame.assign(**{gap.column: pd.to_datetime(frame[gap.column])})
    data = data[data[gap.column].notna()].set_index(gap.column)
    data = data[~data.index.duplicated()]
    out = data.reindex(data.index.union(buckets))
    for cid in gap.zero_columns:
        if cid in out.columns:
            out[cid] = out[cid].fillna(0)
    out = out.rename_axis(gap.column).reset_index()[list(frame.columns)]
    keys = [(c, d) for c, d in order if c in out.columns] or [(gap.column, False)]
    return out.sort_values([c for c, _ in keys], ascending=[not d for _, d in keys],
                           kind="stable", na_position="last").reset_index(drop=True)
