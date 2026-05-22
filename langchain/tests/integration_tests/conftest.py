# FILE: langchain/tests/integration_tests/conftest.py
"""Integration-test fixtures.

These fixtures are scoped narrowly so individual tests stay isolated. The
`tmp_path` builtin (function-scoped) is preferred over session-scoped temp
dirs because integration tests here write real DOCX files, and a leaked
file from one test pollutes the next.

Fixtures here that point at the shared monorepo fixtures (golden.docx,
initial.docx) skip the test if the fixture file isn't on disk — this
keeps the package installable and unit-testable on machines that don't
have the full monorepo checked out.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def working_docx(golden_docx_path: Path, tmp_path: Path) -> Path:
    """A writable copy of golden.docx in a per-test temp dir.

    Tests that mutate the file (apply_changes, accept_all_changes,
    sanitize_docx) should use this fixture rather than golden_docx_path
    directly so they don't pollute the shared fixture.
    """
    dest = tmp_path / "working.docx"
    shutil.copyfile(golden_docx_path, dest)
    return dest


@pytest.fixture
def output_path(tmp_path: Path) -> Path:
    """A per-test output path. The file itself is not created — only the
    parent directory exists.

    Use this when a tool needs an output_path argument. The fixture
    returns a path inside `tmp_path` so cleanup is automatic.
    """
    return tmp_path / "output.docx"
