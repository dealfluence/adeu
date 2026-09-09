#!/usr/bin/env python3
import json
import shutil
from pathlib import Path

SETTINGS_PATH = Path("~/.gemini/antigravity-cli/settings.json").expanduser()
BACKUP_PATH = Path("~/.gemini/antigravity-cli/settings.json.bak").expanduser()
REPO_ROOT = Path(__file__).resolve().parent.parent.as_posix()

# Define autonomous test creator permissions
TEST_CREATOR_PERMISSIONS = {
    "allow": [
        f"read_file({REPO_ROOT}/*)",
        "command(uv run pytest)",
        "command(npm run test)",
        f"write_file({REPO_ROOT}/python/tests/*)",
        f"write_file({REPO_ROOT}/tests/*)",
        f"write_file({REPO_ROOT}/node/packages/core/src/*.test.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/test-utils.ts)",
        f"write_file({REPO_ROOT}/node/packages/n8n-nodes-adeu/test/*)",
        f"write_file({REPO_ROOT}/node/packages/mcp-server/tests/*)",
    ],
    "deny": [
        f"write_file({REPO_ROOT}/python/src/*)",
        f"write_file({REPO_ROOT}/node/packages/core/src/comments.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/diff.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/domain.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/engine.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/index.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/ingest.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/mapper.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/markup.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/models.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/outline.ts)",
        f"write_file({REPO_ROOT}/node/packages/core/src/pagination.ts)",
        f"write_file({REPO_ROOT}/node/packages/mcp-server/src/*)",
        f"write_file({REPO_ROOT}/node/packages/n8n-nodes-adeu/nodes/*)",
    ],
    "ask": [],
}


def main():
    if not SETTINGS_PATH.exists():
        print(f"Error: Settings file not found at {SETTINGS_PATH}")
        return

    # Load existing settings
    with open(SETTINGS_PATH, "r") as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            print("Error: Settings file contains invalid JSON")
            return

    # Backup the original settings if not already backed up
    if not BACKUP_PATH.exists():
        print(f"Backing up current settings to {BACKUP_PATH}")
        shutil.copy(SETTINGS_PATH, BACKUP_PATH)
    else:
        print(f"Backup already exists at {BACKUP_PATH}")

    # Set permissions
    settings["permissions"] = TEST_CREATOR_PERMISSIONS

    # Write back updated settings
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)

    print("✅ Successfully enabled Autonomous Test Creator Mode.")
    print(
        "Restricted files allowed for write, source files explicitly denied, test commands allowed."
    )


if __name__ == "__main__":
    main()
