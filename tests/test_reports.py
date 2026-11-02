import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from reporting.core import Conflict, NotFound, PermissionDenied, ValidationFailed
from reporting.reports import (
    CreateEmbed,
    Element,
    EmbedOptions,
    ImageContent,
    Layout,
    ReportDocument,
    SaveReport,
    create_report_service,
)

KEY = "k" * 40


@pytest.fixture
def reports(app_db_url):
    return create_report_service(app_db_url, KEY)


def doc(*elements):
    return ReportDocument(elements=list(elements))


TITLE = Element(id="t1", type="text", layout=Layout(x=0, y=0, w=12, h=2),
                text={"content": "Sales", "variant": "title"})
CHART = Element(id="c1", type="chart", layout=Layout(x=0, y=2, w=6, h=8),
                binding={"query": {"connection_id": "x"}, "chart": {"type": "bar", "x": "a", "y": ["b"]}})


def test_document_validation():
    with pytest.raises(ValidationError, match="x \\+ w"):
        Layout(x=8, y=0, w=6, h=2)
    with pytest.raises(ValidationError, match="needs a data binding"):
        Element(id="e", type="table", layout=Layout(x=0, y=0, w=4, h=4))
    with pytest.raises(ValidationError, match="https"):
        ImageContent(url="javascript:alert(1)")
    with pytest.raises(ValidationError, match="unique"):
        doc(TITLE, TITLE)
    with pytest.raises(ValidationError, match="at least 30"):
        EmbedOptions(refresh_interval_s=5)
    assert ImageContent(url="data:image/png;base64,AAAA").fit == "contain"


def test_crud_and_optimistic_versioning(reports):
    r = reports.create("u1", SaveReport(name="Sales", kind="template", document=doc(TITLE)))
    assert r.version == 1 and r.document.elements[0].text.content == "Sales"
    r2 = reports.update(r.id, SaveReport(name="Sales v2", kind="template",
                                         document=doc(TITLE, CHART), version=1))
    assert r2.version == 2 and len(r2.document.elements) == 2
    with pytest.raises(Conflict) as exc:
        reports.update(r.id, SaveReport(name="stale", document=doc(), version=1))
    assert exc.value.details["current_version"] == 2
    assert [s.name for s in reports.list_documents(kind="template")] == ["Sales v2"]
    assert reports.list_documents(kind="report") == []
    copy = reports.create("u2", SaveReport(name="Mine", kind="report", document=r2.document),
                          source_id=r.id)
    assert copy.source_id == r.id and reports.list_documents(owner_id="u2")[0].element_count == 2
    reports.delete(r.id)
    with pytest.raises(NotFound):
        reports.get(r.id)


def test_embeds_public_and_signed(reports):
    r = reports.create("u1", SaveReport(name="R", document=doc(TITLE, CHART)))
    with pytest.raises(ValidationFailed):
        reports.create_embed(r.id, "u1", CreateEmbed(element_id="nope"))
    pub = reports.create_embed(r.id, "u1", CreateEmbed(element_id="c1", mode="public"))
    assert pub.id.startswith("emb_") and len(pub.id) > 20
    reports.verify_embed_token(pub, None)  # public: no token needed

    sig = reports.create_embed(r.id, "u1", CreateEmbed(
        element_id="c1", allowed_origins=["https://portal.example.com"]))
    secret = reports.embed_secret(sig)
    assert secret == reports.embed_secret(sig)  # derived, stable
    with pytest.raises(PermissionDenied, match="needs a signed token"):
        reports.verify_embed_token(sig, None)
    good = jwt.encode({"embed": sig.id, "exp": int(time.time()) + 600}, secret, algorithm="HS256")
    reports.verify_embed_token(sig, good)
    for bad, msg in [
        (jwt.encode({"embed": sig.id, "exp": int(time.time()) + 600}, "wrong" * 8, algorithm="HS256"), "invalid"),
        (jwt.encode({"embed": pub.id, "exp": int(time.time()) + 600}, secret, algorithm="HS256"), "different embed"),
        (jwt.encode({"embed": sig.id, "exp": int(time.time()) - 600}, secret, algorithm="HS256"), "expired"),
        (jwt.encode({"embed": sig.id, "exp": int(time.time()) + 3 * 86400}, secret, algorithm="HS256"), "24 hours"),
        (jwt.encode({"embed": sig.id}, secret, algorithm="HS256"), "invalid"),
    ]:
        with pytest.raises(PermissionDenied, match=msg):
            reports.verify_embed_token(sig, bad)

    rotated = reports.rotate_embed_secret(sig.id)
    assert reports.embed_secret(rotated) != secret
    with pytest.raises(PermissionDenied):
        reports.verify_embed_token(rotated, good)  # old tokens stop working

    for _ in range(5):  # repeated loads by one viewer count once, and are buffered
        reports.record_view(pub.id, "1.2.3.4")
    reports.record_view(pub.id, "5.6.7.8")
    assert reports.get_embed(pub.id).view_count == 0  # not written yet
    reports.flush_views()
    assert reports.get_embed(pub.id).view_count == 2
    revoked = reports.revoke_embed(pub.id)
    with pytest.raises(PermissionDenied, match="revoked"):
        reports.verify_embed_token(revoked, None)


def test_expiry_and_delete_revokes(reports):
    r = reports.create("u1", SaveReport(name="R", document=doc(CHART)))
    with pytest.raises(ValidationFailed):
        reports.create_embed(r.id, "u1", CreateEmbed(element_id="c1", mode="public",
                                                     expires_at=datetime.now(UTC) - timedelta(hours=1)))
    e = reports.create_embed(r.id, "u1", CreateEmbed(element_id="c1", mode="public"))
    reports.delete(r.id)
    assert reports.get_embed(e.id).revoked_at is not None
