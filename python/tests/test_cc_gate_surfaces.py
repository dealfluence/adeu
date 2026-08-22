"""CC-4: the override parameters exist on every surface, with the right default.

spec-gates.md §1 lists three parameters and the surfaces each must appear on.
A gate whose override is unreachable from the caller's surface is a gate the
caller cannot legitimately get past, which turns a safety rail into a wall —
so the surface list is part of the contract, not packaging.

The default matters as much as the presence. §1: "Booleans, schema default
`false` (truthy defaults survive client stripping)". A default of True would
mean the gate silently does not exist for any client that strips defaults.
"""

import inspect

import pytest

from adeu.redline.engine import RedlineEngine
from adeu.redline.gates import (
    ALLOW_UNTRACKED_WRITES,
    IGNORE_CONTROL_LOCKS,
    IGNORE_DOCUMENT_PROTECTION,
)

OVERRIDES = (IGNORE_CONTROL_LOCKS, IGNORE_DOCUMENT_PROTECTION, ALLOW_UNTRACKED_WRITES)


@pytest.mark.parametrize("name", OVERRIDES)
def test_the_engine_accepts_each_override_defaulting_off(name):
    sig = inspect.signature(RedlineEngine.__init__)
    assert name in sig.parameters, f"RedlineEngine has no {name} kwarg"
    assert sig.parameters[name].default is False


def test_the_cli_exposes_every_override_as_a_flag():
    """Drives the real CLI, because the parser is built inside main().

    One subprocess for all three flags rather than three: `--help` is the
    slowest thing in this file and the assertion is the same shape either way.
    Routed through run_cli per the encoding contract in tests/utils.py.
    """
    from tests.utils import run_cli

    result = run_cli("apply", "--help")
    assert result.returncode == 0, result.stderr
    for name in OVERRIDES:
        flag = "--" + name.replace("_", "-")
        assert flag in result.stdout, f"{flag} missing from `adeu apply --help`"


@pytest.mark.parametrize("name", OVERRIDES)
def test_both_mcp_registrations_expose_each_override(name):
    """Both, not either: the win32 and non-win32 tools are separate functions.

    They have drifted before, and a parameter present on only one means the
    override works or does not depending on which OS the server runs on —
    which the calling agent cannot see.
    """
    import adeu.mcp_components.tools.document as doc_tools

    source = inspect.getsource(doc_tools)
    registrations = source.count(f"{name}: Ignore") + source.count(f"{name}: Allow")
    assert registrations == 2, f"{name} appears on {registrations} MCP registrations, expected 2"


@pytest.mark.parametrize("name", OVERRIDES)
def test_the_disk_helper_threads_each_override(name):
    from adeu.mcp_components.tools.document import _process_document_batch_disk

    sig = inspect.signature(_process_document_batch_disk)
    assert name in sig.parameters
    assert sig.parameters[name].default is False


@pytest.mark.parametrize("name", OVERRIDES)
def test_the_http_surface_reads_each_override(name):
    import adeu.serve as serve

    assert f'req.get("{name}"' in inspect.getsource(serve)


def test_the_engine_records_protection_at_load():
    # spec-gates §3: read once at load, not per gate, so the gates, the
    # banner and the ledger cannot report different states.
    import io

    from tests.cc_fixture import cc_fixture_bytes

    eng = RedlineEngine(io.BytesIO(cc_fixture_bytes(protection="forms")))
    assert eng.protection.active
    assert eng.protection.edit == "forms"
    assert "fill-in-forms" in eng.protection.describe()

    unprotected = RedlineEngine(io.BytesIO(cc_fixture_bytes()))
    assert not unprotected.protection.active
