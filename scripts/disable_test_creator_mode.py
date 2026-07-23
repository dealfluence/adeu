#!/usr/bin/env python3
import os
import json
import shutil
from pathlib import Path

SETTINGS_PATH = Path("~/.gemini/antigravity-cli/settings.json").expanduser()
BACKUP_PATH = Path("~/.gemini/antigravity-cli/settings.json.bak").expanduser()

DEFAULT_PERMISSIONS = {
    "allow": [
        "command(git status)",
        "command(npm install)",
        "command(npm run)",
        "command(git diff)",
        "command(uv)",
        "command(git log)",
    ]
}


def main():
    if not SETTINGS_PATH.exists():
        print(f"Error: Settings file not found at {SETTINGS_PATH}")
        return

    # Check if backup exists
    if BACKUP_PATH.exists():
        print(f"Restoring settings from backup: {BACKUP_PATH}")
        try:
            shutil.copy(BACKUP_PATH, SETTINGS_PATH)
            os.remove(BACKUP_PATH)
            print(
                "✅ Successfully disabled Autonomous Test Creator Mode (Restored original settings)."
            )
        except Exception as e:
            print(f"Error restoring backup: {e}")
    else:
        print("No backup found. Restoring default permissions.")
        # Load existing settings
        with open(SETTINGS_PATH, "r") as f:
            try:
                settings = json.load(f)
            except json.JSONDecodeError:
                print("Error: Settings file contains invalid JSON")
                return

        # Restore default permissions block
        settings["permissions"] = DEFAULT_PERMISSIONS

        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2)

        print(
            "✅ Successfully disabled Autonomous Test Creator Mode (Restored defaults)."
        )


if __name__ == "__main__":
    main()
