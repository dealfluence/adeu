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
    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        # Patch shutil.which inside the adeu.cli module, and use the
        # correct binary name (uvx). The resolved absolute path is what
        # must end up in the config — not the bare string "uvx".
        with patch("adeu.cli.shutil.which", return_value="/usr/local/bin/uvx"):
            args = MagicMock()
            args.local = False
            handle_init(args)

    assert mock_config_path.exists()

    with open(mock_config_path) as f:
        data = json.load(f)

    assert "adeu" in data["mcpServers"]
    cmd = data["mcpServers"]["adeu"]
    # Must be the resolved absolute path so Claude Desktop can find it
    assert cmd["command"] == "/usr/local/bin/uvx"
    assert "--from" in cmd["args"]
    assert "adeu" in cmd["args"]


def test_init_updates_existing_and_backups(mock_config_path):
    """Test updating a config file that already has other settings."""
    existing_data = {
        "mcpServers": {"existing-tool": {"command": "echo", "args": ["hello"]}},
        "globalShortcut": "Cmd+Space",
    }
    with open(mock_config_path, "w") as f:
        json.dump(existing_data, f)

    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        with patch("adeu.cli.shutil.which", return_value="/usr/local/bin/uvx"):
            args = MagicMock()
            args.local = False
            handle_init(args)

    backups = list(mock_config_path.parent.glob("*.bak"))
    assert len(backups) == 1

    with open(mock_config_path) as f:
        new_data = json.load(f)

    assert "existing-tool" in new_data["mcpServers"]
    assert new_data["globalShortcut"] == "Cmd+Space"
    assert "adeu" in new_data["mcpServers"]


def test_init_exits_if_uvx_missing(mock_config_path):
    """Test that init hard-exits when uvx cannot be found.
    A missing uvx means Claude Desktop can never launch the server,
    so writing a broken config would be worse than doing nothing.
    """
    with patch("adeu.cli._get_claude_config_path", return_value=mock_config_path):
        with patch("adeu.cli.shutil.which", return_value=None):
            args = MagicMock()
            args.local = False
            with pytest.raises(SystemExit) as exc_info:
                handle_init(args)

    assert exc_info.value.code == 1
    # Config must NOT have been written — a partial/broken config is worse than none
    assert not mock_config_path.exists()
