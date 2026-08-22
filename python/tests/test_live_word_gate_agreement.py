# FILE: tests/test_live_word_gate_agreement.py
"""CC-4: Adeu's gates decide what real Word decides — checked against Word.

`test_live_word_content_controls.py` (CC-6) measured what Word permits.
`test_cc_gates.py` pins what Adeu's gates decide. Nothing until now connected
the two, so a gate could be "fixed" into disagreeing with Word and both suites
would stay green.

That gap matters more than it sounds, because the gate errors make claims
about Word in the second person: "Word refuses edits inside locked controls."
If that sentence stops being true, Adeu is not merely over-strict, it is
lying to the agent about why its edit was rejected — and the agent has no way
to check.

So each test here drives a real Word instance over the SAME document Adeu
gates, and asserts the two verdicts agree. Windows-only, and the reason CC-4
was taken on this side of the pair.

The asymmetry that is deliberate and asserted: Adeu is allowed to be stricter
than Word where the write would *succeed in Word and then silently revert*
(G13, data binding — CC-6(e) measured Word resyncing the store on reject).
Everywhere else, disagreement is a bug.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from tests.sdt_fixtures import build_sdt_docx, para, run

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Live Word COM tests require Windows platform",
)

if sys.platform == "win32":
    from tests.word_com import edit_and_save


def _refusal(action) -> str | None:
    """Word's complaint, or None when it permitted the operation."""
    try:
        action()
        return None
    except Exception as exc:  # noqa: BLE001 — COM raises bare pywintypes errors
        return str(exc)


def _lockable(tag: str, sdt_id: int, lock: str | None) -> str:
    lock_xml = f'<w:lock w:val="{lock}"/>' if lock else ""
    return (
        "<w:p>" + run(f"{tag}: ") + f'<w:sdt><w:sdtPr><w:tag w:val="{tag}"/><w:id w:val="{sdt_id}"/>{lock_xml}'
        "<w:text/></w:sdtPr><w:sdtContent>" + run(f"{tag} content") + "</w:sdtContent></w:sdt>" + run(".") + "</w:p>"
    )


AGREEMENT_BODY = (
    para("Ordinary body text.")
    + _lockable("contentlocked", 401, "sdtContentLocked")
    + _lockable("sdtlocked", 402, "sdtLocked")
    + _lockable("unlocked", 403, None)
)


def _adeu_refuses(docx_bytes: bytes, target: str, new: str, **overrides) -> str | None:
    """Adeu's verdict on the same edit, as an error string or None."""
    from adeu.models import ModifyText
    from adeu.redline.engine import RedlineEngine

    eng = RedlineEngine(io.BytesIO(docx_bytes), author="Agreement Test", **overrides)
    errors = eng.validate_edits([ModifyText(type="modify", target_text=target, new_text=new)])
    return "\n".join(errors) or None


def test_adeu_refuses_typing_exactly_where_word_refuses_it(word_app, tmp_path: Path):
    """G1/G2 against Word's own lock semantics.

    Word's mapping is not one-to-one with the XML, which is exactly why this
    has to be measured rather than reasoned about: `sdtContentLocked` sets
    LockContents AND LockContentControl, while `sdtLocked` sets only
    LockContentControl and leaves the CONTENT editable. G2's narrowness — that
    emptying a delete-locked control is allowed — rests on that second fact.
    """
    source = build_sdt_docx(tmp_path / "agree_src.docx", AGREEMENT_BODY)
    docx_bytes = source.read_bytes()

    def probe(document):
        refusals = {}
        for i in range(1, document.ContentControls.Count + 1):
            control = document.ContentControls(i)
            # Bind the control to the lambda's default, not the COM call: the
            # default is evaluated once at definition, so re-fetching it there
            # would issue a fresh COM round trip per invocation.
            refusals[control.Tag] = _refusal(lambda c=control: setattr(c.Range, "Text", "typed"))
        return refusals

    word_refusals = edit_and_save(word_app, source, tmp_path / "agree.docx", probe, track=True)

    for tag in ("contentlocked", "sdtlocked", "unlocked"):
        word_refused = word_refusals[tag] is not None
        adeu_refused = _adeu_refuses(docx_bytes, f"{tag} content", "replacement") is not None
        assert adeu_refused == word_refused, (
            f"{tag}: Word {'refused' if word_refused else 'permitted'} typing "
            f"but Adeu {'refused' if adeu_refused else 'permitted'} it. "
            f"Word said: {word_refusals[tag]!r}"
        )

    # And the one Word refuses is the one whose error quotes Word's reason.
    assert "protected" in (word_refusals["contentlocked"] or "")
    locked_error = _adeu_refuses(docx_bytes, "contentlocked content", "replacement")
    assert locked_error is not None and "content-locked" in locked_error


def test_the_override_reaches_the_same_verdict_word_would_after_unlocking(word_app, tmp_path: Path):
    """`ignore_control_locks` must land where Word lands with the lock removed.

    An override that merely skipped the gate while the edit still could not be
    applied would be worse than no override: it converts a clear refusal into
    a silent no-op, which is the exact failure mode spec-gates §7 exists to
    prevent.
    """
    source = build_sdt_docx(tmp_path / "unlock_src.docx", AGREEMENT_BODY)
    docx_bytes = source.read_bytes()

    def probe(document):
        control = next(
            document.ContentControls(i)
            for i in range(1, document.ContentControls.Count + 1)
            if document.ContentControls(i).Tag == "contentlocked"
        )
        # What the override asserts the user has done in Word.
        control.LockContents = False
        return _refusal(lambda: setattr(control.Range, "Text", "typed"))

    refusal_after_unlock = edit_and_save(word_app, source, tmp_path / "unlock.docx", probe, track=True)

    assert refusal_after_unlock is None, f"Word still refused after clearing LockContents: {refusal_after_unlock!r}"
    assert (
        _adeu_refuses(
            docx_bytes,
            "contentlocked content",
            "replacement",
            ignore_control_locks=True,
        )
        is None
    )
