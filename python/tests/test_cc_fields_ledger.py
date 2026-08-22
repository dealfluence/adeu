"""A2 — the fields ledger, at engine level (CC-2).

The line format is an output contract: spec-fields-ledger.md §7 tells callers
to parse it for cross-document search, so these compare against the frozen
GOLDEN-LEDGER character for character rather than asserting "contains".

Surface tests (MCP modes, CLI, pagination, appendix) live alongside their
surfaces; this file pins the renderer both surfaces share.
"""

import io
import re
from pathlib import Path

import pytest
from docx import Document

from adeu.fields import (
    collect_fields,
    read_document_protection,
    render_banner,
    render_ledger,
    render_line,
    summary_counts,
)
from adeu.ingest import _extract_text_from_doc
from tests.cc_fixture import cc_fixture_bytes

_FIXTURE_STANDARD = Path(__file__).resolve().parents[2] / "shared" / "fixtures" / "fixture-standard.md"


def golden(section: str) -> str:
    """Extract a fenced golden block from the frozen acceptance fixture doc."""
    md = _FIXTURE_STANDARD.read_text(encoding="utf-8")
    m = re.search(r"## " + re.escape(section) + r"[\s\S]*?\n```\n([\s\S]*?)```", md)
    assert m, f"golden section not found: {section}"
    return m.group(1).rstrip("\n")


def _load(protection=None, body_xml=None):
    raw = cc_fixture_bytes(protection=protection, body_xml=body_xml)
    doc = Document(io.BytesIO(raw))
    text = _extract_text_from_doc(doc, clean_view=False, include_appendix=False)
    if isinstance(text, tuple):
        text = text[0]
    return doc, text


def _ledger(protection=None, body_xml=None, offset=0, name="cc_fixture.docx"):
    doc, text = _load(protection, body_xml)
    entries = collect_fields(doc, text, None)
    return render_ledger(name, entries, read_document_protection(doc), offset=offset)


class TestA21LedgerGolden:
    def test_ledger_matches_golden_exactly(self):
        assert _ledger() == golden("GOLDEN-LEDGER")

    def test_banner_matches_golden_exactly(self):
        doc, text = _load()
        entries = collect_fields(doc, text, None)
        assert render_banner(entries, read_document_protection(doc)) == golden("GOLDEN-BANNER")

    def test_counts_are_the_documents_counts(self):
        doc, text = _load()
        # 16 controls, CC:2 empty, CC:7 content-locked + CC:8 group, CC:10 bound.
        assert summary_counts(collect_fields(doc, text, None)) == (16, 1, 2, 1)


class TestA22ProtectionLine:
    """A zero-control document still reports protection (Ontario Juries Form 1)."""

    EMPTY_BODY = "<w:p><w:r><w:t>Plain paragraph.</w:t></w:r></w:p>"

    def test_zero_controls_protected(self):
        out = _ledger(protection="forms", body_xml=self.EMPTY_BODY, name="juries.docx")
        assert out == (
            "# Fields: juries.docx\n"
            "Protection: fill-in-forms only (enforced) \u00b7 no content controls\n"
            "\n"
            "No content controls."
        )

    def test_zero_controls_unprotected_still_renders_a_header(self):
        out = _ledger(body_xml=self.EMPTY_BODY, name="plain.docx")
        assert out.splitlines()[1] == "Protection: none \u00b7 no content controls"

    def test_no_banner_for_a_plain_document(self):
        # spec-projection §7: zero controls AND no protection => no banner at
        # all. A plain document must gain zero noise from this feature.
        doc, text = _load(body_xml=self.EMPTY_BODY)
        entries = collect_fields(doc, text, None)
        assert render_banner(entries, read_document_protection(doc)) is None

    def test_banner_appears_for_protection_alone(self):
        doc, text = _load(protection="forms", body_xml=self.EMPTY_BODY)
        entries = collect_fields(doc, text, None)
        banner = render_banner(entries, read_document_protection(doc))
        assert banner == ("> **Protection:** fill-in-forms only (enforced) \u00b7 **Fields:** no content controls")

    @pytest.mark.parametrize(
        "edit,word",
        [
            ("readOnly", "read-only"),
            ("forms", "fill-in-forms only"),
            ("comments", "comments only"),
            ("trackedChanges", "tracked-changes only"),
        ],
    )
    def test_every_protection_mode_has_a_word(self, edit, word):
        """The parse is shared with CC-4's gate reader; only the WORDING is
        ours. `describe()` says "fill-in-forms, enforced" for gate errors,
        which A3.4 pins; the banner says "fill-in-forms only (enforced)"."""
        from adeu.fields import protection_label

        doc, _ = _load(protection=edit, body_xml=self.EMPTY_BODY)
        prot = read_document_protection(doc)
        assert prot.edit == edit
        assert protection_label(prot) == f"{word} (enforced)"


