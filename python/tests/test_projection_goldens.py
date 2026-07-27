"""Automated projection-golden gate.

docs/PERFORMANCE.md §2 requires proving the projection is byte-unchanged
before/after performance work. Historically that was a manual script run and
the evidence lived only in a commit message, so the claim was not re-checkable
later. This test makes it a standing gate against the COMMITTED hashes in
tests/golden_manifest.txt.

Coverage tiers, by what each machine actually has:

  cells   synthetic fixture built in-process — runs EVERYWHERE, so this is the
          real CI gate. Exercises the cell-anchor branches (§3.6) plus a
          heading, a nested table, and bold/italic runs.
  BIGDOC  0.4 MB control document with tracked changes (raw != clean view).
          Skipped when absent. ~2.5 s.
  VVBIG   9.3 MB stress document. Skipped unless ADEU_GOLDEN_SLOW=1, because
          all seven views cost ~60 s and the default suite runs in ~30 s.

Both non-synthetic documents live on a developer Desktop, not in the repo, so
a fresh clone still gets a meaningful (cells-only) gate.

The view computation itself is imported from scripts/golden_projection.py so
the test and the CLI can never disagree. compute_views() also asserts the twin
contract and the paragraph-offset invariant, meaning those are checked here
even for rows whose hashes this machine cannot verify.
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import golden_projection as gp  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "golden_manifest.txt"


def _paths_by_name():
    return {name: path for name, path in gp.DOCS}


def _check(name: str) -> int:
    """Recompute every view for `name` and assert each hash matches."""
    expected = gp.load_manifest(MANIFEST)
    path = _paths_by_name()[name]
    sanitized = gp.strip_bom_from_docx_bytes(gp.load_bytes(name, path))

    # compute_views asserts the twin contract + offset invariant internally.
    views = gp.compute_views(name, sanitized)
    assert views, f"{name}: no views computed"

    checked = 0
    for view, text in views.items():
        key = f"{name}.{view}.txt"
        assert key in expected, (
            f"{key} missing from {MANIFEST.name}. If a view was added "
            f"deliberately, regenerate the manifest (see golden_projection.py)."
        )
        got = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert got == expected[key], (
            f"{key} CHANGED: manifest {expected[key][:16]} != computed "
            f"{got[:16]} ({len(text)} chars). The projection is a contract "
            f"with downstream agents (anchors/ids/offsets) — if this change "
            f"is intended, regenerate the manifest and say so in the commit."
        )
        checked += 1
    return checked


def test_manifest_is_parseable_and_covers_every_view():
    expected = gp.load_manifest(MANIFEST)
    assert expected, "golden manifest is empty"
    views = {k.split(".", 1)[1] for k in expected}
    assert views == {
        "reader_raw.txt",
        "reader_clean.txt",
        "reader_appendix.txt",
        "mapper_raw.txt",
        "mapper_clean.txt",
        "outline.txt",
        "pagination.txt",
    }, f"unexpected view set in manifest: {sorted(views)}"
    # Every hash must be a full sha256, or a truncated paste would silently
    # weaken the gate.
    for key, sha in expected.items():
        assert len(sha) == 64, f"{key}: hash is not a full sha256 ({sha!r})"


def test_projection_goldens_cells():
    """Synthetic fixture — the portable gate; must run on every machine."""
    assert _check("cells") == 7


@pytest.mark.skipif(
    not (_paths_by_name()["BIGDOC"] or Path("/nonexistent")).exists(),
    reason="BIGDOC control document not present on this machine",
)
def test_projection_goldens_bigdoc():
    """0.4 MB control with tracked changes — guards against regressing small
    documents while optimizing for the stress document."""
    assert _check("BIGDOC") == 7


@pytest.mark.skipif(
    not (_paths_by_name()["VVBIG"] or Path("/nonexistent")).exists(),
    reason="VVBIG stress document not present on this machine",
)
@pytest.mark.skipif(
    os.environ.get("ADEU_GOLDEN_SLOW") != "1",
    reason="VVBIG goldens cost ~60s; set ADEU_GOLDEN_SLOW=1 to run",
)
def test_projection_goldens_vvbig():
    assert _check("VVBIG") == 7
