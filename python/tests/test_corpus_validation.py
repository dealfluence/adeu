# FILE: tests/test_corpus_validation.py
"""A5 â€” corpus validation against real government documents (task CC-3).

Spec: specs/content-controls/spec-corpus.md Â·
Acceptance: specs/content-controls/acceptance/A5-corpus-validation.md

These run against real public-sector .docx/.dotx files that are deliberately
NOT committed. `corpus_path()` skips cleanly when a document is absent, so CI
is green without a single download; the optional corpus job fetches first.

**Scope of what is implemented here.** A5 says outright that "the ledger/anchor
assertions activate with CC-1/CC-2 (structure the tests so the pre-CC-1 subset
is green on CC-0 alone)". CC-1 has not landed, so there is no fields ledger, no
`{#cc:N}` anchors, no `set_field` and no gates to assert on. What ships now is
every A5 assertion that is real today:

* A5.9 â€” the fetch mechanism itself.
* A5.7 (partial) â€” the .dotx opens through the standard path.
* A5.8 (partial) â€” the negative `w:sdt` id survives a no-op round trip.
* A5.1 (partial) â€” the CC-0 repair holds at production scale, asserted in the
  DISCRIMINATING form: cell-level SDT content that is invisible without the fix.

The deferred assertions are listed in PROGRESS.md against the task that unblocks
each one. They are not stubbed here: a skipped test that can never run is
indistinguishable from a passing one at a glance, and this suite's whole purpose
is to not be vacuously green.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import zipfile

import pytest

from adeu.ingest import extract_text_from_stream
from tests.utils import CLI_OUTPUT_ENCODING, CORPUS_MANIFEST, corpus_path

REPO_ROOT = CORPUS_MANIFEST.parents[2]


def _project(data: bytes, clean_view: bool = True) -> str:
    return extract_text_from_stream(io.BytesIO(data), clean_view=clean_view, include_appendix=False)


# ---------------------------------------------------------------------------
# A5.9 â€” fetch mechanism smoke (no network)
# ---------------------------------------------------------------------------


def test_a5_9_fetch_corpus_list_reports_every_manifest_key():
    """`fetch_corpus.py --list` exits 0 and reports presence per manifest key.

    The one part of the corpus machinery that must work on a machine with no
    corpus and no network â€” it is how a developer finds out what to download.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "fetch_corpus.py"), "--list"],
        capture_output=True,
        # Explicit: the default decodes with the host ANSI code page and dies in
        # a reader thread on Windows (see run_cli's docstring).
        encoding=CLI_OUTPUT_ENCODING,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, result.stderr
    keys = json.loads(CORPUS_MANIFEST.read_text(encoding="utf-8"))["documents"]
    for key in keys:
        line = next((ln for ln in result.stdout.splitlines() if ln.startswith(key)), None)
        assert line is not None, f"--list omitted {key!r}:\n{result.stdout}"
        assert re.search(r"\b(present|missing)\b", line), f"no on-disk status for {key!r}: {line}"


