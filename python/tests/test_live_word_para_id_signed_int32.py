# FILE: tests/test_live_word_para_id_signed_int32.py
"""
Word-verified regression tests for BUG_paraId_signed_int32_thread_collapse.md
(B5, reported 2026-08-12 against Adeu 2.2.0 / 0db3cc2).

`w14:paraId` was minted across the full 32-bit range. Word parses every
`ST_LongHexNumber` as a SIGNED 32-bit integer and ECMA-376 requires the value
to be greater than `0x00000000` and less than `0x80000000`; out-of-range values
are not rejected, they are silently discarded and regenerated on load. Roughly
half of all paraIds Adeu minted were therefore thrown away by Word, taking
every `w15:paraIdParent` pointing at them along.

**The XML is worthless as an oracle here.** In every failing case the package
was schema-valid, internally consistent and exactly what the writer intended;
`process_batch` reported success and B1's `CommentThreadingError` correctly did
not fire. Only Word sees the difference, so these tests ask Word — through
`Comment.Ancestor`, `Comment.Replies` and `Comment.Scope`.

Measured here against Word 16.0 (tests/word_com.py is the harness):

| package                              | Word                                     |
|--------------------------------------|------------------------------------------|
| every id in (0, 0x80000000)          | thread intact, anchors intact            |
| thread root's paraId >= 0x80000000   | EVERY reply becomes a top-level comment  |
| a reply's own paraId >= 0x80000000   | that reply leaves the thread             |
| paraId == 0x7FFFFFFF                 | fine (largest legal value)               |
| paraId == 0x80000000                 | thread collapses (smallest illegal one)  |
| paraId == 0x00000000                 | Word REFUSES the file: "appears corrupted" |
| one out-of-range paraId in a part    | Word renumbers EVERY paraId in that part |

The last row is why this is a class and not three point defects: a single bad
id invalidates every id in the part, so every `{#cell:paraId}` anchor an agent
was handed goes stale on the next Word round-trip too.
"""

import io
import random
import re
import sys
import zipfile
from pathlib import Path

import pytest
from docx import Document

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Live Word COM tests require Windows")

from adeu.models import ModifyText, ReplyComment  # noqa: E402
from adeu.redline.engine import RedlineEngine  # noqa: E402
from tests.utils import edge_of_range_randint  # noqa: E402
from tests.word_com import (  # noqa: E402
    WordRefusedDocument,
    author_document,
    read_comments,
    round_trip,
    thread_map,
)

BODY = "The parties shall confer in good faith before moving to compel production."

#: Non-overlapping anchors, so a document can carry several independent threads
#: without the matcher having to disambiguate anything.
ANCHORS = ("confer in good faith", "moving to compel", "The parties", "production")

ROOT = "Is this the right meet-and-confer standard?"
REPLY_ONE = "Addressed in the revised clause."
REPLY_TWO = "Also conformed the cross-reference."


def _blank_doc() -> io.BytesIO:
    doc = Document()
    doc.add_paragraph(BODY)
    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def build_thread(*, replies=(REPLY_ONE, REPLY_TWO), roots=1) -> bytes:
    """A saved package with `roots` independent comment threads, each carrying
    `replies`. Comment bodies are unique so Word's anchor-ordered
    `Comments.Item(i)` can be matched back to what was written."""
    assert roots <= len(ANCHORS), "add another anchor"
    suffix = (lambda i: "") if roots == 1 else (lambda i: f" ({i})")

    engine = RedlineEngine(_blank_doc(), author="Sarah Chen")
    for i in range(roots):
        engine.apply_edits([ModifyText(target_text=ANCHORS[i], new_text=ANCHORS[i], comment=f"{ROOT}{suffix(i)}")])

    engine.author = "Adeu AI"
    for i, root_id in enumerate(list(engine.comments_manager.extract_comments_data())):
        for reply in replies:
            engine.apply_review_actions([ReplyComment(target_id=f"Com:{root_id}", text=f"{reply}{suffix(i)}")])
    return engine.save_to_stream().getvalue()


