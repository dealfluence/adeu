"""Automated projection-golden gate.

docs/PERFORMANCE.md §2 requires proving the projection is byte-unchanged
before/after performance work. Historically that was a manual script run and
the evidence lived only in a commit message, so the claim was not re-checkable
later. This test makes it a standing gate against the COMMITTED hashes in
tests/golden_manifest.txt.

Both inputs are synthetic and run on every checkout: cells exercises anchors,
nested tables, headings, and styling; revisions exercises tracked deletions
and insertions with distinct raw/clean views. No personal files are discovered.

The view computation itself is imported from scripts/golden_projection.py so
the test and the CLI can never disagree. compute_views() also asserts the twin
contract and the paragraph-offset invariant, meaning those are checked here
even for rows whose hashes this machine cannot verify.
"""

import hashlib
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import golden_projection as gp  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "golden_manifest.txt"


def _check(name: str) -> int:
    """Recompute every view for `name` and assert each hash matches."""
    expected = gp.load_manifest(MANIFEST)
    sanitized = gp.strip_bom_from_docx_bytes(gp.load_bytes(name, None))

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
    assert {key.split(".", 1)[0] for key in expected} == {"cells", "revisions"}
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


@pytest.mark.parametrize("name", ["cells", "revisions"])
def test_projection_goldens(name):
    """All seven views of each portable input must match committed hashes."""
    assert _check(name) == 7


def test_revisions_fixture_is_deterministic_and_tracks_changes():
    first = gp.compute_views("revisions", gp.build_revisions_fixture())
    second = gp.compute_views("revisions", gp.build_revisions_fixture())
    assert first == second
    assert first["reader_raw"] != first["reader_clean"]
    assert "removed wording" in first["reader_raw"]
    assert "removed wording" not in first["reader_clean"]
    assert "added wording" in first["reader_clean"]
    assert first["reader_raw"] == first["mapper_raw"]
    assert first["reader_clean"] == first["mapper_clean"]


def test_default_projection_inputs_are_portable():
    assert {name for name, _ in gp.DOCS} == {"cells", "revisions"}
    assert all(path is None for _, path in gp.DOCS)


def test_default_cli_ignores_home_documents(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "BIGDOC.docx").write_bytes(b"not a DOCX; never inspect implicitly")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert gp.main(["verify"]) == 0


def test_private_verify_requires_manifest(tmp_path):
    document = tmp_path / "sample.docx"
    document.write_bytes(gp.build_revisions_fixture())
    with pytest.raises(SystemExit) as error:
        gp.main(["verify", "--document", str(document)])
    assert error.value.code == 2


def _capture_private(tmp_path):
    document = tmp_path / "sample.docx"
    original = gp.build_revisions_fixture()
    document.write_bytes(original)
    baseline = tmp_path / "baseline"
    assert gp.main(["capture", str(baseline), "--document", str(document)]) == 0
    assert document.read_bytes() == original
    assert (baseline / "INPUT_SHA256.txt").read_text().strip() == hashlib.sha256(original).hexdigest()
    return document, baseline


def _verify_private(document, baseline):
    return gp.main(["verify", "--document", str(document), "--manifest", str(baseline / "MANIFEST.txt")])


def test_private_capture_detects_changed_input(tmp_path, capsys):
    document, baseline = _capture_private(tmp_path)
    assert _verify_private(document, baseline) == 0
    initial = capsys.readouterr()
    assert str(document) not in initial.out + initial.err
    assert document.name not in initial.out + initial.err
    document.write_bytes(gp.build_cells_fixture())
    assert _verify_private(document, baseline) == 1
    captured = capsys.readouterr()
    assert "private input differs from captured baseline" in captured.out + captured.err


def test_private_bad_output_hash_still_fails(tmp_path, capsys):
    document, baseline = _capture_private(tmp_path)
    original = document.read_bytes()
    manifest = baseline / "MANIFEST.txt"
    rows = manifest.read_text(encoding="utf-8").splitlines()
    rows[0] = "0" * 64 + rows[0][64:]
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    capsys.readouterr()
    assert _verify_private(document, baseline) == 1
    captured = capsys.readouterr()
    assert "private.reader_raw.txt" in captured.out + captured.err
    assert "private input differs" not in captured.out + captured.err
    assert document.read_bytes() == original


