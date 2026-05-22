# FILE: langchain/tests/conftest.py
"""Shared pytest fixtures for langchain-adeu tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Path to the monorepo root (parent of the langchain/ directory)."""
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def golden_docx_path(repo_root: Path) -> Path:
    """Path to the shared golden fixture DOCX used across tests.

    Reuses `shared/fixtures/golden.docx` from the monorepo — this is the
    same source-of-truth fixture used by the Python and Node engines.
    """
    p = repo_root / "shared" / "fixtures" / "golden.docx"
    if not p.exists():
        pytest.skip(f"Golden fixture not found at {p}; integration tests skipped.")
    return p


@pytest.fixture(scope="session")
def initial_docx_path(repo_root: Path) -> Path:
    """Path to the empty initial fixture DOCX used for write-side tests."""
    p = repo_root / "shared" / "fixtures" / "initial.docx"
    if not p.exists():
        pytest.skip(f"Initial fixture not found at {p}; integration tests skipped.")
    return p
