"""A2.3 / A2.4 — the fields ledger at the python surfaces (CC-2).

The renderer itself is pinned in ``test_cc_fields_ledger.py``. What this file
pins is that the SURFACES reach it: ``adeu extract --mode fields``, its
``--fields-offset`` pagination, the ignored-flag warnings spec §1 requires, and
the appendix summary.

CLI invocations go through ``tests.utils.run_cli`` — mandatory repo-wide, and
enforced by ``test_cli_encoding.py``.
"""

import re

import pytest
from docx import Document

from adeu.domain import build_structural_appendix
from adeu.ingest import _extract_text_from_doc
from tests.cc_fixture import cc_fixture_bytes
from tests.utils import run_cli

PLAIN_BODY = "<w:p><w:r><w:t>Plain paragraph.</w:t></w:r></w:p>"


def _many(n: int) -> str:
    return "".join(
        f'<w:p><w:sdt><w:sdtPr><w:tag w:val="f{i}"/><w:text/></w:sdtPr>'
        f"<w:sdtContent><w:r><w:t>V{i}</w:t></w:r></w:sdtContent></w:sdt></w:p>"
        for i in range(1, n + 1)
    )


@pytest.fixture
def fixture_docx(tmp_path):
    path = tmp_path / "cc_fixture.docx"
    path.write_bytes(cc_fixture_bytes())
    return path


class TestCliFieldsMode:
    def test_renders_the_ledger(self, fixture_docx):
        proc = run_cli("extract", str(fixture_docx), "--mode", "fields")
        assert proc.returncode == 0, proc.stderr
        assert "# Fields: cc_fixture.docx" in proc.stdout
        assert "Protection: none \u00b7 16 content controls" in proc.stdout
        assert 'CC:3   text  "Counterparty" (tag: counterparty)' in proc.stdout

    def test_json_uses_the_established_cli_envelope(self, fixture_docx):
        """spec §1 says ``{"content": …}``; every CLI mode emits
        ``{"markdown", "title", "file_path"}``. Recorded as a deviation on the
        board — following the spec literally would make ``--mode fields`` the
        only mode with a different JSON shape, which is worse for the batch
        surfaces §7 says this format exists to serve."""
        import json

        proc = run_cli("extract", str(fixture_docx), "--mode", "fields", "--json")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert set(payload) == {"markdown", "title", "file_path"}
        assert payload["markdown"].startswith("# Fields:")

    def test_pagination_flag(self, tmp_path):
        path = tmp_path / "many.docx"
        path.write_bytes(cc_fixture_bytes(body_xml=_many(250)))

        proc = run_cli("extract", str(path), "--mode", "fields")
        assert proc.returncode == 0
        assert "\u2026 150 more \u2014 pass fields_offset=100 to continue." in proc.stdout

        proc = run_cli("extract", str(path), "--mode", "fields", "--fields-offset", "100")
        assert proc.returncode == 0
        assert re.search(r"^CC:101\s", proc.stdout, re.M)
        assert "\u2026 50 more \u2014 pass fields_offset=200 to continue." in proc.stdout

    def test_zero_control_document(self, tmp_path):
        path = tmp_path / "plain.docx"
        path.write_bytes(cc_fixture_bytes(body_xml=PLAIN_BODY))
        proc = run_cli("extract", str(path), "--mode", "fields")
        assert proc.returncode == 0
        assert "No content controls." in proc.stdout

    def test_protected_zero_control_document(self, tmp_path):
        path = tmp_path / "juries.docx"
        path.write_bytes(cc_fixture_bytes(protection="forms", body_xml=PLAIN_BODY))
        proc = run_cli("extract", str(path), "--mode", "fields")
        assert proc.returncode == 0
        assert "Protection: fill-in-forms only (enforced) \u00b7 no content controls" in proc.stdout
        assert "No content controls." in proc.stdout


class TestIgnoredFlagWarnings:
    """spec §1: `page` and `search_query` do not apply to this mode.

    Warn rather than silently ignore — the surrounding flags already behave
    this way, and a silently dropped `--page` looks like the ledger answered
    the question that was asked.
    """

    def test_page_warns_in_fields_mode(self, fixture_docx):
        proc = run_cli("extract", str(fixture_docx), "--mode", "fields", "--page", "2")
        assert proc.returncode == 0
        assert "--page is ignored with --mode fields" in proc.stderr
        assert "# Fields:" in proc.stdout

    def test_fields_offset_warns_outside_fields_mode(self, fixture_docx):
        proc = run_cli("extract", str(fixture_docx), "--mode", "full", "--fields-offset", "5")
        assert proc.returncode == 0
        assert "--fields-offset is ignored with --mode full" in proc.stderr

    def test_no_warning_when_flags_match_the_mode(self, fixture_docx):
        proc = run_cli("extract", str(fixture_docx), "--mode", "fields", "--fields-offset", "0")
        assert proc.returncode == 0
        assert "ignored" not in proc.stderr


class TestA24AppendixSummary:
    def _appendix(self, body_xml=None, protection=None):
        import io

        doc = Document(io.BytesIO(cc_fixture_bytes(protection=protection, body_xml=body_xml)))
        base = _extract_text_from_doc(doc, clean_view=False, include_appendix=False)
        if isinstance(base, tuple):
            base = base[0]
        return build_structural_appendix(doc, base)

    def test_summary_present_without_detail_lines(self):
        appendix = self._appendix()
        assert "## Content Controls" in appendix
        assert "Protection: none \u00b7 16 content controls \u2014 1 empty \u00b7 2 locked \u00b7 1 bound" in appendix
        assert 'Read with mode="fields" for the full field ledger.' in appendix
        # The bounded-appendix rule: FedRAMP rev4 would put 5,007 lines here.
        assert not re.search(r"^CC:\d", appendix, re.M)

    def test_absent_for_a_plain_document(self):
        assert "## Content Controls" not in self._appendix(body_xml=PLAIN_BODY)

    def test_present_for_protection_with_zero_controls(self):
        appendix = self._appendix(body_xml=PLAIN_BODY, protection="forms")
        assert "## Content Controls" in appendix
        assert "fill-in-forms only (enforced) \u00b7 no content controls" in appendix

    def test_cli_appendix_mode_shows_it(self, fixture_docx):
        proc = run_cli("extract", str(fixture_docx), "--mode", "appendix")
        assert proc.returncode == 0, proc.stderr
        assert "## Content Controls" in proc.stdout
        assert not re.search(r"^CC:\d", proc.stdout, re.M)