class TestA25AnonymousControls:
    """No fabricated names for controls that carry neither alias nor tag."""

    def test_anonymous_control_renders_bare(self):
        # CC:12 and CC:13 are the fixture's anonymous controls (repeating items).
        line = next(line for line in _ledger().splitlines() if line.startswith("CC:12 "))
        assert line == "CC:12  item \u2014 p1 \u2014 in CC:11 \u2014 wraps 1 block"

    def test_no_empty_quotes_or_empty_tag_anywhere(self):
        out = _ledger()
        assert '""' not in out
        assert "(tag: )" not in out


class TestLineFormat:
    """Segment-level pins for shapes the fixture exercises."""

    def test_ordinal_column_pads_to_the_widest(self):
        lines = [line for line in _ledger().splitlines() if line.startswith("CC:")]
        # "CC:16" is the widest, so every line's class word starts at column 7.
        assert {line.index(line.split()[1]) for line in lines} == {7}

    def test_states_precede_the_value(self):
        line = next(line for line in _ledger().splitlines() if line.startswith("CC:7 "))
        assert line.index("LOCKED (contents)") < line.index("value:")

    def test_group_reports_extent_not_value(self):
        line = next(line for line in _ledger().splitlines() if line.startswith("CC:8 "))
        assert line.endswith("wraps 2 blocks, 1 nested field")
        assert "value:" not in line

    def test_checkbox_reports_state_not_value(self):
        line = next(line for line in _ledger().splitlines() if line.startswith("CC:6 "))
        assert line.endswith("checked")
        assert "value:" not in line

    def test_bound_control_shows_its_xpath(self):
        line = next(line for line in _ledger().splitlines() if line.startswith("CC:10 "))
        assert "BOUND \u2192 /root[1]/matter[1]" in line

    def test_row_and_cell_controls_are_labelled(self):
        out = _ledger().splitlines()
        assert "table cell" in next(x for x in out if x.startswith("CC:14 "))
        assert "table row" in next(x for x in out if x.startswith("CC:15 "))

    def test_row_level_value_is_the_projected_row(self):
        # Proof the ledger reads the projection, not w:t: a row-level control's
        # value is the flattened markdown row, cell separator included.
        line = next(line for line in _ledger().splitlines() if line.startswith("CC:15 "))
        assert 'value: "Approver | Jane Roe"' in line

    def test_empty_control_shows_placeholder_not_value(self):
        line = next(line for line in _ledger().splitlines() if line.startswith("CC:2 "))
        assert "EMPTY" in line
        assert 'placeholder: "Click or tap here to enter text."' in line
        assert "value:" not in line


class TestPreviewCaps:
    """Long values truncate; the golden has no line at the cap."""

    def test_value_truncates_at_80_with_ellipsis(self):
        body = (
            '<w:p><w:sdt><w:sdtPr><w:tag w:val="long"/><w:text/></w:sdtPr>'
            "<w:sdtContent><w:r><w:t>" + ("A" * 200) + "</w:t></w:r>"
            "</w:sdtContent></w:sdt></w:p>"
        )
        line = [x for x in _ledger(body_xml=body).splitlines() if x.startswith("CC:1")][0]
        assert 'value: "' + "A" * 80 + '\u2026"' in line

    def test_options_cap_at_eight_with_overflow_marker(self):
        items = "".join(f'<w:listItem w:displayText="Opt{i}" w:value="{i}"/>' for i in range(1, 12))
        body = (
            '<w:p><w:sdt><w:sdtPr><w:tag w:val="dd"/><w:dropDownList>' + items + "</w:dropDownList></w:sdtPr>"
            "<w:sdtContent><w:r><w:t>Opt1</w:t></w:r></w:sdtContent></w:sdt></w:p>"
        )
        line = [x for x in _ledger(body_xml=body).splitlines() if x.startswith("CC:1")][0]
        assert "options: Opt1 | Opt2 | Opt3 | Opt4 | Opt5 | Opt6 | Opt7 | Opt8 | \u2026 (+3 more)" in line


class TestTemporary:
    """w:temporary was extracted nowhere before CC-2 (spec §3 segment 6)."""

    def test_temporary_renders_a_state_token(self):
        body = (
            '<w:p><w:sdt><w:sdtPr><w:tag w:val="tmp"/><w:temporary/><w:text/>'
            "</w:sdtPr><w:sdtContent><w:r><w:t>Draft</w:t></w:r>"
            "</w:sdtContent></w:sdt></w:p>"
        )
        line = [x for x in _ledger(body_xml=body).splitlines() if x.startswith("CC:1")][0]
        assert "TEMPORARY" in line

    def test_temporary_off_is_not_a_state(self):
        body = (
            '<w:p><w:sdt><w:sdtPr><w:tag w:val="tmp"/><w:temporary w:val="0"/>'
            "<w:text/></w:sdtPr><w:sdtContent><w:r><w:t>Draft</w:t></w:r>"
            "</w:sdtContent></w:sdt></w:p>"
        )
        line = [x for x in _ledger(body_xml=body).splitlines() if x.startswith("CC:1")][0]
        assert "TEMPORARY" not in line