def test_a5_9_fetch_corpus_rejects_an_unknown_key():
    """A typo must fail loudly, not fetch nothing and exit 0.

    `--only` naming a key that does not exist is the shape that would otherwise
    let the optional CI job "succeed" having downloaded nothing at all.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "fetch_corpus.py"),
            "--only",
            "no_such_document",
        ],
        capture_output=True,
        encoding=CLI_OUTPUT_ENCODING,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 2
    assert "unknown manifest key" in result.stderr
    assert "known keys:" in result.stderr, "the error must be self-service"


def test_corpus_path_raises_on_an_unknown_key():
    """The test helper draws the same distinction as the fetcher.

    Absent document â†’ skip; unknown key â†’ raise. A helper that skipped on both
    would turn every typo into a permanently green test.
    """
    with pytest.raises(KeyError, match="unknown corpus key"):
        corpus_path("no_such_document")


# ---------------------------------------------------------------------------
# A5.1 (partial) â€” CC-0 at production scale, in discriminating form
# ---------------------------------------------------------------------------


def _cell_level_sdt_texts(data: bytes) -> list[str]:
    """Every distinct text that lives inside a cell-level SDT (`sdtContent > w:tc`).

    Derived from the document rather than hardcoded: upstream revises these
    templates in place (spec-corpus Â§1), and a list of literal strings would rot
    into a skip-shaped failure. Each entry is a single `w:t` node's text, not a
    join of several â€” runs split at arbitrary points and the projection
    reassembles them with its own whitespace rules, so joined text is not a
    substring of the output even when nothing is wrong.
    """
    from lxml import etree

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        root = etree.fromstring(package.read("word/document.xml"))

    texts: set[str] = set()
    for sdt in root.iter(f"{W}sdt"):
        content = sdt.find(f"{W}sdtContent")
        if content is None:
            continue
        if not any(etree.QName(child).localname == "tc" for child in content):
            continue
        for node in content.iter(f"{W}t"):
            value = (node.text or "").strip()
            if len(value) >= 20:
                texts.add(value)
    return sorted(texts)


def test_a5_1_cell_level_sdt_content_is_visible_at_scale():
    """The FedRAMP SSP's 371 cell-level SDTs project their text.

    A0.5 asserts a 400,000-char floor on this document, and PROGRESS.md records
    that the floor does not discriminate: with row/cell descent disabled the
    same file still projects 490,345 chars, so that guard passes with the bug
    present. This one asserts on content reachable ONLY through a cell-level
    `sdtContent > w:tc`, so it fails the moment the CC-0 repair regresses.
    """
    data = corpus_path("fedramp_ssp_rev4").read_bytes()
    text = _project(data)

    assert len(text) > 400_000, f"clean view projected only {len(text):,} chars"

    cell_texts = _cell_level_sdt_texts(data)
    # ~95% of the 2026-08-21 scan's 371 cell-level controls, per spec-corpus Â§1.
    assert len(cell_texts) >= 20, f"fixture drifted: only {len(cell_texts)} cell-level texts"

    missing = [value for value in cell_texts if value not in text]
    assert not missing, (
        f"{len(missing)} of {len(cell_texts)} cell-level SDT texts are invisible in the "
        f"projection (CC-0 data loss): {missing[:5]}"
    )


def test_a5_1_no_raw_sdt_markup_leaks_into_the_projection():
    """Descending into `w:sdt` must not emit the wrapper itself.

    The failure mode opposite to CC-0: a traversal that "fixes" invisibility by
    stringifying the element would put OOXML in front of the model. Cheap to
    check, and it covers the whole corpus rather than one document.
    """
    text = _project(corpus_path("fedramp_ssp_rev4").read_bytes())

    for token in ("<w:sdt", "sdtContent", "w:sdtPr", "showingPlcHdr"):
        assert token not in text, f"raw OOXML {token!r} leaked into the text projection"


# ---------------------------------------------------------------------------
# A5.7 (partial) â€” .dotx through the standard path
# ---------------------------------------------------------------------------


def test_a5_7_the_fixture_really_is_a_template():
    """Guards the guard: A5.7 means nothing if the file stops being a .dotx."""
    with zipfile.ZipFile(io.BytesIO(corpus_path("odot_uic_drywell").read_bytes())) as package:
        content_types = package.read("[Content_Types].xml").decode("utf-8")
    assert "template.main+xml" in content_types


def test_a5_7_dotx_template_opens_through_the_standard_path():
    """A .dotx is an OPC package like any other; the engine must not sniff content types.

    The ledger/picture halves of A5.7 need CC-1. This is the half that is
    testable today. It failed until CC-11: `python-docx`'s `Document()` accepts
    exactly one main-part content type and raised `ValueError: ... is not a Word
    file` on `template.main+xml`, which the CLI surfaced as an unhandled
    traceback, while `@adeu/core` read the same file happily. `adeu.utils.opc`
    now registers the template and macro-enabled content types against
    `DocumentPart`, so the part keeps its own content type and a `.dotx` saves
    back as a `.dotx` — see `tests/test_opc_document_types.py` for the
    round-trip guard.
    """
    text = _project(corpus_path("odot_uic_drywell").read_bytes())
    assert text.strip(), "the .dotx projected nothing at all"
    assert "<w:sdt" not in text


# ---------------------------------------------------------------------------
# A5.8 (partial) â€” negative w:sdt id survives a round trip
# ---------------------------------------------------------------------------

_SDT_ID_RE = re.compile(r'<w:sdt>.*?<w:id w:val="(-?\d+)"', re.DOTALL)


def test_a5_8_negative_sdt_id_round_trips_untouched():
    """`w:sdt/w:id` is signed, and the wild contains negative values.

    AI_CONTEXT Â§8's ST_LongHexNumber lesson does NOT apply here â€” `w:id` on an
    sdt is `ST_DecimalNumber`, where a negative value is legal and Word keeps
    it. The risk is Adeu "helpfully" normalising it. Surgical mode must leave
    the bytes alone, so the id is asserted identical after a no-op openâ†’save.

    Note where it lives: the negative id in this document is in `word/footer1.xml`,
    not `word/document.xml`. Scanning only the main part finds nothing and the
    test passes vacuously â€” which is how this was first written.
    """
    data = corpus_path("hc_diagnostic_nonlab").read_bytes()

    def sdt_ids(package_bytes: bytes) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as package:
            for name in package.namelist():
                if not name.endswith(".xml"):
                    continue
                ids = _SDT_ID_RE.findall(package.read(name).decode("utf-8", "replace"))
                if ids:
                    found[name] = ids
        return found

    before = sdt_ids(data)
    negative = [(part, value) for part, ids in before.items() for value in ids if value.startswith("-")]
    assert negative, f"fixture no longer carries a negative sdt id (found {before})"

    from adeu.redline.engine import RedlineEngine

    saved = RedlineEngine(io.BytesIO(data)).save_to_stream().getvalue()

    assert sdt_ids(saved) == before, "a no-op save rewrote the sdt ids"


# ---------------------------------------------------------------------------
# CC-1c — checkbox census
#
# Engine-independent corpus facts, so no node twin: these read XML, not
# projections. They exist because spec-projection.md §4 makes assumptions about
# what checkboxes look like in the wild, and the wild is right here.
# ---------------------------------------------------------------------------

_W14_CHECKBOX = re.compile(r"<w14:checkbox\b.*?</w14:checkbox>", re.S)
_W14_CHECKED = re.compile(r'<w14:checked\s+w14:val="([^"]*)"')
_CHECKED_STATE = re.compile(r"<w14:checkedState\s+([^/>]*)/>")
_UNCHECKED_STATE = re.compile(r"<w14:uncheckedState\s+([^/>]*)/>")
_LEGACY_CHECKBOX = re.compile(r"<w:checkBox>.*?</w:checkBox>", re.S)
_SDT_ELEMENT = re.compile(r"<w:sdt>.*?</w:sdt>", re.S)

BALLOT_EMPTY = "\u2610"
BALLOT_X = "\u2612"

# key -> (w14:checkbox count, checked count). Every corpus document; the zeros
# are as load-bearing as the counts, since a document that GAINS checkboxes
# changes what §4 has to cope with.
CHECKBOX_CENSUS = {
    "ca_talent_recruitment": (0, 0),
    "dau_acquisition_plan": (0, 0),
    "fedramp_sar": (0, 0),
    "fedramp_ssp_appx_a_moderate": (3_804, 0),
    "fedramp_ssp_rev4": (3_881, 0),
    "fedramp_ssp_rev5": (3, 0),
    "hc_diagnostic_nonlab": (0, 0),
    "odot_uic_drywell": (19, 0),
    "on_juries_form1": (0, 0),
    "wawd_esi_agreement": (0, 0),
}


def _word_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return "\n".join(
            package.read(name).decode("utf-8", "replace")
            for name in package.namelist()
            if name.startswith("word/") and name.endswith(".xml")
        )


@pytest.mark.parametrize("key", sorted(CHECKBOX_CENSUS))
def test_cc1c_checkbox_census_is_pinned(key):
    """How many checkboxes each corpus document has, and how many are ticked."""
    data = corpus_path(key).read_bytes()
    xml = _word_xml(data)

    total = len(_W14_CHECKBOX.findall(xml))
    checked = sum(1 for value in _W14_CHECKED.findall(xml) if value in ("1", "true"))

    assert (total, checked) == CHECKBOX_CENSUS[key], (
        f"{key}: {total} checkboxes / {checked} checked, expected {CHECKBOX_CENSUS[key]}"
    )


def test_cc1c_the_corpus_contains_no_legacy_form_field_checkboxes():
    """`w14:checkbox` is the only checkbox mechanism here — the legacy one is absent.

    Word has two: the modern content control (`w14:checkbox`, Word 2010+) and the
    legacy form field (`w:fldChar` + `w:ffData/w:checkBox`, Word 97 era), which is
    NOT a content control and would not be reached by any of this initiative's
    traversal work. spec-projection.md §4 only describes the modern one.

    Across ~7,700 checkboxes in ten real government documents there is not one
    legacy field, which is what makes that scope choice safe rather than lucky.
    Pinned so that adding a corpus document containing legacy fields fails here
    and forces the scope question to be re-asked, instead of silently projecting
    nothing where a form has checkboxes.
    """
    legacy = {}
    for key in sorted(CHECKBOX_CENSUS):
        xml = _word_xml(corpus_path(key).read_bytes())
        found = len(_LEGACY_CHECKBOX.findall(xml))
        if found:
            legacy[key] = found

    assert legacy == {}, f"legacy w:checkBox form fields found, §4 does not cover these: {legacy}"


def test_cc1c_every_corpus_checkbox_uses_the_same_glyph_pair():
    """`MS Gothic` 2612/2610 throughout — the state glyphs are not per-document.

    `w14:checkedState`/`w14:uncheckedState` are free-form (font + code point), so
    a document may legitimately use Wingdings `F0FE`, a private-use code point
    whose meaning depends entirely on the font. That case would break any
    projection that recognises checkboxes by their character.

    It does not occur here: all four documents that have checkboxes use the same
    pair. This is the empirical half of why §4 projects from `w14:checked` rather
    than from the glyph — the glyph is *usually* recognisable, and "usually" is
    not a contract.
    """
    pairs = set()
    for key in sorted(k for k, (total, _) in CHECKBOX_CENSUS.items() if total):
        xml = _word_xml(corpus_path(key).read_bytes())
        pairs.update(_CHECKED_STATE.findall(xml))
        pairs.update(_UNCHECKED_STATE.findall(xml))

    fonts = {re.search(r'w14:font="([^"]*)"', p).group(1) for p in pairs if 'w14:font="' in p}
    vals = {re.search(r'w14:val="([^"]*)"', p).group(1) for p in pairs if 'w14:val="' in p}

    assert fonts == {"MS Gothic"}, f"unexpected checkbox state fonts: {fonts}"
    assert vals == {"2612", "2610"}, f"unexpected checkbox state code points: {vals}"


def test_cc1c_the_corpus_has_ballot_glyphs_that_are_not_checkboxes():
    """Two bare `☐` characters in `odot_uic_drywell`, outside every content control.

    Segoe UI Symbol runs in ordinary prose — a human typing a box rather than
    inserting a control. They are TEXT, and must survive projection as `☐`.

    This is the trap for CC-1c's implementation half. The obvious way to satisfy
    A1.8 ("the view contains `[x]`/`[ ]` and NO `☒`/`☐` characters") is to
    substitute on the character; do that and these two turn into `[ ]`, inventing
    two checkboxes that do not exist, in a document that has 19 real ones to hide
    among. The substitution has to be driven by the `w14:checkbox` control, and
    A1.8's "no glyphs" clause has to be read as scoped to control content.
    """
    xml = _word_xml(corpus_path("odot_uic_drywell").read_bytes())
    outside = _SDT_ELEMENT.sub("", xml)

    assert xml.count(BALLOT_EMPTY) == 21
    assert outside.count(BALLOT_EMPTY) == 2, "bare ballot glyphs outside any control"
    assert BALLOT_X not in xml, "no ticked glyph anywhere in the corpus"


def test_cc1c_no_corpus_checkbox_is_ticked():
    """Nothing in the corpus exercises the `[x]` half of A1.8.

    ~7,700 checkboxes, every one unchecked: these are blank templates, which is
    what public bodies publish. So the corpus can validate `[ ]` at scale and can
    say nothing at all about `[x]`, and a corpus-driven implementation would be
    half-tested while looking thoroughly exercised.

    The checked path is covered by synthetic fixtures and by the live-Word probes
    in `test_live_word_content_controls.py` instead. Recorded as a test so the
    gap is a stated fact rather than an omission nobody noticed.
    """
    ticked = {
        key: sum(1 for v in _W14_CHECKED.findall(_word_xml(corpus_path(key).read_bytes())) if v in ("1", "true"))
        for key in sorted(k for k, (total, _) in CHECKBOX_CENSUS.items() if total)
    }
    assert set(ticked.values()) == {0}, f"a corpus document now has ticked checkboxes: {ticked}"
