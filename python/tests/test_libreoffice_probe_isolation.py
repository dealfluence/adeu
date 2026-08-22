"""CC-16 -- the LibreOffice interop harness must not lie under xdist.

`test_repro_qa_2026_07_18.py` pins two shipped interop regressions (QA C1, a
body edit written into `word/footer1.xml`; QA H4, a comment anchored in a
footnote) by asking LibreOffice to actually load the produced file. Under
`pytest -n auto` that harness was reporting results that had nothing to do with
the documents under test.

Concurrent `soffice` invocations that share a user profile do not both convert.
The second finds the first one's lock, hands its request over, and **exits 0
having written nothing**. The caller sees a clean exit and a missing PDF, which
is indistinguishable from "LibreOffice rejected this document". The bug
therefore had two faces, and the quiet one is the dangerous one:

* the *probe* loses the race -> `soffice_can_convert()` caches False for that
  whole worker process -> every interop assertion on it **silently skips**;
* the probe wins and a later *conversion* loses -> the test **fails**, blaming
  the document for a scheduling artefact.

Measured on this 28-core machine, `-n auto`, five full-suite runs each:
unfixed, interop tests skipped in four of five runs (1, 0, 2, 1, 2 skips);
fixed, zero skips and zero failures in five of five, with identical counts. So
for most of its life this file's interop coverage was not running, while the
suite reported green.

These tests pin the harness itself, not any QA scenario.
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from docx import Document

from tests import test_repro_qa_2026_07_18 as qa

pytestmark = pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice (soffice) not installed")


def _docx(path: Path, text: str = "hello") -> Path:
    doc = Document()
    doc.add_paragraph(text)
    doc.save(path)
    return path


class TestProfileIsolation:
    """Each xdist worker must get its own LibreOffice user profile."""

    def test_profile_is_private_and_not_the_default(self):
        prof = qa._soffice_profile_dir()
        assert prof.is_dir()
        # The default profile (~/Library/Application Support/LibreOffice,
        # ~/.config/libreoffice) is the shared resource that serialises
        # concurrent instances. Ours must not be under the home directory.
        assert Path.home() not in prof.parents

    def test_profile_is_stable_within_a_process(self):
        # Rebuilding it per conversion would pay the ~3.5s profile build every
        # time instead of ~1.7s warm, and at 12-way concurrency fresh builds
        # pushed past the old 5s timeout and failed every worker at once.
        assert qa._soffice_profile_dir() == qa._soffice_profile_dir()

    def test_distinct_workers_get_distinct_profiles(self, monkeypatch):
        """The actual isolation guarantee, exercised through the real code."""
        seen = set()
        for worker in ("gw0", "gw1", "gw2"):
            monkeypatch.setattr(qa, "_SOFFICE_PROFILE", None)
            monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
            seen.add(qa._soffice_profile_dir())
        assert len(seen) == 3, "workers shared a profile; conversions will race"


class TestHarnessHonesty:
    """`lo_loads` must answer the question the interop tests think it answers."""

    def test_accepts_a_real_docx(self, tmp_path):
        assert qa.lo_loads(_docx(tmp_path / "good.docx"), tmp_path) is True

    @pytest.mark.parametrize("kind", ["not_a_zip", "truncated", "malformed_xml"])
    def test_rejects_documents_word_cannot_load(self, tmp_path, kind):
        """Without a pinned filter LibreOffice sniffs content and falls back.

        A `.docx` whose entire body was the bytes "this is definitely not a
        zip" was imported as PLAIN TEXT, converted to a perfectly good PDF, and
        reported by the harness as loading correctly -- so the interop tests
        could have passed on a file Word would refuse outright.
        """
        good = _docx(tmp_path / "src.docx")
        bad = tmp_path / f"{kind}.docx"
        if kind == "not_a_zip":
            bad.write_bytes(b"this is definitely not a zip")
        elif kind == "truncated":
            raw = good.read_bytes()
            bad.write_bytes(raw[: len(raw) // 2])
        else:
            with zipfile.ZipFile(good) as src, zipfile.ZipFile(bad, "w") as out:
                for item in src.infolist():
                    data = src.read(item.filename)
                    if item.filename == "word/document.xml":
                        data = b"<w:document><unclosed>"
                    out.writestr(item, data)
        assert qa.lo_loads(bad, tmp_path) is False


class TestNoSilentSkip:
    """The quiet face of the bug: coverage disappearing into a skip."""

    def test_probe_succeeds_when_soffice_is_installed(self, tmp_path):
        # `soffice` is on PATH (module-level skipif), so a False here means the
        # probe lost a race, and every interop assertion in this run silently
        # vanished rather than failing.
        assert qa.soffice_can_convert(tmp_path) is True

    def test_concurrent_workers_all_convert(self, tmp_path):
        """End-to-end reproduction across real processes.

        Threads would not do: the profile is per-process by design, so
        in-process concurrency shares it legitimately. The bug lives between
        xdist worker *processes*, so this spawns them.

        Three is the cheapest reliable reproduction -- sharing one profile gave
        2/3 on every trial measured, while 2-way only failed 2 of 3 times.
        """
        src = _docx(tmp_path / "shared.docx")
        script = tmp_path / "worker.py"
        script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
            "from tests import test_repro_qa_2026_07_18 as qa\n"
            "out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)\n"
            "ok = qa.lo_loads(Path(sys.argv[1]), out)\n"
            "sys.exit(0 if ok else 1)\n"
        )
        procs = []
        for i in range(3):
            env = dict(os.environ, PYTEST_XDIST_WORKER=f"ccgw{i}")
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(script), str(src), str(tmp_path / f"out{i}")],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        # A literal, not qa.LO_TIMEOUT_SECONDS: this test must observe the race
        # itself, not die on a missing constant if the fix is reverted.
        codes = [p.wait(timeout=180) for p in procs]
        assert codes == [0, 0, 0], (
            f"concurrent conversions of a known-good file disagreed: {codes}; "
            "this is the shared-profile race, not a document defect"
        )