class TestPagination:
    """Spec §4 — the 100-line cap and its continuation line."""

    @staticmethod
    def _many(n: int) -> str:
        return "".join(
            f'<w:p><w:sdt><w:sdtPr><w:tag w:val="f{i}"/><w:text/></w:sdtPr>'
            f"<w:sdtContent><w:r><w:t>V{i}</w:t></w:r></w:sdtContent></w:sdt></w:p>"
            for i in range(1, n + 1)
        )

    def test_first_page_caps_and_points_forward(self):
        out = _ledger(body_xml=self._many(250)).splitlines()
        cc = [x for x in out if x.startswith("CC:")]
        assert len(cc) == 100
        assert cc[0].startswith("CC:1 ") and cc[-1].startswith("CC:100 ")
        assert out[-1] == "\u2026 150 more \u2014 pass fields_offset=100 to continue."

    def test_middle_page(self):
        out = _ledger(body_xml=self._many(250), offset=100).splitlines()
        cc = [x for x in out if x.startswith("CC:")]
        assert cc[0].startswith("CC:101 ") and cc[-1].startswith("CC:200 ")
        assert out[-1] == "\u2026 50 more \u2014 pass fields_offset=200 to continue."

    def test_last_page_has_no_continuation(self):
        out = _ledger(body_xml=self._many(250), offset=200).splitlines()
        cc = [x for x in out if x.startswith("CC:")]
        assert len(cc) == 50
        assert cc[-1].startswith("CC:250 ")
        assert "more \u2014 pass fields_offset" not in out[-1]

    def test_header_counts_are_document_wide_not_page_wide(self):
        # A paginated ledger still describes the whole document; reporting 100
        # would make the count depend on where the reader happened to be.
        out = _ledger(body_xml=self._many(250), offset=100)
        assert "250 content controls" in out.splitlines()[1]


class TestHeadingIndexEquivalence:
    """The ledger's fast heading index must match the function it replaced.

    `heading_path_at` re-splits the whole projection per call, which made the
    ledger quadratic: 8.8 seconds on FedRAMP rev4 (5,007 controls), twenty times
    the cost of the entire projection. `_HeadingIndex` precomputes each
    breadcrumb once and binary-searches — 115ms — but a faster answer is only
    worth having if it is the SAME answer.
    """

    DOC = "\n".join(
        [
            "# Master Services Agreement",
            "Intro prose.",
            "## Definitions",
            "Term text here.",
            "### Sub-definition",
            "Deep text.",
            "## Payment",
            "Pay text.",
            "# Schedule A",
            "Schedule text.",
            "Trailing prose with no heading after it.",
        ]
    )

    def test_agrees_at_every_offset(self):
        from adeu.fields import _HeadingIndex
        from adeu.outline import heading_path_at

        index = _HeadingIndex(self.DOC)
        for offset in range(len(self.DOC) + 1):
            assert index.path_at(offset) == heading_path_at(offset, self.DOC), f"diverged at offset {offset}"

    def test_agrees_on_text_with_no_headings(self):
        from adeu.fields import _HeadingIndex
        from adeu.outline import heading_path_at

        plain = "Just prose.\n\nMore prose."
        index = _HeadingIndex(plain)
        for offset in range(len(plain) + 1):
            assert index.path_at(offset) == heading_path_at(offset, plain)

    def test_agrees_on_a_real_corpus_document(self):
        from tests.utils import corpus_path

        path = corpus_path("fedramp_ssp_rev4")
        if path is None:
            pytest.skip("corpus not fetched")

        from docx import Document as _D

        from adeu.fields import _HeadingIndex
        from adeu.outline import heading_path_at

        doc = _D(str(path))
        text = _extract_text_from_doc(doc, clean_view=False, include_appendix=False)
        if isinstance(text, tuple):
            text = text[0]
        index = _HeadingIndex(text)
        # Every anchor offset in the document, which is exactly the set of
        # offsets the ledger will ask about.
        for m in re.finditer(r"\{#cc:\d+[^}]*\}", text):
            assert index.path_at(m.start()) == heading_path_at(m.start(), text)

    def test_ledger_renders_breadcrumbs_on_a_real_document(self):
        """The minimal fixture package has no styles.xml, so `Heading1` never
        resolves and it can never produce a breadcrumb. Use a real document."""
        from tests.utils import corpus_path

        path = corpus_path("fedramp_ssp_rev4")
        if path is None:
            pytest.skip("corpus not fetched")

        from docx import Document as _D

        doc = _D(str(path))
        text = _extract_text_from_doc(doc, clean_view=False, include_appendix=False)
        if isinstance(text, tuple):
            text = text[0]
        entries = collect_fields(doc, text, None)
        with_crumbs = [e for e in entries if e.heading_path]
        assert with_crumbs, "no control resolved a heading path in a headed document"
        line = render_line(with_crumbs[0], 6)
        assert f"p{with_crumbs[0].page} \u00b7 {with_crumbs[0].heading_path}" in line


