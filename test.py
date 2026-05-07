# FILE: repro_bug1.py
"""
Reproduction script for Bug 1: accept_all_revisions() does not remove comments.

The failing test (test_issue_10_accept_all_changes_removes_comments):
  1. Creates a doc with one paragraph.
  2. Adds a comment via process_batch with a same-text 'modify' that carries
     a `comment` parameter (routes through COMMENT_ONLY).
  3. Asserts comments_manager.extract_comments_data() returns 1 entry.
  4. Calls accept_all_revisions().
  5. Asserts the comment count drops to 0.

Current behavior: accept_all_revisions only sweeps w:ins / w:del / paragraph
del markers in document.xml. It never touches comments.xml or the
commentRangeStart/End/Reference anchors in the body, so comment data survives.

This script:
  - Reproduces the test scenario exactly.
  - Dumps comment state BEFORE accept_all_revisions (count + raw comment IDs
    in comments.xml + body-side anchor counts).
  - Calls accept_all_revisions().
  - Dumps comment state AFTER.
  - Reports whether the comment data and anchors are gone.
"""

import io

from docx import Document
from docx.oxml.ns import qn

from adeu.models import ModifyText
from adeu.redline.engine import RedlineEngine


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def dump_comment_state(engine: RedlineEngine, label: str) -> None:
    section(label)

    # 1. Comments via the manager (the test's lens)
    comments = engine.comments_manager.extract_comments_data()
    print(f"comments_manager.extract_comments_data() -> {len(comments)} entries")
    for cid, data in comments.items():
        print(f"  Com:{cid}  author={data['author']!r}  text={data['text']!r}")

    # 2. Raw <w:comment> entries in comments.xml
    comments_part = engine.comments_manager._get_existing_part_by_type(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    )
    if comments_part is None:
        print("comments.xml part: <not present>")
    else:
        if hasattr(comments_part, "element"):
            root = comments_part.element
        else:
            from docx.oxml import parse_xml

            root = parse_xml(comments_part.blob)
        raw_comments = root.findall(qn("w:comment"))
        print(f"comments.xml <w:comment> elements: {len(raw_comments)}")
        for c in raw_comments:
            cid = c.get(qn("w:id"))
            author = c.get(qn("w:author"))
            print(f"  raw w:id={cid}  author={author!r}")

    # 3. Body-side anchors
    body = engine.doc.element
    starts = body.findall(f".//{qn('w:commentRangeStart')}")
    ends = body.findall(f".//{qn('w:commentRangeEnd')}")
    refs = body.findall(f".//{qn('w:commentReference')}")
    print(
        f"body anchors: {len(starts)} commentRangeStart, "
        f"{len(ends)} commentRangeEnd, {len(refs)} commentReference"
    )
    for s in starts:
        print(f"  commentRangeStart w:id={s.get(qn('w:id'))}")
    for e in ends:
        print(f"  commentRangeEnd   w:id={e.get(qn('w:id'))}")
    for r in refs:
        print(f"  commentReference  w:id={r.get(qn('w:id'))}")


def main():
    # --- Build the same doc the failing test builds ---
    doc = Document()
    doc.add_paragraph("Text with comment.")
    stream = io.BytesIO()
    doc.save(stream)
    stream.seek(0)

    engine = RedlineEngine(stream)

    # --- Step 1: attach a comment via a same-text modify (COMMENT_ONLY path) ---
    section("STEP 1 — attach comment via process_batch")
    result = engine.process_batch(
        [ModifyText(target_text="comment", new_text="comment", comment="QA Comment")]
    )
    print(f"process_batch stats: {result}")

    # --- Step 2: state BEFORE accept_all_revisions ---
    dump_comment_state(engine, "STATE BEFORE accept_all_revisions")

    pre_count = len(engine.comments_manager.extract_comments_data())

    # --- Step 3: accept_all_revisions ---
    section("STEP 3 — call accept_all_revisions()")
    engine.accept_all_revisions()
    print("accept_all_revisions() returned")

    # --- Step 4: state AFTER ---
    dump_comment_state(engine, "STATE AFTER accept_all_revisions")

    post_count = len(engine.comments_manager.extract_comments_data())

    # --- Step 5: test assertion preview ---
    section("TEST ASSERTION PREVIEW")
    print(f"comments before: {pre_count}")
    print(f"comments after:  {post_count}")
    pre_marker = "PASS" if pre_count == 1 else "FAIL"
    post_marker = "PASS" if post_count == 0 else "FAIL"
    print(f"  [{pre_marker}] expected 1 comment before accept_all_revisions")
    print(
        f"  [{post_marker}] expected 0 comments after accept_all_revisions  (THE BUG)"
    )


if __name__ == "__main__":
    main()
