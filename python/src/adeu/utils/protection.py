"""Document protection state, read once at load (spec-gates.md §3).

`w:documentProtection` in `word/settings.xml` is what Word writes when a user
picks Review -> Restrict Editing. It carries two things this engine cares
about: which editing mode is permitted (`w:edit`) and whether the restriction
is actually being enforced (`w:enforcement`).

**Adeu never verifies or cracks `w:hash`.** Enforcement is honoured as stated
intent, not as a security boundary, because it is not one: Word's own
enforcement is equally advisory at the XML level - anything that can write the
file can clear the element. The sanctioned bypass is the override parameters,
which are explicit and disclosed in the batch report, rather than a silent
decision by the engine about whether a password looks real.

The TypeScript twin is `node/packages/core/src/utils/protection.ts` and must
stay behaviourally identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

_SETTINGS_PARTNAME = "/word/settings.xml"

#: `w:edit` values this engine gates on. Anything else (e.g. Word's
#: `readOnlyRecommended`, which is a suggestion carried elsewhere) is treated
#: as no restriction, because gating on a mode whose semantics we have not
#: verified against Word would refuse writes Word itself permits.
KNOWN_EDIT_MODES = frozenset({"readOnly", "forms", "comments", "trackedChanges"})

#: Human-readable phrasing per mode, used in gate errors. The wording matters:
#: A3.4 pins "read-only" and "enforced" as substrings of G4's error.
_MODE_PROSE = {
    "readOnly": "read-only",
    "forms": "fill-in-forms",
    "comments": "comments-only",
    "trackedChanges": "tracked-changes-only",
}


@dataclass(frozen=True)
class DocumentProtection:
    """The parsed `w:documentProtection`, or the absence of one."""

    #: `w:edit` verbatim when it is one of `KNOWN_EDIT_MODES`, else `None`.
    edit: Optional[str] = None
    #: `w:enforcement` resolved through the OOXML boolean rule.
    enforced: bool = False

    @property
    def active(self) -> bool:
        """Is there a restriction this engine should gate on?

        Both halves are required. An unenforced `w:documentProtection` is
        Word's own "restriction configured but switched off" state: Word does
        not apply it, so neither do we. Gating on the mode alone would refuse
        edits that Word permits, which is the one direction of wrongness these
        gates must never take.
        """
        return self.edit is not None and self.enforced

    def describe(self) -> str:
        """Phrasing for gate errors, e.g. `read-only, enforced`."""
        if self.edit is None:
            return "unprotected"
        prose = _MODE_PROSE.get(self.edit, self.edit)
        return f"{prose}, {'enforced' if self.enforced else 'not enforced'}"


#: The shared "no restriction" value. Frozen, so it is safe to share.
UNPROTECTED = DocumentProtection()


def _is_truthy(value: Optional[str]) -> bool:
    """The OOXML boolean rule: absent attribute means true when the element is present."""
    if value is None:
        return True
    return value.lower() not in ("0", "false", "off")


def read_document_protection(doc: Any) -> DocumentProtection:
    """Read protection state from a loaded `python-docx` document.

    Defensive throughout, and deliberately so: this runs on every engine load,
    including for documents Adeu did not write. A malformed or unreadable
    settings part means "unprotected" rather than an exception, because
    failing to load a document is a much worse outcome than failing to gate
    one - and the gates are a safety rail over Word's own behaviour, not a
    security control.
    """
    settings_part = None
    try:
        for part in doc.part.package.parts:
            if str(part.partname) == _SETTINGS_PARTNAME:
                settings_part = part
                break
    except Exception:
        return UNPROTECTED
    if settings_part is None:
        return UNPROTECTED

    from docx.oxml import parse_xml

    try:
        root = parse_xml(settings_part.blob)
    except Exception:
        return UNPROTECTED

    # Local-name matching rather than a qualified lookup, mirroring
    # `domain.py`'s privacy-flag reader: settings.xml variants from different
    # Word versions are not reliably prefixed the way the canonical schema is.
    element = None
    for el in root.iter():
        tag = el.tag
        if isinstance(tag, str) and (tag == "documentProtection" or tag.endswith("}documentProtection")):
            element = el
            break
    if element is None:
        return UNPROTECTED

    edit = None
    enforcement = None
    for name, value in element.attrib.items():
        local = name.rsplit("}", 1)[-1]
        if local == "edit":
            edit = value
        elif local == "enforcement":
            enforcement = value

    if edit not in KNOWN_EDIT_MODES:
        # A restriction we do not model. Treated as unprotected on purpose -
        # see KNOWN_EDIT_MODES.
        return UNPROTECTED

    return DocumentProtection(edit=edit, enforced=_is_truthy(enforcement))
