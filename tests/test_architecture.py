"""Architecture tests (plan §1.3): public contracts exchange only immutable data objects."""

import dataclasses
import importlib
import inspect
import typing
from typing import get_args, get_origin, get_type_hints

import pydantic
import pytest

MODULES = ["connections", "metadata", "query", "render", "auth", "reports"]
PROTOCOLS = {"connections": "ConnectionService", "metadata": "MetadataService",
             "query": "QueryService", "render": "RenderService", "auth": "AuthService",
             "reports": "ReportService"}
FORBIDDEN_PREFIXES = ("sqlalchemy", "psycopg", "pymysql", "pyodbc", "redis", "lark")
# pandas may cross a boundary only inside these named result containers.
PANDAS_ALLOWED_IN = {"ResultTable", "QueryResult"}


def _types(annotation):
    yield annotation
    for arg in get_args(annotation):
        yield from _types(arg)


def _module_of(t):
    origin = get_origin(t) or t
    return getattr(origin, "__module__", "") or ""


def _api(name):
    return importlib.import_module(f"reporting.{name}.api")


@pytest.mark.parametrize("name", MODULES)
def test_init_reexports_api_only(name):
    pkg = importlib.import_module(f"reporting.{name}")
    api = _api(name)
    assert set(api.__all__) <= set(dir(pkg))


@pytest.mark.parametrize("name", MODULES)
def test_protocol_signatures_use_no_driver_or_orm_types(name):
    api = _api(name)
    proto = getattr(api, PROTOCOLS[name])
    assert getattr(proto, "_is_protocol", False)
    ns = {**vars(api), **vars(importlib.import_module("reporting.core"))}
    for meth_name, meth in inspect.getmembers(proto, inspect.isfunction):
        if meth_name.startswith("_"):
            continue
        hints = get_type_hints(meth, globalns=ns)
        for param, ann in hints.items():
            for t in _types(ann):
                mod = _module_of(t)
                assert not mod.startswith(FORBIDDEN_PREFIXES), f"{proto.__name__}.{meth_name}:{param} uses {mod}"
                assert not mod.startswith("pandas"), f"{proto.__name__}.{meth_name}:{param} exposes pandas"


def _dto_classes(module):
    for obj in vars(module).values():
        if inspect.isclass(obj) and obj.__module__ == module.__name__:
            if issubclass(obj, pydantic.BaseModel) or dataclasses.is_dataclass(obj):
                yield obj


@pytest.mark.parametrize("name", [*MODULES, "core.types", "core.results"])
def test_dtos_are_frozen_and_driver_free(name):
    module = importlib.import_module(f"reporting.{name}" if "." in name else f"reporting.{name}.api")
    for cls in _dto_classes(module):
        if issubclass(cls, pydantic.BaseModel):
            assert cls.model_config.get("frozen"), f"{cls.__name__} is not frozen"
        else:
            assert cls.__dataclass_params__.frozen, f"{cls.__name__} is not frozen"
        hints = typing.get_type_hints(cls, globalns={**vars(module), **vars(
            importlib.import_module("reporting.core"))}, include_extras=False)
        for field, ann in hints.items():
            if field.startswith("_") or get_origin(ann) is typing.ClassVar:
                continue
            for t in _types(ann):
                mod = _module_of(t)
                assert not mod.startswith(FORBIDDEN_PREFIXES), f"{cls.__name__}.{field} uses {mod}"
                if mod.startswith("pandas"):
                    assert cls.__name__ in PANDAS_ALLOWED_IN, f"{cls.__name__}.{field} exposes pandas"


def test_errors_crossing_boundaries_are_core_errors():
    from reporting.core import errors
    names = {"NotFound", "ValidationFailed", "PermissionDenied", "ConnectionFailed", "QueryTimeout"}
    assert names <= set(vars(errors))
