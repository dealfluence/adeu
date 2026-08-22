"""CC-4 — every run knows which content controls enclose it.

The write gates are all the same question in different clothes: "does this
edit's text sit inside control X, and what does X permit?". Answering it needs
a run-to-control mapping, and before CC-4 nothing in either engine had one -
`TextSpan` carries `part_index` for the OPC wall but no control identity.

`ProjectedRun.sdt_stack` (node: `Run.sdtStack`) is that mapping, maintained by
the traversal that already walks into every `w:sdt`. The node twin is
`node/packages/core/src/cc_run_control_identity.test.ts`.

The property these tests exist to defend is the non-obvious one: the stack
tracks **every** control, not only the anchored ones that project `{#cc:N}`
tokens. Anchoring answers "does a token appear in the text"; enclosure answers
"which gates apply". A `sdtContentLocked` picture control projects no token
and is still locked, and a gate that consulted only anchor events would let
edits straight through it.
"""

import io

import pytest
from docx import Document

from adeu.utils.content_controls import assign_ordinals
from adeu.utils.docx import ProjectedRun, iter_paragraph_content
from tests.sdt_fixtures import build_sdt_docx, run


def _sdt(inner: str, *, cls_xml: str, lock: str | None = None, sdt_id: int = 900) -> str:
    lock_xml = f'<w:lock w:val="{lock}"/>' if lock else ""
    return (
        f'<w:sdt><w:sdtPr><w:id w:val="{sdt_id}"/><w:tag w:val="t{sdt_id}"/>'
        f"{lock_xml}{cls_xml}</w:sdtPr><w:sdtContent>{inner}</w:sdtContent></w:sdt>"
    )


#: A picture control: UNANCHORED, so it emits no `sdt_start`/`sdt_end` at all.
_PICTURE = "<w:picture/>"
#: A group: anchored, and the wrapper G3 gates on.
_GROUP = "<w:group/>"
#: A plain text control: anchored.
_TEXT = "<w:text/>"


@pytest.fixture
def runs_for(tmp_path):
    """Every ProjectedRun the traversal emits for a one-paragraph body.

    `with_infos=False` omits the ordinal map, which is how outline and
    sanitize call the traversal.
    """

    def _build(body: str, *, with_infos: bool = True) -> list[ProjectedRun]:
        path = build_sdt_docx(tmp_path / "identity.docx", body)
        doc = Document(io.BytesIO(path.read_bytes()))
        infos = assign_ordinals([doc.element.body]) if with_infos else None
        return [
            item
            for para in doc.paragraphs
            for item in iter_paragraph_content(para, sdt_infos=infos)
            if isinstance(item, ProjectedRun)
        ]

    return _build


def test_a_run_outside_every_control_has_an_empty_stack(runs_for):
    (only,) = runs_for(f"<w:p>{run('Plain body text.')}</w:p>")
    assert only.proj_text == "Plain body text."
    assert only.sdt_stack == ()


def test_a_run_inside_an_anchored_control_names_it(runs_for):
    body = f"<w:p>{_sdt(run('Inside.'), cls_xml=_TEXT, sdt_id=901)}</w:p>"
    (only,) = runs_for(body)
    assert [i.sdt_id for i in only.sdt_stack] == ["901"]


def test_the_stack_tracks_UNANCHORED_controls_too(runs_for):
    """The point of the whole design.

    A picture control never emits `sdt_start`/`sdt_end` - it is in
    `_UNANCHORED_CLASSES`, so the traversal descends through it transparently
    and no token is projected. A gate driven by anchor events would therefore
    be blind to its lock. The stack is not, because it is maintained
    structurally rather than from the projection.
    """
    body = f"<w:p>{_sdt(run('Caption text.'), cls_xml=_PICTURE, lock='sdtContentLocked', sdt_id=902)}</w:p>"
    (only,) = runs_for(body)
    assert only.proj_text == "Caption text."
    assert [i.sdt_id for i in only.sdt_stack] == ["902"], "an unanchored control still encloses its runs"
    assert only.sdt_stack[0].content_locked is True


def test_nesting_is_outermost_first(runs_for):
    """G1 says "control (or ancestor)" and G3 needs the group/leaf distinction,
    so the ORDER carries meaning: index 0 is the outermost wrapper."""
    inner = _sdt(run("Nested."), cls_xml=_TEXT, sdt_id=904)
    body = f"<w:p>{_sdt(inner, cls_xml=_GROUP, lock='sdtContentLocked', sdt_id=903)}</w:p>"
    (only,) = runs_for(body)
    assert [i.sdt_id for i in only.sdt_stack] == ["903", "904"]
    assert only.sdt_stack[0].cls == "group"


def test_the_stack_is_popped_on_the_way_out(runs_for):
    """A snapshot, not a shared reference. If the stack leaked, the trailing
    run would claim to be inside a control it left."""
    body = f"<w:p>{run('Before ')}{_sdt(run('inside'), cls_xml=_TEXT, sdt_id=905)}{run(' after.')}</w:p>"
    before, inside, after = runs_for(body)
    assert before.sdt_stack == ()
    assert [i.sdt_id for i in inside.sdt_stack] == ["905"]
    assert after.sdt_stack == (), "the control was closed before this run"


def test_omitting_the_ordinal_map_leaves_the_stack_empty(runs_for):
    """Callers that opt out of control awareness (outline, sanitize) must see
    exactly the historical behaviour, stack included."""
    body = f"<w:p>{_sdt(run('Inside.'), cls_xml=_TEXT, sdt_id=906)}</w:p>"
    assert [r.sdt_stack for r in runs_for(body, with_infos=False)] == [()]
