import pytest
from pydantic import ValidationError

from reporting.core import AccessContext, DataType, TableRef, ValidationFailed


def test_table_ref_alias_and_key():
    t = TableRef(connection_id="c", database="sales", schema="public", name="orders")
    assert t.key == "public.orders"
    assert t.model_dump(by_alias=True)["schema"] == "public"
    assert TableRef(connection_id="c", database="d", name="x").key == "x"
    with pytest.raises(ValidationError):
        t.name = "other"  # frozen


def test_datatype_traits():
    assert DataType.DECIMAL.is_numeric and not DataType.TEXT.is_numeric
    assert DataType.DATE.is_temporal and DataType.TEXT.is_orderable
    assert not DataType.JSON.is_orderable


def test_access_context():
    a = AccessContext("c", allowed_tables=frozenset({"public.orders"}),
                      hidden_columns=frozenset({"public.orders.secret"}))
    assert a.column_visible("public.orders", "id")
    assert not a.column_visible("public.orders", "secret")
    assert not a.table_visible("public.customers")
    assert AccessContext.unrestricted("c").column_visible("x", "y")


def test_validation_failed_default_issue():
    e = ValidationFailed("bad")
    assert e.issues[0].message == "bad"
