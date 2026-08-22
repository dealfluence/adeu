# FILE: tests/test_live_word_content_controls.py
"""Measurements of real Word behavior on content controls.

These tests pin real Word behavior on content controls: every claim in gate
and set-field handling is confirmed or amended here, and pinned so a future
Word build that changes its mind fails the build.

The short version of measured behaviors:

* (d) Word DOES allow Accept/Reject of a revision inside `sdtContentLocked`.
  Locking content stops *typing*, not *review*. G9 downgrades to allow.
* (e) Rejecting a tracked edit of a data-bound control RESYNCS the store back —
  Word does not leave it holding the new value.
* (f) Word re-instates the placeholder as soon as the emptying is real (untracked
  delete, or accepting a tracked delete). It withholds it only while a deletion
  is still pending.

All of these drive a real Word instance and are skipped off Windows. conftest
auto-marks `test_live_word*` into the `live_word` xdist group so they never run
concurrently with each other — two of them editing at once corrupts the shared
application state.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from tests.sdt_fixtures import build_sdt_docx, custom_xml, document_xml, para, run

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Live Word COM tests require Windows platform",
)

if sys.platform == "win32":
    from tests.word_com import edit_and_save

STORE_ID = "{A1B2C3D4-0000-0000-0000-000000000001}"

WD_REVISION_INSERT = 1
WD_REVISION_DELETE = 2


def _content_revisions(document) -> int:
    """Insertions and deletions only — never `Document.Revisions.Count`.

    The raw count also includes format revisions (`wdRevisionProperty`,
    `w:rPrChange`), which Word manufactures on its own initiative: with tracking
    on it re-stamps things like the proofing language and bills the edit to the
    current author. `sdt_fixtures` pins `w:lang` so it has no excuse, but
    counting only what the test actually caused keeps these assertions from
    being hostage to Word's housekeeping.
    """
    return sum(
        1
        for i in range(1, document.Revisions.Count + 1)
        if document.Revisions(i).Type in (WD_REVISION_INSERT, WD_REVISION_DELETE)
    )


def _refusal(action) -> str | None:
    """`None` when Word allowed `action`, else the message it refused with."""
    try:
        action()
    except Exception as exc:  # pywintypes.com_error
        return str(exc)
    return None


def _sdt_of(xml: str, tag: str) -> str:
    """The `w:sdt` element carrying `w:tag w:val="tag"`, as raw XML."""
    marker = f'<w:tag w:val="{tag}"/>'
    at = xml.find(marker)
    assert at != -1, f"no control tagged {tag!r} in\n{xml}"
    start = xml.rfind("<w:sdt>", 0, at)
    end = xml.find("</w:sdt>", at)
    assert start != -1 and end != -1, f"unbalanced sdt around {tag!r}"
    return xml[start : end + len("</w:sdt>")]


# --------------------------------------------------------------------------
# (a) placeholder fill under track changes
# --------------------------------------------------------------------------


def test_placeholder_fill_drops_the_ghost_untracked_and_inserts_tracked(word_app, tmp_path: Path):
    """CC-6(a): Word does NOT redline the disappearance of placeholder text.

    Confirms spec-set-field.md §4.2. Filling an empty control produces exactly
    one revision — the insertion. The ghost run and `w:showingPlcHdr` simply
    vanish, and the inserted run does NOT inherit `rStyle PlaceholderText`,
    which is why §4.3 has to strip it when Adeu does the same job.
    """
    body = para("Probe.") + (
        "<w:p>"
        + run("This Agreement is made between ")
        + '<w:sdt><w:sdtPr><w:tag w:val="client_name"/><w:id w:val="102"/>'
        "<w:showingPlcHdr/><w:text/></w:sdtPr><w:sdtContent>"
        '<w:r><w:rPr><w:rStyle w:val="PlaceholderText"/></w:rPr>'
        "<w:t>Click or tap here to enter text.</w:t></w:r>"
        "</w:sdtContent></w:sdt>" + run(" and the Government of Example.") + "</w:p>"
    )
    source = build_sdt_docx(tmp_path / "fill_src.docx", body)

    def fill(document):
        document.ContentControls(1).Range.Text = "Acme Legal Services Ltd."
        return _content_revisions(document)

    revisions = edit_and_save(word_app, source, tmp_path / "filled.docx", fill, track=True)

    assert revisions == 1, "the ghost removal must not add a second revision"
    sdt = _sdt_of(document_xml(tmp_path / "filled.docx"), "client_name")
    assert "<w:showingPlcHdr/>" not in sdt
    assert "Click or tap here to enter text." not in sdt
    assert "<w:del " not in sdt, "Word does not redline placeholder removal"
    assert "<w:ins " in sdt and "Acme Legal Services Ltd." in sdt
    assert "PlaceholderText" not in sdt, "the fill must not inherit the ghost's style"


# --------------------------------------------------------------------------
# (b) checkbox toggle redline shape
# --------------------------------------------------------------------------


def _checkbox(tag: str, sdt_id: int, checked: str, glyph: str) -> str:
    return (
        "<w:p>" + run(f"{tag}: ") + f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="{sdt_id}"/>'
        f'<w14:checkbox><w14:checked w14:val="{checked}"/>'
        '<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
        '<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/></w14:checkbox></w:sdtPr>'
        '<w:sdtContent><w:r><w:rPr><w:rFonts w:ascii="MS Gothic" w:eastAsia="MS Gothic" '
        f'w:hAnsi="MS Gothic"/></w:rPr><w:t>{glyph}</w:t></w:r></w:sdtContent></w:sdt>'
        "</w:p>"
    )


def test_checkbox_toggle_is_insert_then_delete_with_a_silent_state_attribute(word_app, tmp_path: Path):
    """CC-6(b): the toggle is a glyph del+ins pair; `w14:checked` is NOT tracked.

    Amends spec-set-field.md §5 in one detail: the spec says "ONE tracked
    del+ins"; Word emits the `w:ins` FIRST and the `w:del` after it. Order
    matters to Adeu because the projection reads document order — writing
    del-then-ins would render `{--☒--}{++☐++}` where Word renders
    `{++☐++}{--☒--}`.

    The `w14:checked` attribute flips with no revision of its own: it is an
    attribute sync, the same class as the URL_RETARGET precedent.
    """
    body = para("Probe.") + _checkbox("confidential", 106, "1", "\u2612") + _checkbox("waiver", 116, "0", "\u2610")
    source = build_sdt_docx(tmp_path / "cb_src.docx", body)

    def toggle(document):
        document.ContentControls(1).Checked = False
        document.ContentControls(2).Checked = True
        return _content_revisions(document)

    revisions = edit_and_save(word_app, source, tmp_path / "cb.docx", toggle, track=True)
    assert revisions == 4, "two toggles, each an insert and a delete"

    xml = document_xml(tmp_path / "cb.docx")

    unchecked = _sdt_of(xml, "confidential")
    assert '<w14:checked w14:val="0"/>' in unchecked, "state attribute follows the toggle"
    assert unchecked.index("<w:ins ") < unchecked.index("<w:del "), "Word inserts before deleting"
    assert "\u2610" in unchecked[unchecked.index("<w:ins ") : unchecked.index("<w:del ")]
    assert "<w:delText>\u2612</w:delText>" in unchecked

    checked = _sdt_of(xml, "waiver")
    assert '<w14:checked w14:val="1"/>' in checked
    assert checked.index("<w:ins ") < checked.index("<w:del ")
    assert "<w:delText>\u2610</w:delText>" in checked


# --------------------------------------------------------------------------
# (c) w:temporary unwrap
# --------------------------------------------------------------------------


def _temporary(tag: str, sdt_id: int, content: str, showing: bool) -> str:
    plc = "<w:showingPlcHdr/>" if showing else ""
    return (
        "<w:p>" + run(f"{tag}: ") + f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="{sdt_id}"/>'
        f"<w:temporary/>{plc}<w:text/></w:sdtPr><w:sdtContent>{content}</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
    )


@pytest.mark.parametrize("track", [True, False], ids=["tracked", "untracked"])
def test_temporary_control_unwraps_on_any_content_edit(word_app, tmp_path: Path, track: bool):
    """CC-6(c): `w:temporary` unwraps the moment content is edited — either way.

    Amends spec-set-field.md §4.4, which ties the unwrap to *the fill* of a
    placeholder. Word unwraps on ANY content edit, tracked or not, and whether
    or not the control was showing a placeholder. The revision survives the
    unwrap and sits in the bare paragraph, so rejecting it restores the text but
    NOT the control: the unwrap is not itself undoable.
    """
    ghost = '<w:r><w:rPr><w:rStyle w:val="PlaceholderText"/></w:rPr><w:t>Enter something.</w:t></w:r>'
    body = para("Probe.") + _temporary("temp_field", 120, ghost, showing=True)
    source = build_sdt_docx(tmp_path / f"tmp_{track}_src.docx", body)

    def fill(document):
        document.ContentControls(1).Range.Text = "Filled temp"
        return document.ContentControls.Count

    remaining = edit_and_save(word_app, source, tmp_path / f"tmp_{track}.docx", fill, track=track)

    assert remaining == 0, "Word removed the control as soon as it was filled"
    xml = document_xml(tmp_path / f"tmp_{track}.docx")
    assert "<w:sdt>" not in xml and "<w:temporary/>" not in xml
    assert "Filled temp" in xml
    assert ("<w:ins " in xml) is track


def test_temporary_control_survives_an_untouched_round_trip(word_app, tmp_path: Path):
    """CC-6(c): opening and saving is not an edit — the wrapper stays.

    The complement of the test above, and the reason Adeu may not "helpfully"
    unwrap temporary controls at ingest: an untouched one is still a control to
    Word, so it is still a control in the projection.
    """
    body = para("Probe.") + _temporary("temp_filled", 121, run("old value"), showing=False)
    source = build_sdt_docx(tmp_path / "tmp_keep_src.docx", body)

    count = edit_and_save(
        word_app,
        source,
        tmp_path / "tmp_keep.docx",
        lambda document: document.ContentControls.Count,
        track=True,
    )

    assert count == 1
    xml = document_xml(tmp_path / "tmp_keep.docx")
    assert "<w:temporary/>" in xml
    assert _sdt_of(xml, "temp_filled").count("old value") == 1


# --------------------------------------------------------------------------
# (d) locks, review actions and document protection
# --------------------------------------------------------------------------


def _lockable(tag: str, sdt_id: int, lock: str | None) -> str:
    lock_xml = f'<w:lock w:val="{lock}"/>' if lock else ""
    prior_ins = (
        '<w:ins w:id="900" w:author="Prior Author" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>tracked insert</w:t></w:r></w:ins>"
    )
    return (
        "<w:p>" + run(f"{tag}: ") + f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="{sdt_id}"/>{lock_xml}'
        "<w:text/></w:sdtPr><w:sdtContent>" + run("kept ") + prior_ins + "</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
    )


LOCK_BODY = (
    para("Probe.")
    + _lockable("contentlocked", 301, "sdtContentLocked")
    + _lockable("sdtlocked", 302, "sdtLocked")
    + _lockable("unlocked", 303, None)
)


def test_word_allows_review_actions_inside_a_content_locked_control(word_app, tmp_path: Path):
    """CC-6(d): resolves spec-gates.md G9 — locks do not gate Accept/Reject.

    `sdtContentLocked` refuses typing ("You are not allowed to edit this
    selection because it is protected") but Word's review engine accepts and
    rejects revisions inside it without complaint. Per G9's own instruction,
    that downgrades the gate to *allow*: refusing review actions Adeu-side would
    make Adeu stricter than Word for no protective benefit, and would strand
    revisions that a user can resolve by hand in two clicks.

    Also pins Word's lock mapping, which is not one-to-one with the XML:
    `sdtContentLocked` sets BOTH `LockContents` and `LockContentControl`, while
    `sdtLocked` sets only `LockContentControl` and leaves content editable.
    """
    source = build_sdt_docx(tmp_path / "lock_src.docx", LOCK_BODY)

    def probe(document):
        locks = {
            document.ContentControls(i).Tag: (
                document.ContentControls(i).LockContents,
                document.ContentControls(i).LockContentControl,
            )
            for i in range(1, document.ContentControls.Count + 1)
        }
        accepts, types = {}, {}
        for i in range(1, document.ContentControls.Count + 1):
            control = document.ContentControls(i)
            revisions = control.Range.Revisions
            accepts[control.Tag] = _refusal(lambda r=revisions: r.Item(1).Accept())
        for i in range(1, document.ContentControls.Count + 1):
            control = document.ContentControls(i)
            types[control.Tag] = _refusal(lambda c=control: setattr(c.Range, "Text", "typed"))
        return locks, accepts, types

    locks, accepts, types = edit_and_save(word_app, source, tmp_path / "lock.docx", probe, track=True)

    assert locks == {
        "contentlocked": (True, True),
        "sdtlocked": (False, True),
        "unlocked": (False, False),
    }
    assert accepts == {"contentlocked": None, "sdtlocked": None, "unlocked": None}, (
        f"Word permits review actions inside locked controls: {accepts}"
    )
    assert types["contentlocked"] is not None and "protected" in types["contentlocked"]
    assert types["sdtlocked"] is None, "sdtLocked protects the control, not its contents"
    assert types["unlocked"] is None


def test_tracked_changes_protection_blocks_every_review_action(word_app, tmp_path: Path):
    """CC-6(d): `w:documentProtection edit="trackedChanges"` gates review, not editing.

    The mirror image of the lock result, and the case spec-gates.md G9 should
    actually be worried about. Editing stays allowed (and is forcibly tracked);
    Accept and Reject both fail with "This command is not available" — document
    wide, not just inside controls. Adeu's review actions have to refuse here or
    they will report success for changes that never resolved.
    """
    source = build_sdt_docx(tmp_path / "prot_src.docx", LOCK_BODY, protection="trackedChanges")

    def probe(document):
        before = _content_revisions(document)
        control = document.ContentControls(1)
        accept = _refusal(lambda: control.Range.Revisions.Item(1).Accept())
        reject = _refusal(lambda: control.Range.Revisions.Item(1).Reject())
        after = control.Range.Revisions.Count
        edit = _refusal(lambda: setattr(document.ContentControls(3).Range, "Text", "typed"))
        return accept, reject, edit, before, after

    accept, reject, edit, before, unresolved = edit_and_save(
        word_app, source, tmp_path / "prot.docx", probe, track=True
    )

    assert accept is not None and "not available" in accept
    assert reject is not None and "not available" in reject
    assert edit is None, "trackedChanges protection permits tracked editing"
    assert before == 3
    assert unresolved == 1, "the refused Accept/Reject left the revision in place"
    # Word renumbers every w:id on save, so the surviving revision is identified
    # by its author, not by the id the fixture gave it.
    assert 'w:author="Prior Author"' in _sdt_of(document_xml(tmp_path / "prot.docx"), "contentlocked")


def test_forms_protection_hides_track_revisions_and_writes_untracked(word_app, tmp_path: Path):
    """CC-6(d): under forms protection Word will not even TELL you about tracking.

    Reading `Document.TrackRevisions` throws, not just assigning it — the reason
    `edit_and_save` takes `track=None`. Fills of unlocked controls are permitted
    and are written UNTRACKED; locked controls still refuse. That is the shape
    spec-gates.md G5 has to match: forms protection is not "read only", it is
    "fields only, and your redlines are not welcome".
    """
    source = build_sdt_docx(tmp_path / "forms_src.docx", LOCK_BODY, protection="forms")

    def probe(document):
        return (
            document.ProtectionType,
            _refusal(lambda: document.TrackRevisions),
            _refusal(lambda: setattr(document.ContentControls(1).Range, "Text", "typed")),
            _refusal(lambda: setattr(document.ContentControls(3).Range, "Text", "typed")),
        )

    protection, reading, locked, unlocked = edit_and_save(word_app, source, tmp_path / "forms.docx", probe, track=None)

    assert protection == 2, "wdAllowOnlyFormFields"
    assert reading is not None and "TrackRevisions" in reading
    assert locked is not None and "protected" in locked
    assert unlocked is None

    filled = _sdt_of(document_xml(tmp_path / "forms.docx"), "unlocked")
    assert "typed" in filled
    assert "<w:ins " not in filled, "forms protection suppresses tracking of the fill"


# --------------------------------------------------------------------------
# (e) data-bound controls
# --------------------------------------------------------------------------


def _bound(content: str) -> str:
    return para("Probe.") + (
        "<w:p>" + run("Matter number: ") + '<w:sdt><w:sdtPr><w:tag w:val="matter_number"/><w:id w:val="110"/>'
        f'<w:dataBinding w:xpath="/root[1]/matter[1]" w:storeItemID="{STORE_ID}"/>'
        "<w:text/></w:sdtPr><w:sdtContent>" + run(content) + "</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
    )


def _store(value: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><root><matter>{value}</matter></root>'


def test_bound_store_overwrites_control_content_on_open(word_app, tmp_path: Path):
    """CC-6(e): the STORE wins. This is why §6's dual-write is mandatory.

    When `sdtContent` and the bound CustomXML node disagree, Word rewrites the
    content from the store on load — silently, with no revision. A tracked edit
    written to the content alone would therefore be *destroyed the next time the
    document is opened*, not merely inconsistent. Content-only writing to a
    bound control is data loss with extra steps.
    """
    source = build_sdt_docx(
        tmp_path / "bound_src.docx",
        _bound("M-CONTENT-VALUE"),
        custom_xml=_store("M-STORE-VALUE"),
        store_item_id=STORE_ID,
    )

    text = edit_and_save(
        word_app,
        source,
        tmp_path / "bound.docx",
        lambda document: document.ContentControls(1).Range.Text,
        track=False,
    )

    assert text == "M-STORE-VALUE"
    assert "M-STORE-VALUE" in _sdt_of(document_xml(tmp_path / "bound.docx"), "matter_number")
    assert "M-CONTENT-VALUE" not in document_xml(tmp_path / "bound.docx")


def test_word_dual_writes_the_bound_store_on_a_tracked_edit(word_app, tmp_path: Path):
    """CC-6(e): confirms spec-set-field.md §6.1/§6.2 exactly.

    Word puts a normal `w:ins`/`w:del` pair in the content AND pushes the new
    value straight into the store with no redline of its own. The store is
    therefore always the *proposed* value while a revision is pending — which is
    the asymmetry §6 discloses.
    """
    source = build_sdt_docx(
        tmp_path / "dual_src.docx",
        _bound("M-CONTENT-VALUE"),
        custom_xml=_store("M-CONTENT-VALUE"),
        store_item_id=STORE_ID,
    )

    def edit(document):
        document.ContentControls(1).Range.Text = "M-2026-999"
        return _content_revisions(document)

    revisions = edit_and_save(word_app, source, tmp_path / "dual.docx", edit, track=True)

    assert revisions == 2
    sdt = _sdt_of(document_xml(tmp_path / "dual.docx"), "matter_number")
    assert "<w:ins " in sdt and "M-2026-999" in sdt
    assert "<w:delText>M-CONTENT-VALUE</w:delText>" in sdt
    assert "<w:matter>M-2026-999</w:matter>" not in (custom_xml(tmp_path / "dual.docx") or "")
    assert "<matter>M-2026-999</matter>" in (custom_xml(tmp_path / "dual.docx") or "")


def test_word_resyncs_the_bound_store_when_the_edit_is_rejected(word_app, tmp_path: Path):
    """CC-6(e): AMENDS spec-set-field.md §6's "known asymmetry".

    The spec says rejecting "leaves the store holding the new value". In Word it
    does not: rejecting restores the content and the binding engine pushes the
    restored value back into the store, so both converge on the original.

    The asymmetry is real, but it belongs to Adeu, not to Word — a HEADLESS
    reject (Adeu's own accept/reject path, which does not run a binding engine)
    is what leaves the store stale. And because the store wins on open
    (test above), a stale store does not merely disagree: on the next open Word
    re-applies the rejected value to the content. CC-9's resync policy needs to
    cover the reject path, not the accept path.
    """
    source = build_sdt_docx(
        tmp_path / "reject_src.docx",
        _bound("M-CONTENT-VALUE"),
        custom_xml=_store("M-CONTENT-VALUE"),
        store_item_id=STORE_ID,
    )
    edited = tmp_path / "reject_edited.docx"
    edit_and_save(
        word_app,
        source,
        edited,
        lambda document: setattr(document.ContentControls(1).Range, "Text", "M-2026-999"),
        track=True,
    )
    assert "<matter>M-2026-999</matter>" in (custom_xml(edited) or "")

    def reject(document):
        document.Revisions.RejectAll()
        return document.ContentControls(1).Range.Text

    text = edit_and_save(word_app, edited, tmp_path / "rejected.docx", reject, track=False)

    assert text == "M-CONTENT-VALUE"
    assert "<matter>M-CONTENT-VALUE</matter>" in (custom_xml(tmp_path / "rejected.docx") or "")


def test_dangling_binding_edits_the_content_and_does_not_fail(word_app, tmp_path: Path):
    """CC-6(e): confirms spec-set-field.md §6.3 — a dead binding is not an error.

    `w:storeItemID` pointing at a part that is not there leaves
    `XMLMapping.IsMapped` false. Word edits the content normally and says
    nothing, which is why §6.3 downgrades to a content-only write plus a warning
    rather than refusing the edit.
    """
    source = build_sdt_docx(tmp_path / "dangle_src.docx", _bound("M-CONTENT-VALUE"))

    def edit(document):
        control = document.ContentControls(1)
        mapped = control.XMLMapping.IsMapped
        control.Range.Text = "M-DANGLING"
        return mapped, _content_revisions(document)

    mapped, revisions = edit_and_save(word_app, source, tmp_path / "dangle.docx", edit, track=True)

    assert mapped is False
    assert revisions == 2
    sdt = _sdt_of(document_xml(tmp_path / "dangle.docx"), "matter_number")
    assert "<w:ins " in sdt and "M-DANGLING" in sdt
    assert f'w:storeItemID="{STORE_ID}"' in sdt, "the dead binding is preserved, not scrubbed"


# CC-20: bindings to the PACKAGE CORE PROPERTIES rather than a customXml item.
# Word exposes docProps/core.xml through the data store under a well-known item
# id, so `/ns1:coreProperties[1]/ns0:title[1]` is a live binding, not a dangling
# one — three corpus documents drive their cover-page title/subject fields this
# way. The prefixes are Word's own generated ones and carry no meaning beyond
# the mappings declared on the sdt.
CORE_PROPS_STORE_ID = "{6C3C8BC8-F283-45AE-878A-BAB7291924A1}"
CORE_TITLE_XPATH = "/ns1:coreProperties[1]/ns0:title[1]"
CORE_PREFIX_MAPPINGS = (
    "xmlns:ns0='http://purl.org/dc/elements/1.1/' "
    "xmlns:ns1='http://schemas.openxmlformats.org/package/2006/metadata/core-properties'"
)


def _core_bound(content: str) -> str:
    return para("Probe.") + (
        "<w:p>"
        + run("Title: ")
        + '<w:sdt><w:sdtPr><w:tag w:val="doc_title"/><w:id w:val="120"/>'
        + f'<w:dataBinding w:prefixMappings="{CORE_PREFIX_MAPPINGS}" '
        + f'w:xpath="{CORE_TITLE_XPATH}" w:storeItemID="{CORE_PROPS_STORE_ID}"/>'
        + "<w:text/></w:sdtPr><w:sdtContent>"
        + run(content)
        + "</w:sdtContent></w:sdt>"
        + run(".")
        + "</w:p>"
    )


def test_a_core_properties_binding_is_mapped_and_the_store_still_wins(word_app, tmp_path: Path):
    """CC-20, the measurement CC-6(e) never covered.

    CC-6(e) established "the store wins on open" against a `customXml` item. A
    core-properties binding resolves through a different mechanism — a well-known
    store item id over `docProps/core.xml`, a package part — so whether the same
    rule holds was an open question, and the answer decides CC-20's fix. If the
    store wins, `find_bound_store` must learn about this part and dual-write it,
    exactly as for customXml. If the content won, the fix would instead be to
    stop warning about a binding that needs no store write.
    """
    source = build_sdt_docx(
        tmp_path / "core_src.docx",
        _core_bound("T-CONTENT-VALUE"),
        core_properties={"title": "T-STORE-VALUE"},
    )

    def probe(document):
        control = document.ContentControls(1)
        return control.XMLMapping.IsMapped, control.Range.Text

    mapped, text = edit_and_save(word_app, source, tmp_path / "core.docx", probe, track=False)

    # The binding is LIVE. Adeu currently calls this one dangling.
    assert mapped is True, "a core-properties binding is mapped, not dangling"
    assert text == "T-STORE-VALUE", "the store wins here too"
    assert "T-CONTENT-VALUE" not in document_xml(tmp_path / "core.docx")


def test_word_dual_writes_the_core_properties_store(word_app, tmp_path: Path):
    """CC-20: and the write goes back to `docProps/core.xml`, so Adeu must too.

    The consequence is the same as CC-18's: a content-only write to one of these
    is reverted the next time the document is opened. Which makes the "the data
    store could not be resolved" warning worse than merely inaccurate — it
    describes a write as degraded-but-done when it is in fact temporary.
    """
    source = build_sdt_docx(
        tmp_path / "core_dw_src.docx",
        _core_bound("T-STORE-VALUE"),
        core_properties={"title": "T-STORE-VALUE"},
    )
    out = tmp_path / "core_dw.docx"
    edit_and_save(
        word_app,
        source,
        out,
        lambda document: setattr(document.ContentControls(1).Range, "Text", "T-TYPED"),
        track=True,
    )

    with zipfile.ZipFile(out) as z:
        core = z.read("docProps/core.xml").decode("utf-8")
    assert "T-TYPED" in core, f"Word wrote the store: {core}"


# --------------------------------------------------------------------------
# (f) placeholder re-instatement
# --------------------------------------------------------------------------

GLOSSARY = {"CounterpartyPlc": "[Counterparty legal name]"}

PLACEHOLDER_BODY = para("Probe.") + (
    "<w:p>" + run("Counterparty: ") + '<w:sdt><w:sdtPr><w:tag w:val="counterparty"/><w:id w:val="103"/>'
    '<w:placeholder><w:docPart w:val="CounterpartyPlc"/></w:placeholder>'
    "<w:text/></w:sdtPr><w:sdtContent>" + run("ACME Corp") + "</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
)


def test_placeholder_returns_when_the_content_is_deleted_untracked(word_app, tmp_path: Path):
    """CC-6(f): emptying a control untracked re-shows its placeholder immediately.

    AMENDS spec-set-field.md §4's "placeholder is NOT re-instated in v1": Word
    does re-instate it, resolving the prose from the glossary doc part and
    setting `w:showingPlcHdr` — and the regenerated ghost run does NOT carry
    `rStyle PlaceholderText`.

    That last detail is load-bearing for CC-1: `w:showingPlcHdr` is the ONLY
    reliable signal that a control is showing placeholder text. Detecting the
    ghost by its style would miss every control Word itself emptied.
    """
    source = build_sdt_docx(tmp_path / "plc_src.docx", PLACEHOLDER_BODY, glossary=GLOSSARY)

    def clear(document):
        document.ContentControls(1).Range.Delete()
        return document.ContentControls(1).ShowingPlaceholderText

    showing = edit_and_save(word_app, source, tmp_path / "plc.docx", clear, track=False)

    assert showing is True
    sdt = _sdt_of(document_xml(tmp_path / "plc.docx"), "counterparty")
    assert "<w:showingPlcHdr/>" in sdt
    assert "[Counterparty legal name]" in sdt
    assert "ACME Corp" not in sdt
    assert "PlaceholderText" not in sdt, "the regenerated ghost is unstyled"


def test_placeholder_stays_hidden_while_the_deletion_is_pending(word_app, tmp_path: Path):
    """CC-6(f): a tracked delete does NOT re-show the placeholder.

    Confirms the v1 rule for the state Adeu actually produces. The content is
    still logically present as `w:delText`, so the control is not empty yet and
    Word leaves `w:showingPlcHdr` off — across a save and a reopen.
    """
    source = build_sdt_docx(tmp_path / "plct_src.docx", PLACEHOLDER_BODY, glossary=GLOSSARY)

    def clear(document):
        document.ContentControls(1).Range.Delete()
        return document.ContentControls(1).ShowingPlaceholderText

    showing = edit_and_save(word_app, source, tmp_path / "plct.docx", clear, track=True)
    assert showing is False

    sdt = _sdt_of(document_xml(tmp_path / "plct.docx"), "counterparty")
    assert "<w:showingPlcHdr/>" not in sdt
    assert "<w:delText>ACME Corp</w:delText>" in sdt

    reopened = edit_and_save(
        word_app,
        tmp_path / "plct.docx",
        tmp_path / "plct_re.docx",
        lambda document: document.ContentControls(1).ShowingPlaceholderText,
        track=False,
    )
    assert reopened is False, "reopening does not resolve the pending deletion"


def test_placeholder_returns_when_the_pending_deletion_is_accepted(word_app, tmp_path: Path):
    """CC-6(f): accepting the delete is what brings the placeholder back.

    The consequence for Adeu: `accept_all_changes` on a document whose controls
    were emptied by tracked deletion produces a file that DIFFERS from what Word
    produces from the same input — Word re-shows placeholders, Adeu leaves empty
    controls. Recorded as a v1.1/CC-9 item rather than fixed here; CC-6 ships
    knowledge, not behaviour.
    """
    source = build_sdt_docx(tmp_path / "plca_src.docx", PLACEHOLDER_BODY, glossary=GLOSSARY)
    deleted = tmp_path / "plca_deleted.docx"
    edit_and_save(
        word_app,
        source,
        deleted,
        lambda document: document.ContentControls(1).Range.Delete(),
        track=True,
    )

    def accept(document):
        document.Revisions.AcceptAll()
        return document.ContentControls(1).ShowingPlaceholderText

    showing = edit_and_save(word_app, deleted, tmp_path / "plca.docx", accept, track=False)

    assert showing is True
    sdt = _sdt_of(document_xml(tmp_path / "plca.docx"), "counterparty")
    assert "<w:showingPlcHdr/>" in sdt
    assert "[Counterparty legal name]" in sdt
    assert "ACME Corp" not in sdt


# --------------------------------------------------------------------------
# CC-1c — is `w14:checked` authoritative for the projection?
#
# spec-projection.md §4 says "Project as `[x]` (checked per `w14:checked
# w14:val` ∈ {"1","true"}) or `[ ]` — never the raw glyph run". That picks the
# state attribute over the glyph, which is only safe if the two cannot disagree.
# CC-6(b) already showed the attribute is untracked: Word flips it with no
# revision of its own. An untracked attribute riding alongside a TRACKED glyph
# swap is exactly the shape that produced the bound-store asymmetry in CC-6(e),
# so it needs the same reject probe before §4 can be trusted.
# --------------------------------------------------------------------------


def test_rejecting_a_checkbox_toggle_restores_the_state_attribute(word_app, tmp_path: Path):
    """CC-1c: Word keeps `w14:checked` and the glyph consistent across a reject.

    The glyph swap is tracked (`w:ins` + `w:del`) but `w14:checked` is not, so
    rejecting could plausibly restore the glyph and strand the attribute — the
    document would then read `☐` on screen and `checked="1"` in the file, and
    spec-projection.md §4's "project per `w14:checked`" would render `[x]` under
    a visibly empty box. Word does not do that: it rolls the attribute back with
    the revision.

    That is what licenses §4. Pinned because the licence is Word's behaviour,
    not a property of the format — nothing in the schema ties the attribute to
    the run, and a future Word that forgot would silently turn every rejected
    toggle into a projection lie.
    """
    body = para("Probe.") + _checkbox("confidential", 126, "0", "\u2610")
    source = build_sdt_docx(tmp_path / "cbrej_src.docx", body)

    toggled = tmp_path / "cbrej_toggled.docx"
    edit_and_save(
        word_app,
        source,
        toggled,
        lambda document: setattr(document.ContentControls(1), "Checked", True),
        track=True,
    )
    assert '<w14:checked w14:val="1"/>' in _sdt_of(document_xml(toggled), "confidential")

    def reject(document):
        document.Revisions.RejectAll()
        return document.ContentControls(1).Checked

    checked = edit_and_save(word_app, toggled, tmp_path / "cbrej.docx", reject, track=False)

    assert checked is False, "Word reports the control unchecked again"
    sdt = _sdt_of(document_xml(tmp_path / "cbrej.docx"), "confidential")
    assert '<w14:checked w14:val="0"/>' in sdt, (
        "the untracked state attribute rolled back with the tracked glyph — "
        "this is what makes spec-projection.md §4 safe"
    )
    assert "\u2610" in sdt and "\u2612" not in sdt
    assert "<w:ins " not in sdt and "<w:del " not in sdt


def test_word_writes_the_checkbox_glyph_as_literal_text_not_w_sym(word_app, tmp_path: Path):
    """CC-1c: the glyph is a `w:t` character, so the projection can see it.

    This is a load-bearing assumption of A1.8's "NO `☒`/`☐` characters" check and
    of the mapper's width accounting: a 3-character token maps onto a
    1-character run only if there IS a 1-character run. Word could legitimately
    have used `<w:sym w:font="MS Gothic" w:char="F0FE"/>`, which projects as
    nothing at all today (the node engine drops `w:sym` deliberately, CC-12) —
    the token would then map onto a zero-width run and every offset after it
    would shift.

    Measured on a toggle, not just on the fixture, because the fixture's run is
    ours; the inserted run is Word's.
    """
    body = para("Probe.") + _checkbox("waiver", 136, "0", "\u2610")
    source = build_sdt_docx(tmp_path / "cbsym_src.docx", body)

    edit_and_save(
        word_app,
        source,
        tmp_path / "cbsym.docx",
        lambda document: setattr(document.ContentControls(1), "Checked", True),
        track=True,
    )

    sdt = _sdt_of(document_xml(tmp_path / "cbsym.docx"), "waiver")
    assert "<w:sym" not in sdt, "Word wrote the glyph as a symbol run, not text"
    assert "<w:t>\u2612</w:t>" in sdt, "the checked glyph is one literal character in a w:t"


def test_word_refuses_plain_text_edits_inside_a_checkbox(word_app, tmp_path: Path):
    """CC-1c: a checkbox's content is protected by Word itself, with no lock set.

    Probed expecting the opposite. The hypothesis was that Word would let you
    overwrite the glyph with arbitrary text, leaving a `w14:checkbox` whose
    content is not a ballot glyph and whose `w14:checked` describes nothing —
    which would have meant the projection must never assume the content of a
    checkbox is a glyph. Word refuses outright:

        "You are not allowed to edit this selection because it is protected."

    The fixture sets no `w:lock` and no `w:documentProtection`. The protection is
    intrinsic to the checkbox control type. Two consequences:

    * spec-projection.md §4 may assume the content IS the glyph run, so mapping a
      3-character token onto it is sound for every document Word produced.
    * A3.8's rejection of any textual mutation other than `[ ]` ↔ `[x]` is not an
      Adeu house rule; it reproduces Word's own refusal. That is a much stronger
      footing for the error message, which can now say what Word says.

    A document with prose inside a `w14:checkbox` remains constructible by hand
    (we just built one to test with), so the projection should still not CRASH on
    it — but it is malformed input, not a shape Word makes.
    """
    body = para("Probe.") + _checkbox("waiver", 146, "0", "\u2610")
    source = build_sdt_docx(tmp_path / "cbtext_src.docx", body)

    def overwrite(document):
        return _refusal(lambda: setattr(document.ContentControls(1).Range, "Text", "maybe"))

    refusal = edit_and_save(word_app, source, tmp_path / "cbtext.docx", overwrite, track=False)

    assert refusal is not None, "Word allowed arbitrary text into a checkbox control"
    assert "protected" in refusal, f"refused, but not as a protection error: {refusal}"

    sdt = _sdt_of(document_xml(tmp_path / "cbtext.docx"), "waiver")
    assert "\u2610" in sdt and "maybe" not in sdt, "the refusal left the glyph intact"
