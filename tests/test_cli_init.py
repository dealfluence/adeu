import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from adeu.cli import _get_claude_config_path, handle_init

# --- Tests for Path Resolution ---


def test_get_config_path_windows():
    with patch("platform.system", return_value="Windows"):
        with patch.dict(os.environ, {"APPDATA": "C:\\Users\\Test\\AppData\\Roaming"}):
            path = _get_claude_config_path()
            # Normalize slashes to forward slash for consistent comparison across Windows/Linux runners
            assert str(path).replace("\\", "/") == "C:/Users/Test/AppData/Roaming/Claude/claude_desktop_config.json"


def test_get_config_path_macos():
    with patch("platform.system", return_value="Darwin"):
        with patch("pathlib.Path.home", return_value=Path("/Users/Test")):
            path = _get_claude_config_path()
            assert path.as_posix() == "/Users/Test/Library/Application Support/Claude/claude_desktop_config.json"


# --- Tests for Init Logic ---


@pytest.fixture
def mock_config_path(tmp_path):
    """Returns a temporary path acting as the Claude config file."""
    d = tmp_path / "Claude"
    d.mkdir()
    return d / "claude_desktop_config.json"


def test_init_creates_fresh_config(mock_config_path):
    """Test initializing when no config exists."""
    # Patch the path resolver to return our temp path
    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        with patch("shutil.which", return_value="/usr/bin/uv"):  # Simulate uv installed
            args = MagicMock()
            args.local = False
            handle_init(args)

    # Verify file was created
    assert mock_config_path.exists()

    with open(mock_config_path) as f:
        data = json.load(f)

    # Verify Content
    assert "adeu" in data["mcpServers"]
    cmd = data["mcpServers"]["adeu"]
    assert cmd["command"] == "uvx"
    assert "--from" in cmd["args"]
    assert "adeu" in cmd["args"]


def test_init_updates_existing_and_backups(mock_config_path):
    """Test updating a config file that already has other settings."""
    # Create existing config
    existing_data = {
        "mcpServers": {"existing-tool": {"command": "echo", "args": ["hello"]}},
        "globalShortcut": "Cmd+Space",
    }
    with open(mock_config_path, "w") as f:
        json.dump(existing_data, f)

    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        with patch("shutil.which", return_value="/usr/bin/uv"):
            args = MagicMock()
            args.local = False
            handle_init(args)

    # 1. Verify Backup Created
    backups = list(mock_config_path.parent.glob("*.bak"))
    assert len(backups) == 1

    # 2. Verify Config Updated
    with open(mock_config_path) as f:
        new_data = json.load(f)

    # Old data preserved
    assert "existing-tool" in new_data["mcpServers"]
    assert new_data["globalShortcut"] == "Cmd+Space"
    # New data added
    assert "adeu" in new_data["mcpServers"]


def test_init_warns_if_uv_missing(mock_config_path, capsys):
    """Test that a warning is printed if uv is not found."""
    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        with patch("shutil.which", return_value=None):  # uv NOT found
            args = MagicMock()
            args.local = False
            handle_init(args)

    captured = capsys.readouterr()
    assert "Warning: 'uv' tool not found" in captured.err
    # It should still create the config though
    assert mock_config_path.exists()
