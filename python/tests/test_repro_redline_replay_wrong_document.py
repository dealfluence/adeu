"""CC-14 — a batch that applies cleanly must produce the document it was asked for.

`test_property_invariants.py::test_p2_json_text_roundtrip_is_exact_or_loud` is
allowed to see the engine REFUSE an edit; it is not allowed to see the engine
accept one and write something else. Two independent defects broke that, both
pre-existing and both silent — `edits_skipped == 0`, no `BatchValidationError`,
a wrong document.

`.hypothesis` is gitignored, so the property search will not rediscover these
deterministically on CI. They are pinned here as explicit examples, plus the
minimal apply-level shapes underneath them.

DEFECT 1 (Python only) — the rstrip "Smart Fallback" in `_resolve_single_match`.
    "0 0." → "0.\\n\\n0." diffs to target "0 " / new "0.\\n\\n". The branch
    rstripped the target to "0", saw the replacement extend it, and inserted the
    remainder BEFORE the document's trailing space, stranding that space at the
    head of the new paragraph: "0.\\n\\n 0.". The branch is right for a
    separator space inside one paragraph and wrong the moment the replacement
    introduces a paragraph break. Node has no such branch and was already
    correct, so this was a dual-engine parity break too.

DEFECT 2 (both engines) — a shared trailing paragraph mark in a COMMENTED edit.
    `trim_common_context` is word-boundary aware and will not trim across
    "\\n\\n", so `_single_commented_sub_edit` received the whole span. The apply
    layer track-deletes a target's trailing mark (a genuine merge needs that)
    but never re-creates the one the replacement asks for, so exactly one
    paragraph break vanished. Only reachable WITH a comment: the uncommented
    path word-diffs the span and never hands a bare mark to the apply layer.
    Every edit the diff pipeline emits carries a "Diff: ..." comment.
"""

import json
from io import BytesIO

import pytest
from pydantic import TypeAdapter

from adeu.diff import generate_edits_via_paragraph_alignment, make_edits_self_contained
from adeu.ingest import extract_text_from_stream
from adeu.models import BatchChanges, ModifyText
from adeu.redline.engine import RedlineEngine
from tests.test_property_invariants import build_doc_stream


def _clean(stream: BytesIO) -> str:
    return extract_text_from_stream(BytesIO(stream.getvalue()), clean_view=True)


def _accept(engine: RedlineEngine) -> str:
    engine.accept_all_revisions(remove_comments=True)
    return extract_text_from_stream(engine.save_to_stream(), clean_view=True)


def _replay(paras: list[str], mod: list[str]) -> tuple[str, str, int]:
    """The exact P2 pipeline: align, self-contain, JSON round trip, apply."""
    stream = build_doc_stream(paras)
    text_orig = _clean(stream)
    text_mod = "\n\n".join(mod)

    edits = make_edits_self_contained(generate_edits_via_paragraph_alignment(text_orig, text_mod), text_orig)
    dumped = json.loads(json.dumps([e.model_dump() for e in edits]))
    changes = TypeAdapter(BatchChanges).validate_python(dumped)

    engine = RedlineEngine(BytesIO(stream.getvalue()), author="Fuzz")
    stats = engine.process_batch(list(changes))
    return _accept(engine), text_mod, stats["edits_skipped"]


def _apply_pinned(paras: list[str], target: str, new: str, comment: str | None) -> tuple[str, str]:
    """One atomic edit pinned by index, bypassing match resolution."""
    stream = build_doc_stream(paras)
    engine = RedlineEngine(BytesIO(stream.getvalue()), author="Fuzz")
    edit = ModifyText(type="modify", target_text=target, new_text=new, comment=comment)
    edit._match_start_index = _clean(stream).index(target)
    engine.process_batch([edit])
    return _accept(engine), "\n\n".join(paras).replace(target, new, 1)


# ---------------------------------------------------------------------------
# The two falsifying examples, end to end
# ---------------------------------------------------------------------------
def test_defect_1_paragraph_split_at_a_space_keeps_no_stray_space():
    """Was '0.\\n\\n 0.' — the split consumed the space's neighbours, not the space."""
    got, want, skipped = _replay(["0 0."], ["0.", "0."])
    assert skipped == 0
    assert got == want


def test_defect_2_inserting_paragraphs_around_one_keeps_every_break():
    """Was '0.A.' — the break between the inserted paragraph and 'A.' vanished."""
    got, want, skipped = _replay(["0.", "0 0.", "A.", "00."], ["0.", "0 0.", "0.", "A.", "0.", "00."])
    assert skipped == 0
    assert got == want


# ---------------------------------------------------------------------------
# Defect 1, at the resolution layer
# ---------------------------------------------------------------------------
def test_defect_1_resolves_as_one_modification_not_a_misplaced_insertion():
    """The wrong shape was INSERTION at index 1, which left the space behind."""
    stream = build_doc_stream(["0 0."])
    engine = RedlineEngine(BytesIO(stream.getvalue()), author="Fuzz")
    edit = ModifyText(type="modify", target_text="0 ", new_text="0.\n\n")

    resolved = engine._pre_resolve_heuristic_edit(edit, index_offset=0)
    subs = resolved if isinstance(resolved, list) else [resolved]

    assert len(subs) == 1
    assert subs[0]._match_start_index == 0, "must consume the target from its start"
    assert subs[0].target_text == "0 ", "the trailing space must be part of the target"


def test_the_separator_space_shortcut_still_works_within_one_paragraph():
    """The guard must not disarm the branch it was narrowed on.

    "Section 1 " → "Section 1 Revised" has no paragraph break, so the trailing
    space is still preserved and the following word is not glued on.
    """
    got, want = _apply_pinned(["Section 1 ends here."], "Section 1 ", "Section 1 Revised ", None)
    assert got == want


# ---------------------------------------------------------------------------
# Defect 2, at the apply layer — the shape matrix that located it
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "new_text",
    ["Z.\n\n", "Z.\n\nY.\n\n", "Z.\n\nY.\n\nW.\n\n"],
    ids=["one-segment", "two-segments", "three-segments"],
)
def test_a_shared_trailing_paragraph_mark_survives_a_commented_edit(new_text):
    got, want = _apply_pinned(["A.", "00."], "A.\n\n", new_text, "C")
    assert got == want


def test_a_genuine_paragraph_merge_still_deletes_the_mark():
    """The counter-case the fix must NOT break: the target's mark really goes.

    Only the SHARED mark is structural context. When the replacement does not
    end in one, the caller is merging two paragraphs and the deletion is the
    whole point.
    """
    got, want = _apply_pinned(["A.", "00."], "A.\n\n", "Z.", "C")
    assert got == want == "Z.00."


@pytest.mark.parametrize("comment", [None, "C"], ids=["uncommented", "commented"])
def test_a_leading_paragraph_mark_was_never_affected(comment):
    """Pinned to keep the fix honest: only the TRAILING mark was broken."""
    got, want = _apply_pinned(["0.", "A.", "00."], "\n\nA.", "\n\nZ.\n\nY.", comment)
    assert got == want


def test_splitting_a_paragraph_without_touching_its_mark_is_unchanged():
    got, want = _apply_pinned(["A.", "00."], "A.", "Z.\n\nY.", "C")
    assert got == want