class TestA26PerEditReportsNameTheField:
    """spec-fields-ledger §6 — audit-trail symmetry with `heading_path`."""

    @staticmethod
    def _report(target, new_text):
        from adeu.models import ModifyText
        from adeu.redline.engine import RedlineEngine

        engine = RedlineEngine(io.BytesIO(cc_fixture_bytes()), author="Tester")
        stats = engine.process_batch([ModifyText(type="modify", target_text=target, new_text=new_text)])
        return stats["edits"][0]

    def test_edit_inside_a_control_names_it(self):
        report = self._report("ACME Corp", "Acme Corporation")
        assert report["status"] == "applied"
        assert report["field"] == 'CC:3 "Counterparty" (tag: counterparty)'

    def test_edit_outside_any_control_names_nothing(self):
        report = self._report("Signed by the parties below.", "Signed below.")
        assert report["status"] == "applied"
        assert report["field"] == ""

    def test_nested_control_reports_the_innermost(self):
        # CC:9 sits inside the group CC:8. The specific answer is the useful
        # one: CC:9's own lock and binding govern the edit.
        report = self._report("123 Main Street, Ottawa", "1 Queen Street, Ottawa")
        assert report["status"] == "applied"
        assert report["field"] == 'CC:9 "Notice Address" (tag: notice_address)'

    def test_anonymous_control_reports_its_tag_only(self):
        report = self._report("Approved without conditions.", "Approved.")
        assert report["status"] == "applied"
        assert report["field"] == "CC:16 (tag: cell_notes)"


class TestSummaryAgreesWithLedger:
    """The cheap banner counts must equal the ledger's own counts.

    `field_summary` walks the DOM only — no projection, no value previews, no
    breadcrumbs — because the banner and the appendix render four numbers and
    computing the whole ledger for them cost 115ms per read on a control-heavy
    document. Two ways of counting is two chances to disagree, and a banner
    that contradicts the ledger it advertises is worse than no banner, so the
    agreement is pinned rather than assumed.
    """

    def test_agrees_on_the_standard_fixture(self):
        from adeu.fields import field_summary

        doc, text = _load()
        assert field_summary(doc) == summary_counts(collect_fields(doc, text, None))

    @pytest.mark.parametrize(
        "body",
        [
            "<w:p><w:r><w:t>No controls at all.</w:t></w:r></w:p>",
            (
                '<w:p><w:sdt><w:sdtPr><w:tag w:val="e"/><w:text/></w:sdtPr>'
                "<w:sdtContent><w:r><w:t></w:t></w:r></w:sdtContent></w:sdt></w:p>"
            ),
            (
                '<w:p><w:sdt><w:sdtPr><w:tag w:val="f"/><w:text/></w:sdtPr>'
                "<w:sdtContent><w:r><w:t>Filled</w:t></w:r></w:sdtContent></w:sdt></w:p>"
            ),
        ],
    )
    def test_agrees_on_edge_shapes(self, body):
        from adeu.fields import field_summary

        doc, text = _load(body_xml=body)
        assert field_summary(doc) == summary_counts(collect_fields(doc, text, None))

    def test_agrees_on_a_real_corpus_document(self):
        from tests.utils import corpus_path

        path = corpus_path("odot_uic_drywell")
        if path is None:
            pytest.skip("corpus not fetched")

        # load_document, not docx.Document: this corpus entry is a .dotx and
        # python-docx refuses the template content type outright (CC-11).
        from adeu.fields import field_summary
        from adeu.utils.opc import load_document

        doc = load_document(str(path))
        text = _extract_text_from_doc(doc, clean_view=False, include_appendix=False)
        if isinstance(text, tuple):
            text = text[0]
        assert field_summary(doc) == summary_counts(collect_fields(doc, text, None))

    def test_banner_matches_the_golden_via_the_cheap_path(self):
        from adeu.fields import banner_for_document

        doc, _ = _load()
        assert banner_for_document(doc) == golden("GOLDEN-BANNER")
