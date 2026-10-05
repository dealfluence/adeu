"""
ooxml-integrity checks on the outputs of the shared cross-platform scenarios.

For each folder under shared/cross_platform_tests/ that writes a document, this
applies the changes with the Python RedlineEngine, as
test_cross_platform_consistency.py does, then runs ooxml-integrity on the
output: a structural check of the package, and a comparison with input.docx
that reports lost comment anchors, footnote references, tracked changes and
other constructs. Only error-level findings fail.

Accept/reject scenarios remove revisions on purpose. EXPECTED_REVISION_CHANGES
lists the revision-count changes each one requests, declared to ooxml-integrity
as expectations: a requested change that does not happen fails (EXP001), any
other loss still fails, and a new accept/reject scenario declares its own.
"""

import io
import json
from pathlib import Path

import pytest
from ooxml_integrity import Expectation, Severity, check, compare, expect

from adeu.models import (
    AcceptChange,
    DeleteTableRow,
    InsertTableRow,
    ModifyText,
    RejectChange,
    ReplyComment,
)
from adeu.redline.engine import RedlineEngine

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CORPUS_DIR = REPO_ROOT / "shared" / "cross_platform_tests"

MODEL_MAP = {
    "modify": ModifyText,
    "accept": AcceptChange,
    "reject": RejectChange,
    "reply": ReplyComment,
    "insert_row": InsertTableRow,
    "delete_row": DeleteTableRow,
}

# (tag, count before, count after) of each FID001 revision-count change requested.
EXPECTED_REVISION_CHANGES = {
    "10_agent_counsel_accept_reject": {("ins", 2, 0), ("del", 1, 0)},
    "11_agent_table_accept_reject": {("ins", 11, 9), ("del", 3, 1)},
}


def _written_scenarios():
    if not CORPUS_DIR.exists():
        return []
    cases = []
    for test_dir in sorted(CORPUS_DIR.iterdir()):
        test_json = test_dir / "test.json"
        if not test_json.exists() or not (test_dir / "input.docx").exists():
            continue
        if json.loads(test_json.read_text(encoding="utf-8")).get("read_only", False):
            continue
        cases.append(pytest.param(test_dir, id=test_dir.name))
    return cases


def _expectations(name):
    """The scenario's requested revision-count changes as ooxml-integrity expectations."""
    return [
        Expectation("FID001", {"tag": tag, "before": before, "after": after}, reason=f"{name} accepts or rejects")
        for tag, before, after in sorted(EXPECTED_REVISION_CHANGES.get(name, set()))
    ]


@pytest.mark.parametrize("test_dir", _written_scenarios())
def test_scenario_output_integrity(test_dir: Path, tmp_path: Path):
    cfg = json.loads((test_dir / "test.json").read_text(encoding="utf-8"))
    changes = [MODEL_MAP[c["type"]](**{k: v for k, v in c.items() if k != "type"}) for c in cfg.get("changes", [])]
    source = test_dir / "input.docx"
    engine = RedlineEngine(io.BytesIO(source.read_bytes()), author=cfg.get("author", "Adeu AI"))
    engine.process_batch(changes)
    output = tmp_path / "output.docx"
    output.write_bytes(engine.save_to_stream().getvalue())

    kept, _ = expect(check(output) + compare(source, output), _expectations(test_dir.name))
    errors = [f for f in kept if f.severity is Severity.ERROR]
    assert not errors, f"[{test_dir.name}] ooxml-integrity errors:\n" + "\n".join(str(f) for f in errors)
