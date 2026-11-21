"""Path checks for LLM-facing tools. Checking a path never creates it."""

import ntpath
import os
import re
import stat
from pathlib import Path
from zipfile import BadZipFile, ZipFile

_OPEN_ENDINGS = {".docx", ".docm", ".dotx", ".dotm"}
_SIBLING_ONLY = {".docm", ".dotm", ".dotx", ".dot"}
_WORD_ENDINGS = _OPEN_ENDINGS | {".doc", ".dot"}
_WORD_PARTS = {"[Content_Types].xml", "word/document.xml"}
_DEVICE = re.compile(r"^(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[1-9¹²³])$", re.I)


class PathPolicyError(ValueError):
    """A tool path could not be used safely."""


def _check_windows_name(raw: str) -> str:
    # Validate the spelling before Win32 can discard trailing dots/spaces or
    # interpret an alternate data stream, DOS device, or named pipe as a file.
    name = raw.replace("/", "\\")
    if name.lower().startswith("\\\\?\\unc\\"):
        name = "\\\\" + name[8:]
    elif re.match(r"^\\\\\?\\[a-z]:\\", name, re.I):
        name = name[4:]
    if name.startswith(("\\\\.\\", "\\\\?\\", "\\??\\")):
        raise PathPolicyError(f"Refused '{raw}': device path. Use an ordinary Word file path.")
    drive, tail = ntpath.splitdrive(name)
    if drive.startswith("\\\\") and drive.rsplit("\\", 1)[-1].lower() in {"pipe", "mailslot"}:
        raise PathPolicyError(f"Refused '{raw}': device path. Use an ordinary Word file path.")
    if ":" in tail or (":" in drive and not re.fullmatch("[A-Za-z]:", drive)):
        raise PathPolicyError(f"Refused '{raw}': colon after the drive prefix. Use an ordinary Word file path.")
    components = name if drive.startswith("\\\\") else tail
    for part in components.split("\\"):
        if part in {"", ".", ".."}:
            continue
        if part.endswith((".", " ")):
            raise PathPolicyError(f"Refused '{raw}': name ends in a dot or space. Use an ordinary Word file path.")
        if _DEVICE.fullmatch(part.partition(".")[0].rstrip(" ")):
            raise PathPolicyError(f"Refused '{raw}': reserved device name. Use an ordinary Word file path.")
    return name


def _target_stat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PathPolicyError(f"Refused '{path}': cannot inspect target. Choose an accessible file path.") from exc


