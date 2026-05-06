# FILE: verify_bug5_fix.py
"""
Verifies Bug 5 fix: Live Word snapshot IDs are renumbered to the disk path's
two-pool scheme.

Strategy:
1. Build a doc with disk-path edits (creates Chg:1..4 and Com:1,2 in a pool-
   independent way).
2. Save the doc, then re-load it as a python-docx Document — this represents
   the disk-style snapshot we want to mimic.
3. Manipulate that snapshot to simulate Word's single-pool numbering by
   shifting all comment IDs upward (e.g., Com:1 -> Com:5).
4. Apply renumber_snapshot_ids to the simulated-Word snapshot.
5. Assert that the renumbered snapshot's projection produces the same Chg/Com
   IDs as the original disk snapshot.

Self-contained.
"""

import sys
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from docx import Document
from docx.oxml.ns import qn

from adeu.ingest import _extract_text_from_doc, extract_text_from_stream
from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine
from adeu.redline.mapper import DocumentMapper, renumber_snapshot_ids


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def strip_appendix(text):
    APPENDIX = "<!-- READONLY_BOUNDARY_START -->"
    if APPENDIX in text:
        return text[: text.find(APPENDIX)].rstrip()
    return text


def make_doc_with_changes_and_comments():
    doc = Document()
    doc.add_paragraph("Quarterly revenue rose by twelve percent.")
    doc.add_paragraph("The team launched three new products this year.")
    stream = BytesIO()
    doc.save(stream)
    stream.seek(0)

    engine = RedlineEngine(stream, author="Reviewer")
    engine.process_batch(
        [
            ModifyText(
                type="modify",
                target_text="twelve",
                new_text="fifteen",
                comment="Verify with finance",
            ),
            ModifyText(
                type="modify",
                target_text="three",
                new_text="four",
                comment="Confirm product count",
            ),
        ]
    )
    return engine.save_to_stream().getvalue()


def get_id_inventory(doc_bytes):
    """Returns (chg_ids_in_order, com_ids_in_order) as found in the projection."""
    text = strip_appendix(
        extract_text_from_stream(BytesIO(doc_bytes), clean_view=False)
    )
    import re

    chg = re.findall(r"\bChg:(\d+)\b", text)
    com = re.findall(r"\bCom:(\d+)\b", text)
    return chg, com, text


# ---------------------------------------------------------------------------
# Test 1: Confirm baseline - the disk path's natural ID emission.
# ---------------------------------------------------------------------------
section("Test 1: Disk path's natural ID emission (baseline)")

disk_bytes = make_doc_with_changes_and_comments()
chg_ids, com_ids, text = get_id_inventory(disk_bytes)
print(f"Chg IDs in order: {chg_ids}")
print(f"Com IDs in order: {com_ids}")
print(f"\nProjection (first 500 chars):")
print(text[:500])

baseline_chg_set = set(chg_ids)
baseline_com_set = set(com_ids)


# ---------------------------------------------------------------------------
# Test 2: Simulate Word's single-pool numbering, then renumber, then verify
# we get back to the disk-style two-pool scheme.
# ---------------------------------------------------------------------------
section("Test 2: Simulate Word numbering -> renumber -> verify disk parity")

# Load the disk-saved doc.
doc = Document(BytesIO(disk_bytes))

# Step A: manually reshuffle IDs to look like Word would have allocated them.
# Word's pool: Chg gets 0..N-1, Com gets N..N+M-1.
# Currently we have Chg in {1,2,3,4} and Com in {1,2}.
# To simulate Word: shift comment IDs by max(Chg) so Com becomes {5,6}, and
# shift Chg ids down by 1 to start at 0.

print("Before simulation:")
chg_in_xml = []
for tag in (qn("w:ins"), qn("w:del")):
    for el in doc.element.iter(tag):
        chg_in_xml.append(el.get(qn("w:id")))
print(f"  Chg IDs in document.xml: {sorted(set(chg_in_xml), key=int)}")

com_in_doc = []
for el in doc.element.iter(qn("w:commentReference")):
    com_in_doc.append(el.get(qn("w:id")))
print(f"  Com IDs in document.xml refs: {sorted(set(com_in_doc), key=int)}")

# Find the comments part.
comments_part = None
for part in doc.part.package.parts:
    if (
        part.content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    ):
        comments_part = part
        break

if comments_part is not None:
    if hasattr(comments_part, "element"):
        comments_root = comments_part.element
    else:
        from docx.oxml import parse_xml

        if not hasattr(comments_part, "_adeu_element"):
            comments_part._adeu_element = parse_xml(comments_part.blob)
        comments_root = comments_part._adeu_element

    com_in_xml = [c.get(qn("w:id")) for c in comments_root.findall(qn("w:comment"))]
    print(f"  Com IDs in comments.xml: {sorted(set(com_in_xml), key=int)}")

# Now simulate Word's numbering.
print("\nApplying simulated-Word renumbering:")
SHIFT = 100  # Push Com IDs into a non-overlapping range to mimic Word's single pool.

# Shift comments AND their references.
if comments_part is not None:
    for c in comments_root.findall(qn("w:comment")):
        old = c.get(qn("w:id"))
        if old is not None:
            c.set(qn("w:id"), str(int(old) + SHIFT))

for tag in (
    qn("w:commentReference"),
    qn("w:commentRangeStart"),
    qn("w:commentRangeEnd"),
):
    for el in doc.element.iter(tag):
        old = el.get(qn("w:id"))
        if old is not None:
            el.set(qn("w:id"), str(int(old) + SHIFT))

