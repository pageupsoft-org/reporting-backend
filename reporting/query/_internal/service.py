"""Implementation of ``QueryService``."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from reporting.connections import ConnectionService
from reporting.core import (
    PREVIEW_LIMITS,
    AccessContext,
    ConnectionId,
    ExecutionLimits,
    Issue,
    ValidationFailed,
)
from reporting.metadata import MetadataService
from reporting.query._internal.cache import cache_key, make_cache
from reporting.query._internal.compiler import compile_plan
from reporting.query._internal.normalize import fill_gaps, normalize
from reporting.query._internal.validator import Plan, Validator
from reporting.query.api import CompiledQuery, QueryResult, QuerySpec, ValidationReport


class SqlQueryService:
    def __init__(self, metadata: MetadataService, connections: ConnectionService,
                 cache_url: str | None = None, cache_ttl_s: int = 300,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._metadata = metadata
        self._connections = connections
        self._validator = Validator(metadata, clock)
        self._cache = make_cache(cache_url)
        self._ttl = cache_ttl_s

    def validate(self, spec: QuerySpec, access: AccessContext | None = None) -> ValidationReport:
        plan, issues = self._validator.run(spec, access)
        return ValidationReport(
            valid=plan is not None,
            issues=tuple(issues),
            output_columns=tuple(plan.output) if plan else (),
            snapshot_version=plan.snapshot.version if plan else None,
        )

    def _plan(self, spec: QuerySpec, access: AccessContext) -> Plan:
        if access.connection_id != spec.connection_id:
            raise ValidationFailed("Access context does not match the spec's connection")
        plan, issues = self._validator.run(spec, access)
        if plan is None:
            errors = [Issue(i.path, i.message, i.code) for i in issues if i.severity == "error"]
            raise ValidationFailed(errors[0].message if errors else "Invalid spec", errors)
        return plan

    def compile(self, spec: QuerySpec, access: AccessContext) -> CompiledQuery:
        plan = self._plan(spec, access)
        dialect = self._connections.dialect(ConnectionId(spec.connection_id))
        return compile_plan(plan, dialect)

    def execute(self, spec: QuerySpec, access: AccessContext, limits: ExecutionLimits,
                use_cache: bool = True, max_cache_age_s: float | None = None) -> QueryResult:
        compiled = self.compile(spec, access)
        key = cache_key(compiled.statement_sql, compiled.params, spec.connection_id,
                        compiled.snapshot_version, limits.max_rows)
        started = time.perf_counter()
        cached = self._cache.get(key, max_cache_age_s) if use_cache else None
        if cached is not None:
            frame, truncated = cached
            return QueryResult(
                frame=frame, columns=compiled.output_columns, row_count=len(frame),
                truncated=truncated, elapsed_ms=int((time.perf_counter() - started) * 1000),
                cache_hit=True, sql=compiled.statement_sql, warnings=compiled.warnings,
            )
        result = self._connections.execute_read(
            ConnectionId(spec.connection_id), compiled.statement, limits)
        frame = result.frame
        frame.columns = [c.id for c in compiled.output_columns][: len(frame.columns)]
        frame = normalize(frame, compiled.output_columns)
        if compiled.gap_fill is not None:
            order = [(o.ref, o.dir == "desc") for o in spec.order_by]
            frame = fill_gaps(frame, compiled.gap_fill, order)
        self._cache.set(key, frame, result.truncated, self._ttl)
        return QueryResult(
            frame=frame, columns=compiled.output_columns, row_count=len(frame),
            truncated=result.truncated, elapsed_ms=result.elapsed_ms, cache_hit=False,
            sql=compiled.statement_sql, warnings=compiled.warnings,
        )

    def preview(self, spec: QuerySpec, access: AccessContext) -> QueryResult:
        limit = min(spec.limit or PREVIEW_LIMITS.max_rows, PREVIEW_LIMITS.max_rows)
        return self.execute(spec.model_copy(update={"limit": limit}), access, PREVIEW_LIMITS)
