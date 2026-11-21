"""Filesystem fixtures shared by the Python MCP and LangChain policy tests."""

import os
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


def prepare_directory_link(root: Path, case: dict) -> None:
    if "link" in case:
        name, destination = case["link"]
        actual = root / destination
        actual.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(actual), str(root / name))
        else:
            (root / name).symlink_to(actual, target_is_directory=True)


def check_confinement_case(root: Path, case: dict) -> None:
    from adeu.path_policy import AllowedDirectories, PathPolicyError

    for name in ("allowed", "second", "outside", "allowed2", "Café"):
        (root / name).mkdir()
    prepare_directory_link(root, case)
    for name in case.get("existing", ()):
        shutil.copyfile(BASE_DOCX, root / name)
    roots = [root / name for name in case["roots"]]
    policy = AllowedDirectories.from_value(os.pathsep.join(map(str, roots)))
    paths = [root / name for name in case["paths"]]
    allow = case["allow"]
    if allow in ("case_alias", "unicode_alias"):
        alias = paths[0].parent
        allow = alias.exists() and alias.samefile(roots[0])
    try:
        for path in paths:
            policy.check(path)
    except PathPolicyError as exc:
        assert not allow
        assert str(exc) == f"File access refused. Allowed folders: {os.pathsep.join(str(p.resolve()) for p in roots)}"
    else:
        assert allow


def check_startup_case(root: Path, case: dict) -> None:
    from adeu.path_policy import AllowedDirectories, PathPolicyError

    (root / "allowed").mkdir()
    (root / "second").mkdir()
    (root / "file.docx").touch()
    prepare_directory_link(root, case)
    entries = case["entries"]
    if entries is None:
        assert not AllowedDirectories.from_value(None).enabled
        return
    entries = [str(root / entry[6:]) if entry.startswith("$root/") else entry for entry in entries]
    expected = {
        "empty": "ADEU_ALLOWED_DIRS is set but contains no folders.",
        "relative": "ADEU_ALLOWED_DIRS entry 'relative' is relative. Use an absolute folder path.",
        "missing": f"ADEU_ALLOWED_DIRS entry '{entries[-1]}' does not exist.",
        "file": f"ADEU_ALLOWED_DIRS entry '{root / 'file.docx'}' is not a folder.",
    }
    try:
        policy = AllowedDirectories.from_value(os.pathsep.join(entries))
    except PathPolicyError as exc:
        assert str(exc) == expected[case["error"]]
    else:
        assert "error" not in case
        effective = [root / e[6:] for e in case["resolved"]] if "resolved" in case else entries
        assert policy.roots == tuple(Path(entry).resolve() for entry in effective if str(entry).strip())
