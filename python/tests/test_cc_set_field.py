"""CC-5 — `set_field` (A4).

Resolution and fill semantics for the `set_field` change type. The XML
assertions read the SAVED package, not the in-memory tree: the whole point of
a fill is what Word opens, and the two have diverged before.
"""

import pytest

from adeu.fields import FieldResolutionError, collect_fields, resolve_field
from tests.cc_fixture import cc_fixture_bytes


@pytest.fixture(scope="module")
def entries():
    import io

    from docx import Document

    from adeu.ingest import _extract_text_from_doc

    doc = Document(io.BytesIO(cc_fixture_bytes()))
    text = _extract_text_from_doc(doc, clean_view=False, include_appendix=False)
    if isinstance(text, tuple):
        text = text[0]
    return collect_fields(doc, text, None)


class TestA42Resolution:
    """A4.2 — field resolution order and ambiguity."""

    def test_resolves_by_cc_ordinal(self, entries):
        hits = resolve_field(entries, "CC:2")
        assert [e.ordinal for e in hits] == [2]

    def test_resolves_by_exact_tag(self, entries):
        tagged = next(e for e in entries if e.tag)
        hits = resolve_field(entries, tagged.tag)
        assert tagged.ordinal in [e.ordinal for e in hits]

    def test_resolves_by_exact_alias(self, entries):
        tags = {e.tag for e in entries if e.tag}
        aliased = next(e for e in entries if e.alias and e.alias not in tags)
        hits = resolve_field(entries, aliased.alias)
        assert [e.ordinal for e in hits] == [aliased.ordinal]

    def test_a_tag_beats_an_alias_of_the_same_string(self, entries):
        """Resolution order is tag before alias, so a string that is one
        control's tag and another's alias resolves to the tagged one."""
        from dataclasses import replace

        tagged = replace(entries[0], ordinal=201, tag="shared_name", alias=None)
        aliased = replace(entries[0], ordinal=202, tag=None, alias="shared_name")
        hits = resolve_field([aliased, tagged], "shared_name")
        assert [e.ordinal for e in hits] == [201]

    def test_ordinal_wins_over_a_tag_that_looks_like_one(self, entries):
        """A document may legally tag a control `CC:2`. The published id wins.

        Otherwise the addressing scheme this engine advertises could be
        shadowed by the document it is addressing.
        """
        from dataclasses import replace

        decoy = replace(entries[-1], tag="CC:2")
        hits = resolve_field(list(entries) + [decoy], "CC:2")
        assert [e.ordinal for e in hits] == [2]

    def test_matching_is_case_sensitive(self, entries):
        tagged = next(e for e in entries if e.tag and e.tag.lower() != e.tag.upper())
        with pytest.raises(FieldResolutionError):
            resolve_field(entries, tagged.tag.upper() if tagged.tag.islower() else tagged.tag.lower())

    def test_unresolvable_field_teaches_the_alternatives(self, entries):
        with pytest.raises(FieldResolutionError) as exc:
            resolve_field(entries, "nonexistent")
        msg = str(exc.value)
        assert "nonexistent" in msg
        assert "mode='fields'" in msg
        assert any(e.tag and e.tag in msg for e in entries)

    def test_unknown_ordinal_names_the_id(self, entries):
        with pytest.raises(FieldResolutionError) as exc:
            resolve_field(entries, "CC:9999")
        assert "CC:9999" in str(exc.value)

    def test_empty_field_is_a_clean_error_not_a_crash(self, entries):
        """Clients drop primitive `required[]` entries, so this arrives empty."""
        with pytest.raises(FieldResolutionError) as exc:
            resolve_field(entries, "")
        assert "requires 'field'" in str(exc.value)


class TestA42Ambiguity:
    """A4.2 — a tag shared by several controls, the repeating-section reality."""

    @pytest.fixture
    def dupes(self, entries):
        from dataclasses import replace

        a = replace(entries[0], ordinal=101, tag="item_name")
        b = replace(entries[0], ordinal=102, tag="item_name")
        return list(entries) + [a, b]

    def test_strict_rejects_listing_the_candidates(self, dupes):
        with pytest.raises(FieldResolutionError) as exc:
            resolve_field(dupes, "item_name")
        msg = str(exc.value)
        assert "CC:101" in msg and "CC:102" in msg
        assert "match_mode" in msg

    def test_first_takes_document_order(self, dupes):
        assert [e.ordinal for e in resolve_field(dupes, "item_name", "first")] == [101]

    def test_all_fans_out(self, dupes):
        assert [e.ordinal for e in resolve_field(dupes, "item_name", "all")] == [101, 102]


