"""Filesystem fixtures shared by the Python MCP and LangChain policy tests."""

import shutil
from pathlib import Path
from unittest import SkipTest
from zipfile import ZipFile

BASE_DOCX = Path(__file__).resolve().parents[1] / "fixtures" / "base.docx"


def prepare_case(root: Path, case: dict) -> tuple[Path, list[Path]]:
    inputs = [root / name for name in case["inputs"]]
    for source in inputs:
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BASE_DOCX, source)

    target = root / case["target"]
    actual = root / case.get("symlink", case["target"])
    kind = case["kind"]
    if kind != "absent":
        actual.parent.mkdir(parents=True, exist_ok=True)
        if kind == "word":
            shutil.copyfile(BASE_DOCX, actual)
        elif kind == "other_zip":
            with ZipFile(actual, "w") as archive:
                archive.writestr("[Content_Types].xml", "")
                archive.writestr("xl/workbook.xml", "")
        elif kind == "text":
            actual.write_bytes(b"Existing contents")
        elif kind == "empty":
            actual.touch()
        elif kind == "directory":
            actual.mkdir()
        else:
            raise ValueError(f"Unknown path policy fixture: {kind}")
    if "symlink" in case:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.symlink_to(actual)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                raise SkipTest("Windows file symlink privilege is unavailable") from exc
            raise
    return target, inputs
