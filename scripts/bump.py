import json
import re
import subprocess
import sys
from pathlib import Path

FILES_TO_BUMP = [
    "python/pyproject.toml",
    "langchain/pyproject.toml",
    "node/packages/core/package.json",
    "node/packages/mcp-server/package.json",
    "desktop-extension/manifest.json",
    "gemini-extension.json",
    "python/server.json",
    "node/packages/n8n-nodes-adeu/package.json",
]


def run_cmd(cmd, cwd=None, check=True):
    """Helper to run shell commands."""
    use_shell = sys.platform == "win32"
    result = subprocess.run(
        cmd, cwd=cwd, text=True, capture_output=True, shell=use_shell
    )
    if check and result.returncode != 0:
        print(f"❌ Command failed: {' '.join(cmd)}")
        print(result.stderr)
        sys.exit(1)
    return result


def update_json_version(filepath, version):
    path = Path(filepath)
    if not path.exists():
        print(f"⚠️  Skipping {filepath} (not found)")
        return False

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    data = json.loads(content)
    old_version = data.get("version", "unknown")

    if old_version == version:
        return False

    # Regex replace to preserve exact file formatting (indents/newlines)
    new_content = re.sub(r'("version"\s*:\s*)"[^"]+"', f'\\g<1>"{version}"', content)

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"✅ Updated {filepath} ({old_version} -> {version})")
    return True


def update_toml_version(filepath, version):
    path = Path(filepath)
    if not path.exists():
        print(f"⚠️  Skipping {filepath} (not found)")
        return False

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content = re.sub(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )

    if new_content == content:
        return False

    old_match = re.search(r'^version\s*=\s*"([^"]+)"', content, flags=re.MULTILINE)
    old_version = old_match.group(1) if old_match else "unknown"

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"✅ Updated {filepath} ({old_version} -> {version})")
    return True


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/bump.py <version>")
        print("Example: python scripts/bump.py 1.6.0")
        sys.exit(1)

    target_version = sys.argv[1].lstrip("v")
    if not re.match(r"^\d+\.\d+\.\d+(-\w+(\.\d+)?)?$", target_version):
        print(
            f"❌ Error: '{target_version}' does not look like a valid semver (e.g. 1.6.0)."
        )
        sys.exit(1)

    print(f"🚀 Synchronizing monorepo to version {target_version}...\n")

    modified = False

    # Dynamically determine the updater based on file extension
    for filepath in FILES_TO_BUMP:
        if filepath.endswith(".toml"):
            if update_toml_version(filepath, target_version):
                modified = True
        else:
            if update_json_version(filepath, target_version):
                modified = True

    if not modified:
        print("\n⚠️  No files were modified. Are they already at this version?")
        sys.exit(0)

    print("\n📦 Updating lockfiles...")

    # Update uv.lock
    print("   Running 'uv lock' in python/...")
    run_cmd(["uv", "lock"], cwd="python")

    # Update uv.lock in langchain/
    print("   Running 'uv lock' in langchain/...")
    run_cmd(["uv", "lock"], cwd="langchain")

    # Update package-lock.json
    print("   Running 'npm install --package-lock-only' in node/...")
    run_cmd(["npm", "install", "--package-lock-only"], cwd="node", check=False)

    print("\n🎉 Files and lockfiles updated successfully!")
    print("\nNext steps:")
    print("  1. Review changes: git diff")
    print(f'  2. git commit -am "chore(release): bump version to {target_version}"')
    print("  3. git push origin main")
    print(
        "  4. Wait for CI to create the Draft Release, then go to GitHub to add notes and click 'Publish'"
    )


if __name__ == "__main__":
    main()
