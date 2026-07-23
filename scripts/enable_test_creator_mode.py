#!/usr/bin/env python3
import json
import shutil
from pathlib import Path

SETTINGS_PATH = Path("~/.gemini/antigravity-cli/settings.json").expanduser()
BACKUP_PATH = Path("~/.gemini/antigravity-cli/settings.json.bak").expanduser()

# Define autonomous test creator permissions
TEST_CREATOR_PERMISSIONS = {
    "allow": [
        "read_file(/Users/mkorpela/workspace/adeu/*)",
        "command(uv run pytest)",
        "command(npm run test)",
        "write_file(/Users/mkorpela/workspace/adeu/python/tests/*)",
        "write_file(/Users/mkorpela/workspace/adeu/tests/*)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/*.test.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/test-utils.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/n8n-nodes-adeu/test/*)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/mcp-server/tests/*)",
    ],
    "deny": [
        "write_file(/Users/mkorpela/workspace/adeu/python/src/*)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/comments.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/diff.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/domain.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/engine.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/index.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/ingest.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/mapper.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/markup.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/models.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/outline.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/core/src/pagination.ts)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/mcp-server/src/*)",
        "write_file(/Users/mkorpela/workspace/adeu/node/packages/n8n-nodes-adeu/nodes/*)",
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