@pytest.mark.parametrize(
    "corruption", ["empty", "missing", "unknown", "duplicate", "bad-hash", "bad-size", "traversal"]
)
def test_private_manifest_corruption_fails(tmp_path, corruption):
    document, baseline = _capture_private(tmp_path)
    manifest = baseline / "MANIFEST.txt"
    rows = manifest.read_text(encoding="utf-8").splitlines()
    if corruption == "empty":
        rows = []
    elif corruption == "missing":
        rows.pop()
    elif corruption == "unknown":
        rows[0] = rows[0].replace("private.", "unknown.")
    elif corruption == "duplicate":
        rows.append(rows[0])
    elif corruption == "bad-hash":
        rows[0] = "x" + rows[0][1:]
    elif corruption == "bad-size":
        sha, _, name = rows[0].split()
        rows[0] = f"{sha}  -1  {name}"
    else:
        rows[0] = rows[0].replace("private.reader_raw.txt", "../../outside.txt")
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert _verify_private(document, baseline) == 1


@pytest.mark.parametrize("fingerprint", [None, "", "x" * 64, "0" * 63])
def test_private_fingerprint_is_required_and_validated(tmp_path, fingerprint):
    document, baseline = _capture_private(tmp_path)
    sidecar = baseline / "INPUT_SHA256.txt"
    if fingerprint is None:
        sidecar.unlink()
    else:
        sidecar.write_text(fingerprint, encoding="ascii")
    assert _verify_private(document, baseline) == 1


def test_explicit_missing_private_document_fails_without_path_disclosure(tmp_path, capsys):
    missing = tmp_path / "absent.docx"
    assert gp.main(["capture", str(tmp_path / "baseline"), "--document", str(missing)]) == 1
    captured = capsys.readouterr()
    assert str(missing) not in captured.out + captured.err
    assert missing.name not in captured.out + captured.err


def test_private_document_is_read_once_for_capture(tmp_path, monkeypatch):
    document = tmp_path / "sample.docx"
    original = gp.build_revisions_fixture()
    document.write_bytes(original)
    read_bytes = Path.read_bytes
    reads = []

    def read_once(path):
        if path == document:
            reads.append(path)
            assert len(reads) == 1, "input reread could fingerprint different bytes from the projection"
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    baseline = tmp_path / "baseline"
    assert gp.main(["capture", str(baseline), "--document", str(document)]) == 0
    assert reads == [document]
    assert (baseline / "INPUT_SHA256.txt").read_text().strip() == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("filename", ["private.reader_raw.txt", "MANIFEST.txt", "INPUT_SHA256.txt"])
def test_capture_never_overwrites_its_input(tmp_path, filename):
    # Content, not extension, determines whether an input is a DOCX.
    document = tmp_path / filename
    original = gp.build_revisions_fixture()
    document.write_bytes(original)
    assert gp.main(["capture", str(tmp_path), "--document", str(document)]) == 1
    assert document.read_bytes() == original


def test_private_compare_checks_input_identity(tmp_path):
    document, baseline = _capture_private(tmp_path)
    second = tmp_path / "second"
    assert gp.main(["capture", str(second), "--document", str(document)]) == 0
    assert gp.main(["compare", str(baseline), str(second)]) == 0
    document.write_bytes(gp.build_cells_fixture())
    assert gp.main(["capture", str(second), "--document", str(document)]) == 0
    assert gp.main(["compare", str(baseline), str(second)]) == 1


def test_empty_comparison_is_not_success(tmp_path):
    (tmp_path / "MANIFEST.txt").write_text("", encoding="utf-8")
    assert gp.main(["compare", str(tmp_path), str(tmp_path)]) == 1


def test_bad_portable_hash_still_fails(tmp_path):
    rows = MANIFEST.read_text(encoding="utf-8").splitlines()
    rows[0] = "0" * 64 + rows[0][64:]
    manifest = tmp_path / "MANIFEST.txt"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert gp.main(["verify", "--manifest", str(manifest)]) == 1


def test_verify_cannot_succeed_without_processing_inputs(monkeypatch):
    monkeypatch.setattr(gp, "iter_documents", lambda document=None: iter(()))
    assert gp.main(["verify"]) == 1
