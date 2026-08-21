"""CC-4 — load-time document protection state (spec-gates.md §3).

`w:documentProtection` was not parsed anywhere in either engine before CC-4;
these tests pin the reader that every G4-G7 gate consults. The node twin is
`node/packages/core/src/cc_protection_state.test.ts`.

The two behaviours worth stating out loud, because both are choices rather
than consequences:

* An **unenforced** restriction is not a restriction. Word writes
  `w:documentProtection` with `w:enforcement="0"` when a user configures
  Restrict Editing and then switches it off; Word does not apply it, so
  neither does Adeu. Gating on the mode alone would refuse edits Word permits,
  which is the one direction of wrongness these gates must never take.
* An **unrecognised** `w:edit` mode is treated as unprotected, for the same
  reason: refusing on semantics we have not verified against real Word would
  invent policy.
"""

import io
from dataclasses import FrozenInstanceError

import pytest
from docx import Document

from adeu.utils.protection import (
    UNPROTECTED,
    DocumentProtection,
    read_document_protection,
)
from tests.sdt_fixtures import build_sdt_docx, para


def _doc(tmp_path, protection: str | None, *, enforcement: str | None = "1"):
    """A minimal document carrying (or not carrying) a protection element."""
    body = para("Body text that a gate might refuse to touch.")
    path = build_sdt_docx(
        tmp_path / f"prot_{protection}_{enforcement}.docx",
        body,
        protection=protection,
        protection_enforcement=enforcement,
    )
    return Document(io.BytesIO(path.read_bytes()))


def test_an_unprotected_document_reads_as_unprotected(tmp_path):
    assert read_document_protection(_doc(tmp_path, None)) == UNPROTECTED


@pytest.mark.parametrize("mode", ["readOnly", "forms", "comments", "trackedChanges"])
def test_each_known_mode_is_read_and_active(tmp_path, mode):
    prot = read_document_protection(_doc(tmp_path, mode))
    assert prot.edit == mode
    assert prot.enforced is True
    assert prot.active is True


def test_an_unenforced_restriction_is_not_active(tmp_path):
    """Configured but switched off. Word does not apply it; neither do we."""
    prot = read_document_protection(_doc(tmp_path, "readOnly", enforcement="0"))
    assert prot.edit == "readOnly"
    assert prot.enforced is False
    assert prot.active is False, "an unenforced restriction must not gate anything"


def test_a_missing_enforcement_attribute_defaults_to_true(tmp_path):
    """The OOXML boolean rule: the attribute's absence means the element is on."""
    prot = read_document_protection(_doc(tmp_path, "forms", enforcement=None))
    assert prot.enforced is True
    assert prot.active is True


def test_an_unknown_edit_mode_is_treated_as_unprotected(tmp_path):
    """`readOnlyRecommended` is a suggestion, not an enforced restriction.

    Gating on a mode whose semantics were never verified against Word would
    invent policy, so unknown modes read as no restriction at all.
    """
    assert read_document_protection(_doc(tmp_path, "readOnlyRecommended")) == UNPROTECTED


def test_describe_names_the_mode_and_the_enforcement(tmp_path):
    """A3.4 pins `read-only` and `enforced` as substrings of G4's error."""
    assert read_document_protection(_doc(tmp_path, "readOnly")).describe() == ("read-only, enforced")
    assert read_document_protection(_doc(tmp_path, "readOnly", enforcement="0")).describe() == "read-only, not enforced"
    assert UNPROTECTED.describe() == "unprotected"


def test_reading_a_document_with_no_settings_part_does_not_raise():
    """Defensive by design: failing to load a document is far worse than
    failing to gate one, so anything unreadable reads as unprotected."""

    class _NoParts:
        class part:
            class package:
                parts: list = []

    assert read_document_protection(_NoParts()) == UNPROTECTED


def test_protection_is_frozen_and_comparable():
    """Gate code compares and caches these; both need value semantics."""
    a = DocumentProtection(edit="forms", enforced=True)
    assert a == DocumentProtection(edit="forms", enforced=True)
    with pytest.raises(FrozenInstanceError):
        a.edit = "readOnly"  # type: ignore[misc]