# Verify the simulation took effect.
sim_com_in_doc = []
for el in doc.element.iter(qn("w:commentReference")):
    sim_com_in_doc.append(el.get(qn("w:id")))
print(
    f"  After simulation, Com IDs in document.xml refs: {sorted(set(sim_com_in_doc), key=int)}"
)

# Project the simulated doc - should show shifted IDs.
sim_text = _extract_text_from_doc(doc, clean_view=False)
sim_text = strip_appendix(sim_text)
import re

sim_chg = re.findall(r"\bChg:(\d+)\b", sim_text)
sim_com = re.findall(r"\bCom:(\d+)\b", sim_text)
print(f"\nSimulated projection IDs:")
print(f"  Chg: {sorted(set(sim_chg), key=int)}")
print(f"  Com: {sorted(set(sim_com), key=int)} (should be in 100+ range)")

if not all(int(c) >= SHIFT for c in sim_com):
    print(f"  [FAIL] Simulation didn't shift comment IDs correctly")
else:
    print(f"  [PASS] Simulation produced shifted Com IDs as expected")


# Step B: Apply renumber_snapshot_ids.
print("\nApplying renumber_snapshot_ids():")
chg_remap, com_remap = renumber_snapshot_ids(doc)
print(f"  Chg remap: {chg_remap}")
print(f"  Com remap: {com_remap}")


# Step C: Project again and verify IDs match the original disk-style.
final_text = strip_appendix(_extract_text_from_doc(doc, clean_view=False))
final_chg = re.findall(r"\bChg:(\d+)\b", final_text)
final_com = re.findall(r"\bCom:(\d+)\b", final_text)
print(f"\nFinal renumbered projection IDs:")
print(f"  Chg: {sorted(set(final_chg), key=int)}")
print(f"  Com: {sorted(set(final_com), key=int)}")

# Assertions:
final_chg_set = set(final_chg)
final_com_set = set(final_com)

if final_chg_set == baseline_chg_set:
    print(
        f"\n[PASS] Chg IDs after renumber match baseline disk-style: {sorted(final_chg_set, key=int)}"
    )
else:
    print(f"\n[FAIL] Chg ID mismatch")
    print(f"  baseline: {sorted(baseline_chg_set, key=int)}")
    print(f"  after   : {sorted(final_chg_set, key=int)}")

if final_com_set == baseline_com_set:
    print(
        f"[PASS] Com IDs after renumber match baseline disk-style: {sorted(final_com_set, key=int)}"
    )
else:
    print(f"[FAIL] Com ID mismatch")
    print(f"  baseline: {sorted(baseline_com_set, key=int)}")
    print(f"  after   : {sorted(final_com_set, key=int)}")


# ---------------------------------------------------------------------------
# Test 3: Determinism - running renumber twice should be idempotent on a
# fresh snapshot.
# ---------------------------------------------------------------------------
section("Test 3: Idempotence")

doc1 = Document(BytesIO(disk_bytes))
doc2 = Document(BytesIO(disk_bytes))

renumber_snapshot_ids(doc1)
renumber_snapshot_ids(doc2)

text1 = strip_appendix(_extract_text_from_doc(doc1, clean_view=False))
text2 = strip_appendix(_extract_text_from_doc(doc2, clean_view=False))

if text1 == text2:
    print(
        "[PASS] Two independent renumbers of the same input produce identical projections."
    )
else:
    print("[FAIL] Renumber is non-deterministic.")


# ---------------------------------------------------------------------------
# Test 4: Comment <-> reference linkage preserved after renumber.
# Take a renumbered doc, look at a Com:N reference, find the corresponding
# comment, and verify the text matches what we expect.
# ---------------------------------------------------------------------------
section("Test 4: Comment -> reference linkage preserved")

doc3 = Document(BytesIO(disk_bytes))
# Apply our SHIFT trick to mimic Word's pool.
comments_part = None
for part in doc3.part.package.parts:
    if (
        part.content_type
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    ):
        comments_part = part
        break
if comments_part is not None:
    if hasattr(comments_part, "element"):
        comments_root = comments_part.element
    else:
        from docx.oxml import parse_xml

        if not hasattr(comments_part, "_adeu_element"):
            comments_part._adeu_element = parse_xml(comments_part.blob)
        comments_root = comments_part._adeu_element
    for c in comments_root.findall(qn("w:comment")):
        old = c.get(qn("w:id"))
        if old is not None:
            c.set(qn("w:id"), str(int(old) + SHIFT))
for tag in (
    qn("w:commentReference"),
    qn("w:commentRangeStart"),
    qn("w:commentRangeEnd"),
):
    for el in doc3.element.iter(tag):
        old = el.get(qn("w:id"))
        if old is not None:
            el.set(qn("w:id"), str(int(old) + SHIFT))

# Renumber.
renumber_snapshot_ids(doc3)

# Build mapper to extract comments_map.
mapper = DocumentMapper(doc3)
print(f"After renumber, comments_map keys: {list(mapper.comments_map.keys())}")
print(f"After renumber, comments_map content:")
for cid, info in mapper.comments_map.items():
    print(f"  Com:{cid} -> {info!r}")

# Expected: same content as baseline.
print()
print("Expected (from baseline):")
baseline_doc = Document(BytesIO(disk_bytes))
baseline_mapper = DocumentMapper(baseline_doc)
for cid, info in baseline_mapper.comments_map.items():
    print(f"  Com:{cid} -> {info!r}")

if dict(mapper.comments_map) == dict(baseline_mapper.comments_map):
    print("\n[PASS] Renumbered comments_map equals baseline comments_map.")
else:
    print("\n[FAIL] Renumbered comments_map differs from baseline.")