# ---------------------------------------------------------------------------
# A4.1 — the end-to-end fill
# ---------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _saved_sdt(raw_bytes, ordinal):
    """The `w:sdt` element with the given CC ordinal, read from SAVED bytes."""
    import io
    import zipfile

    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
        tree = etree.fromstring(z.read("word/document.xml"))
    sdts = tree.iter(f"{W}sdt")
    return list(sdts)[ordinal - 1]


def _fill(field, value, **kw):
    """Run one `set_field` through the real batch pipeline; return saved bytes."""
    import io

    from adeu.models import SetField
    from adeu.redline.engine import RedlineEngine

    engine = RedlineEngine(io.BytesIO(cc_fixture_bytes()), author="Test Author")
    result = engine.process_batch([SetField(field=field, value=value, **kw)])
    return engine.save_to_stream().getvalue(), result


class TestA41FillEmptyTextField:
    """A4.1 — fill an empty text field by tag, checked in the saved package."""

    @pytest.fixture(scope="class")
    @classmethod
    def filled(cls):
        return _fill("client_name", "Acme Legal Services Ltd.")

    def test_the_edit_applies(self, filled):
        _raw, result = filled
        assert result["edits_applied"] == 1, result.get("skipped_details")

    def test_showing_placeholder_is_gone(self, filled):
        raw, _ = filled
        sdt = _saved_sdt(raw, 2)
        assert sdt.find(f".//{W}showingPlcHdr") is None

    def test_no_run_keeps_the_placeholder_style(self, filled):
        """CC-6(a): Word's own fill carries no rStyle PlaceholderText at all."""
        raw, _ = filled
        sdt = _saved_sdt(raw, 2)
        styles = [s.get(f"{W}val") for s in sdt.iter(f"{W}rStyle")]
        assert "PlaceholderText" not in styles

    def test_the_ghost_run_left_without_a_deletion(self, filled):
        """CONFIRMED CC-6(a): filling an empty control makes ONE revision.

        A `w:del` here would strike through prompt text the author never
        wrote, which is worse than wrong - it is libellous to the document.
        """
        raw, _ = filled
        sdt = _saved_sdt(raw, 2)
        assert sdt.find(f".//{W}del") is None

    def test_the_value_lands_inside_a_tracked_insertion(self, filled):
        raw, _ = filled
        sdt = _saved_sdt(raw, 2)
        ins = sdt.findall(f".//{W}ins")
        assert ins, "no tracked insertion in the filled control"
        text = "".join(t.text or "" for i in ins for t in i.iter(f"{W}t"))
        assert text == "Acme Legal Services Ltd."

    def test_the_insertion_is_attributed_to_the_acting_author(self, filled):
        raw, _ = filled
        sdt = _saved_sdt(raw, 2)
        authors = {i.get(f"{W}author") for i in sdt.iter(f"{W}ins")}
        assert authors == {"Test Author"}

    def test_the_report_names_the_field(self, filled):
        """spec-fields-ledger §6 — audit-trail symmetry with heading_path."""
        _raw, result = filled
        rep = result["edits"][0]
        assert rep["field"] == 'CC:2 "Client Name" (tag: client_name)'

    def test_raw_view_shows_the_insertion_inside_the_anchor_pair(self, filled):
        import io

        raw, _ = filled
        from adeu.ingest import extract_text_from_stream

        text = extract_text_from_stream(io.BytesIO(raw), clean_view=False, include_appendix=False)
        if isinstance(text, tuple):
            text = text[0]
        assert "{#cc:2}{++Acme Legal Services Ltd.++}" in text

    def test_clean_view_shows_the_value_as_settled_text(self, filled):
        import io

        raw, _ = filled
        from adeu.ingest import extract_text_from_stream

        text = extract_text_from_stream(io.BytesIO(raw), clean_view=True, include_appendix=False)
        if isinstance(text, tuple):
            text = text[0]
        assert "{#cc:2}Acme Legal Services Ltd.{#/cc:2}" in text
