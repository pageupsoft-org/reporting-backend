import pytest

from reporting.core import ValidationFailed
from reporting.query import AggregateColumn, ExpressionColumn, parse_spec
from tests.spec_fixtures import example_spec


def test_example_spec_parses():
    spec = parse_spec(example_spec("conn_1"))
    assert isinstance(spec.columns[1], AggregateColumn)
    assert isinstance(spec.columns[4], ExpressionColumn)
    assert spec.columns[3].arg.kind == "date_diff"


@pytest.mark.parametrize("mutate,path,fragment", [
    (lambda s: s["columns"][1].update(fn="median"), "columns[1].fn", "avg"),
    (lambda s: s["columns"][0].update(kind="pivot"), "columns[0]", "kind"),
    (lambda s: s.update(extra_field=1), "extra_field", "Extra"),
    (lambda s: s["columns"][4].update(expr="x" * 501), "columns[4].expr", "500"),
    (lambda s: s.update(joins=[s["joins"][0]] * 9), "joins", "8"),
    (lambda s: s.update(columns=[]), "columns", "at least 1"),
    (lambda s: s["base"].update(alias="1bad"), "base.alias", "pattern"),
    (lambda s: s.update(limit=0), "limit", "greater"),
])
def test_shape_errors_have_json_paths(mutate, path, fragment):
    raw = example_spec("conn_1")
    mutate(raw)
    with pytest.raises(ValidationFailed) as exc:
        parse_spec(raw)
    matching = [i for i in exc.value.issues if i.path == path]
    assert matching, [(i.path, i.message) for i in exc.value.issues]
    assert fragment.lower() in matching[0].message.lower()


def test_too_many_columns():
    raw = example_spec("c")
    raw["columns"] = [{"id": f"c{i}", "kind": "field", "ref": "o.id"} for i in range(51)]
    with pytest.raises(ValidationFailed):
        parse_spec(raw)