def _canonical_path(name: str, links: int = 0) -> Path:
    # Resolve links before "..", including when the link refers to a new file.
    # Win32 realpath alone normalizes ".." before following junctions.
    path = Path(_check_windows_name(name) if os.name == "nt" else name).absolute()
    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        if part == "..":
            current = current.parent
            continue
        current /= part
        info = _target_stat(current)
        if info is None:
            continue
        if stat.S_ISLNK(info.st_mode) or (os.name == "nt" and info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT):
            if links >= 40:
                raise PathPolicyError(f"Refused '{name}': too many symbolic links. Use a direct file path.")
            current = _canonical_path(str(current.parent / current.readlink()), links + 1)
            info = _target_stat(current)
        elif os.name == "nt" and (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            # Expand 8.3 names and recover the on-disk spelling.
            current = Path(_check_windows_name(str(current.resolve(strict=True))))
        if index < len(parts) - 1 and info is not None and not stat.S_ISDIR(info.st_mode):
            raise PathPolicyError(f"Refused '{name}': invalid parent folder. Choose an accessible folder.")
    return current


def resolved_input_path(target: str | Path) -> Path:
    """Resolve an input without imposing write or desktop-launch requirements."""
    name = os.fspath(target)
    if not name or "\0" in name:
        raise PathPolicyError(f"Refused {name!r}: invalid path. Supply a file name.")
    try:
        return _canonical_path(name)
    except PathPolicyError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathPolicyError(f"Refused '{name}': cannot resolve path. Use an accessible file path.") from exc


def _is_word_package(path: Path, *, allow_interrupted: bool = False) -> bool:
    # Only the ZIP directory is needed. An intact non-Word archive can be
    # positively identified and never qualifies as an interrupted Word save.
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PathPolicyError(f"Refused '{path}': target changed. Choose a regular Word document.")
            with ZipFile(stream) as archive:
                return _WORD_PARTS.issubset(entry.orig_filename for entry in archive.infolist())
    except (BadZipFile, UnicodeError, NotImplementedError):
        return allow_interrupted
    except OSError as exc:
        raise PathPolicyError(f"Refused '{path}': cannot read target. Choose a readable Word document.") from exc


def _same_folder(first: Path, second: Path) -> bool:
    # Windows directories can opt into case sensitivity. Path equality alone
    # folds case and could grant the sibling exception in a different folder.
    if str(first.parent) == str(second.parent):
        return True
    try:
        first_stat = first.parent.stat()
        return bool(first_stat.st_ino) and os.path.samestat(first_stat, second.parent.stat())
    except OSError:
        return False


def checked_output_path(target: str | Path, *inputs: str | Path, pdf: bool = False) -> Path:
    """Return the canonical write destination. A PDF export also checks existing contents."""
    path = resolved_input_path(target)
    sources = [resolved_input_path(source) for source in inputs]
    ending = path.suffix.lower()
    matching = [source for source in sources if source.suffix.lower() == ending]
    if pdf:
        allowed = ending == ".pdf"
    else:
        allowed = bool(ending) and ending != ".pdf" and (ending == ".docx" or bool(matching))
    if not allowed:
        if pdf:
            raise PathPolicyError(f"Refused '{path}': unsupported PDF output ending. Choose a .pdf file.")
        raise PathPolicyError(
            f"Refused '{path}': output must be a .docx file or match the input ending, with .pdf reserved for export. "
            "Choose a .docx output file."
        )
    if ending in _SIBLING_ONLY and not any(_same_folder(path, source) for source in matching):
        raise PathPolicyError(
            f"Refused '{path}': {ending} output must be beside its input document. "
            "Use that folder or choose a .docx output."
        )

    info = _target_stat(path)
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise PathPolicyError(f"Refused '{path}': target is not a regular file. Choose a new .docx file.")
        sibling = any(_same_folder(path, source) for source in sources)
        if info.st_size and not _is_word_package(path, allow_interrupted=sibling and ending in _WORD_ENDINGS):
            raise PathPolicyError(f"Refused '{path}': existing file is not a Word package. Choose a new output file.")
    return path


def checked_open_path(target: str | Path) -> Path:
    """Return a canonical file safe to hand to the OS or Microsoft Word."""
    path = resolved_input_path(target)
    hint = "Choose an existing .docx, .docm, .dotx or .dotm Word document."
    if path.suffix.lower() not in _OPEN_ENDINGS:
        raise PathPolicyError(f"Refused to open '{path}': unsupported file ending. {hint}")
    info = _target_stat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise PathPolicyError(f"Refused to open '{path}': expected an existing regular file. {hint}")
    if not _is_word_package(path):
        raise PathPolicyError(f"Refused to open '{path}': file is not a readable Word package. {hint}")
    return path


def checked_revision_paths(target: str | Path, source: str | Path) -> tuple[Path, Path]:
    from adeu.compare import _same_file
    from adeu.text_revision import unverified_revision_output

    primary = checked_output_path(target, source)
    diagnostic = checked_output_path(unverified_revision_output(primary), source)
    if _same_file(diagnostic, primary) or _same_file(diagnostic, source):
        raise PathPolicyError(
            f"Refused '{diagnostic}': diagnostic copy would replace the input or requested output. "
            "Choose a different output file."
        )
    return primary, diagnostic