def write(tmp_path: Path, name: str, package: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(package)
    return path


def rewrite_ids(package: bytes, mapping: dict) -> bytes:
    """Substitute literal text across every XML part — the one-bit experiment."""
    source = zipfile.ZipFile(io.BytesIO(package))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename.endswith(".xml"):
                text = data.decode("utf-8")
                for old, new in mapping.items():
                    text = text.replace(old, new)
                data = text.encode("utf-8")
            target.writestr(item, data)
    return out.getvalue()


#: The four attributes a comment paragraph's identity is spelled with. A paraId
#: has to move in all of them at once or the package stops being self-consistent
#: and the experiment measures the wrong thing.
PARA_ID_ATTRIBUTES = ("w14:paraId", "w15:paraId", "w15:paraIdParent", "w16cid:paraId")


def remap_para_ids(package: bytes, mapping: dict) -> bytes:
    """Move paraIds, attribute-qualified so an identically-valued rsid cannot
    be caught in the crossfire."""
    literal = {f'{attr}="{old}"': f'{attr}="{new}"' for old, new in mapping.items() for attr in PARA_ID_ATTRIBUTES}
    return rewrite_ids(package, literal)


def comment_para_ids(package: bytes) -> list:
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        for name in archive.namelist():
            if "comments" in name and name.endswith(".xml") and "Extended" not in name and "Ids" not in name:
                xml = archive.read(name).decode("utf-8")
                return re.findall(r'w14:paraId="([0-9A-Fa-f]{8})"', xml)
    raise AssertionError("no comments part in the package")


#: Fixed, unmistakably in-range paraIds for the controls below. The controls
#: measure WORD, so they must not inherit whatever the engine's generator
#: happens to produce — before the fix, half of those are the thing under test.
HEALTHY_PARA_IDS = ("11111101", "11111104", "11111107")


def healthy_thread_package() -> tuple:
    """A root + two replies whose paraIds are all known-good. Returns
    `(package bytes, [root paraId, reply paraIds…])`."""
    package = build_thread()
    original = comment_para_ids(package)
    assert len(original) == len(HEALTHY_PARA_IDS), original
    assert len(set(original)) == len(original), f"the engine minted duplicate paraIds: {original}"
    remapped = dict(zip(original, HEALTHY_PARA_IDS, strict=True))
    return remap_para_ids(package, remapped), list(HEALTHY_PARA_IDS)


# ---------------------------------------------------------------------------
# The defect, as Word sees it
# ---------------------------------------------------------------------------


class TestWordSeesTheThread:
    def test_word_threads_replies_when_the_rng_lands_at_the_top_of_the_range(self, word_app, tmp_path, monkeypatch):
        """
        The deterministic form of the bug. `random.randint` is pinned to the
        TOP of whatever range each generator asks for — a value it could
        genuinely have returned — so the pre-fix generator mints `FFFFFFFF,
        FFFFFFFE, …` (every one out of range) and the fixed one mints
        `7FFFFFFF, 7FFFFFFE, …` (every one legal).

        Pre-fix Word reports three unrelated top-level comments. Nothing in the
        XML says so.
        """
        monkeypatch.setattr(random, "randint", edge_of_range_randint(high=True))
        path = write(tmp_path, "top_of_range.docx", build_thread())

        comments = read_comments(word_app, path)
        assert len(comments) == 3, f"expected the root and both replies, Word found {comments}"

        threads = thread_map(comments)
        assert threads == {ROOT: None, REPLY_ONE: ROOT, REPLY_TWO: ROOT}, (
            f"Word did not thread the replies: {threads}. Every w15:paraIdParent in the package "
            "points at the right paraId — but Word discarded the out-of-range paraId it pointed "
            "AT, so the references dangle and each reply renders as a new top-level comment. "
            "This is the failure an agent cannot see: it reads the document back, cannot find "
            "its reply in the thread, and retries."
        )
        root = next(c for c in comments if c.body == ROOT)
        assert root.reply_count == 2, f"the thread root reports {root.reply_count} replies, expected 2"

    def test_word_opens_a_package_when_the_rng_lands_at_the_bottom_of_the_range(self, word_app, tmp_path, monkeypatch):
        """
        The other end of the same range bug, and the more severe outcome:
        `randint(0, …)` can return 0, and `w14:paraId="00000000"` is not
        silently repaired — Word refuses to open the document at all ("The file
        appears to be corrupted"). The spec forbids `0x00000000` for exactly
        this reason; the generator must start at 1.
        """
        monkeypatch.setattr(random, "randint", edge_of_range_randint(high=False))
        package = build_thread()
        assert "00000000" not in comment_para_ids(package), (
            "a generator that can return 0 minted w14:paraId=00000000; Word will not open this file"
        )

        path = write(tmp_path, "bottom_of_range.docx", package)
        try:
            comments = read_comments(word_app, path)
        except WordRefusedDocument as exc:
            pytest.fail(f"Word refused a package Adeu reported as written successfully: {exc}")
        assert thread_map(comments) == {ROOT: None, REPLY_ONE: ROOT, REPLY_TWO: ROOT}

    def test_word_threads_every_reply_with_the_real_rng(self, word_app, tmp_path):
        """
        No RNG substitution: the engine's own generator, four independent
        threads. Each thread survives the pre-fix generator only if BOTH its
        root's and its reply's paraId happen to land in the low half, so this
        passes by luck with probability (1/4)^4 < 0.5%. After the fix it is
        deterministic.
        """
        path = write(tmp_path, "real_rng.docx", build_thread(replies=(REPLY_ONE,), roots=4))

        comments = read_comments(word_app, path)
        assert len(comments) == 8, f"expected 4 roots + 4 replies, Word found {len(comments)}"

        orphans = [c.body for c in comments if c.body.startswith(REPLY_ONE) and c.is_top_level]
        assert not orphans, (
            f"{len(orphans)}/4 replies rendered as new top-level comments: {orphans}. "
            "With a full-range paraId generator this is the EXPECTED outcome, not an edge case."
        )

    def test_word_anchors_every_comment_adeu_writes(self, word_app, tmp_path, monkeypatch):
        """
        The B3 half of the class, now Word-verified rather than XML-verified:
        an out-of-range `w16cid:durableId` collapses the comment's anchor to a
        zero-length point — right author, right text, no highlight. The masked
        durableId generator must survive the same top-of-range RNG.
        """
        monkeypatch.setattr(random, "randint", edge_of_range_randint(high=True))
        path = write(tmp_path, "anchors.docx", build_thread())

        unanchored = [c.body for c in read_comments(word_app, path) if not c.is_anchored]
        assert not unanchored, (
            f"Word collapsed the anchor of {unanchored} to a zero-length point: the comment "
            "opens attached to nothing at all."
        )


# ---------------------------------------------------------------------------
# Controls — these prove the oracle above is not vacuous
# ---------------------------------------------------------------------------


class TestWordOracleControls:
    """
    A green threading test means nothing unless the same check goes red when
    the id really is out of range. These take a package Word threads correctly
    and change ONE BIT, which is the whole experiment: the failure is caused by
    the high bit and by nothing else.

    They pass before AND after the fix — they pin Word's behaviour, not Adeu's.
    """

    def test_setting_the_high_bit_on_the_root_para_id_collapses_the_thread(self, word_app, tmp_path):
        package, (root_para_id, *_) = healthy_thread_package()

        healthy = read_comments(word_app, write(tmp_path, "healthy.docx", package))
        assert thread_map(healthy) == {ROOT: None, REPLY_ONE: ROOT, REPLY_TWO: ROOT}, (
            "the baseline package does not even thread — nothing below measures anything"
        )

        flipped = remap_para_ids(package, {root_para_id: f"{int(root_para_id, 16) | 0x80000000:08X}"})
        broken = read_comments(word_app, write(tmp_path, "high_bit.docx", flipped))
        assert thread_map(broken) == {ROOT: None, REPLY_ONE: None, REPLY_TWO: None}, (
            "flipping the high bit of the thread root's paraId did NOT collapse the thread — "
            "the oracle these tests depend on no longer measures the defect"
        )

    def test_setting_the_high_bit_on_a_reply_detaches_that_reply(self, word_app, tmp_path):
        """The other role the bad id can sit in. The root stays valid, so the
        thread survives — minus the reply Word could no longer resolve."""
        package, (_root, first_reply, _second) = healthy_thread_package()

        flipped = remap_para_ids(package, {first_reply: f"{int(first_reply, 16) | 0x80000000:08X}"})
        threads = thread_map(read_comments(word_app, write(tmp_path, "reply_high_bit.docx", flipped)))
        assert threads[REPLY_ONE] is None, f"a reply whose OWN paraId is out of range stayed in the thread: {threads}"
        assert threads[REPLY_TWO] == ROOT, (
            f"the untouched sibling was collateral damage: {threads}. Expected only the reply "
            "with the bad id to detach."
        )

    @pytest.mark.parametrize(
        "value, threads",
        [
            ("7FFFFFFF", True),  # largest legal ST_LongHexNumber
            ("80000000", False),  # smallest illegal one — exactly one greater
        ],
    )
    def test_the_threading_boundary_is_exactly_0x80000000(self, word_app, tmp_path, value, threads):
        package, (root_para_id, *_) = healthy_thread_package()
        path = write(tmp_path, f"boundary_{value}.docx", remap_para_ids(package, {root_para_id: value}))

        root = next(c for c in read_comments(word_app, path) if c.body == ROOT)
        assert (root.reply_count == 2) is threads, (
            f"root paraId={value}: Word reported {root.reply_count} replies, expected "
            f"{'2' if threads else '0'}. The legal range is 0x00000000 < paraId < 0x80000000."
        )

    def test_a_zero_para_id_makes_word_refuse_the_file(self, word_app, tmp_path):
        """`00000000` is not a silent-repair case: Word rejects the package.
        The spec forbids it as explicitly as it forbids the high half, and it
        is the one outcome in this class the user sees immediately."""
        package, (root_para_id, *_) = healthy_thread_package()
        path = write(tmp_path, "zero.docx", remap_para_ids(package, {root_para_id: "00000000"}))

        with pytest.raises(WordRefusedDocument):
            read_comments(word_app, path)


# ---------------------------------------------------------------------------
# Blast radius — one bad id renumbers the whole part
# ---------------------------------------------------------------------------


class TestParaIdBlastRadius:
    """
    `{#cell:<paraId>}` anchors are handed to agents as addresses of empty table
    cells, and Adeu reads them back. They only survive a Word round-trip
    because Word preserves paraIds it accepts — measured below: 32/32 kept
    across an open/save with no edits.

    Push ONE of those 32 out of range and Word keeps NONE of them: it renumbers
    every paraId in the part. So a single bad id does not damage one anchor, it
    invalidates all of them.
    """

    @staticmethod
    def _para_ids(path: Path) -> list:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        return re.findall(r'w14:paraId="([0-9A-Fa-f]{8})"', xml)

    def test_one_out_of_range_para_id_invalidates_every_para_id_in_the_part(self, word_app, tmp_path):
        """
        Paired one-bit experiment: the SAME Word-authored baseline is
        round-tripped twice, once untouched and once with a single paraId
        pushed over `0x7FFFFFFF`. Both arms go through the identical zip-rewrite
        pipeline, so the repack is controlled for and the high bit is the only
        difference between them.

        The baseline must be a document WORD wrote. A foreign document has its
        paraIds re-stamped wholesale on first save (verified — all 32 changed),
        which would swamp the one-bit signal entirely.
        """
        baseline = author_document(word_app, tmp_path / "baseline.docx")
        before = self._para_ids(baseline)
        assert len(before) >= 4, f"too few paraIds to measure: {before}"
        assert all(int(v, 16) < 0x80000000 for v in before), (
            f"Word's own output contains high-bit paraIds "
            f"{[v for v in before if int(v, 16) >= 0x80000000]} — the premise of this whole bug "
            "report is that it never does"
        )

        control = tmp_path / "control.docx"
        control.write_bytes(rewrite_ids(baseline.read_bytes(), {}))
        kept = self._para_ids(round_trip(word_app, control, tmp_path / "control_rt.docx"))

        victim = before[len(before) // 2]
        treatment = tmp_path / "one_bad.docx"
        treatment.write_bytes(
            rewrite_ids(
                baseline.read_bytes(),
                {f'w14:paraId="{victim}"': f'w14:paraId="{int(victim, 16) | 0x80000000:08X}"'},
            )
        )
        after = self._para_ids(round_trip(word_app, treatment, tmp_path / "one_bad_rt.docx"))

        assert set(before) <= set(kept), (
            f"CONTROL: Word dropped {len(set(before) - set(kept))}/{len(before)} paraIds from a "
            "package it accepted, with no id changed at all. The experiment below measures "
            "nothing if the round-trip is not otherwise faithful."
        )
        survivors = [v for v in before if v in after]
        assert not survivors, (
            f"{len(survivors)}/{len(before)} paraIds survived one out-of-range sibling. This "
            "test documents that NONE do: a single bad id makes Word renumber the entire part, "
            "so one bad id invalidates every {#cell:paraId} anchor in the document — not just "
            "its own."
        )
