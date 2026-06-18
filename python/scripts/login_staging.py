import asyncio
import os
import sys
import urllib.request
import json
from pathlib import Path

# Add python/src to path dynamically
repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "python" / "src"))

def load_env_file(path: Path) -> dict[str, str]:
    env_vars = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    env_vars[key.strip()] = val.strip().strip('"').strip("'")
    return env_vars

env_path = repo_root / ".env"
if not env_path.exists():
    env_path = repo_root / "python" / ".env"
env_vars = load_env_file(env_path)
for k, v in env_vars.items():
    if k not in os.environ:
        os.environ[k] = v
if "ADEU_BACKEND_URL" not in os.environ:
    os.environ["ADEU_BACKEND_URL"] = "https://app.adeu.ai"
if "ADEU_FRONTEND_URL" not in os.environ:
    os.environ["ADEU_FRONTEND_URL"] = "https://app.adeu.ai"

from adeu.mcp_components.desktop_auth import DesktopAuthManager


def save_env_file(path: Path, env_vars: dict[str, str]) -> None:
    lines = []
    existing_keys = set()
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key, _ = stripped.split("=", 1)
                    key = key.strip()
                    if key in env_vars:
                        lines.append(f"{key}={env_vars[key]}\n")
                        existing_keys.add(key)
                        continue
                lines.append(line)
    
    for key, val in env_vars.items():
        if key not in existing_keys:
            lines.append(f"{key}={val}\n")
            
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


async def test_api_key(api_key: str) -> bool:
    """Verifies the token is valid against the mailbox listing API."""
    backend_url = os.environ.get("ADEU_BACKEND_URL", "https://app.adeu.ai")
    url = f"{backend_url}/api/v1/users/me/shared-mailboxes"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                return True
    except Exception:
        pass
    return False


async def main():
    print("====================================================")
    print("Adeu Log In & Token Retrieval Tool")
    print("====================================================")
    print(f"Target Backend: {os.environ.get('ADEU_BACKEND_URL')}")
    
    # 1. Clear any stale cached keys to guarantee a fresh auth loop
    print("🧹 Cleaning local system keyring of stale keys...")
    try:
        DesktopAuthManager.clear_api_key()
    except Exception as e:
        print(f"⚠️ Non-fatal keyring clear error: {e}")

    # 2. Fire the login server
    print("\n🌐 Spinning up ephemeral local listener on localhost...")
    print(f"Opening your browser to authenticate with {os.environ.get('ADEU_FRONTEND_URL')}...")
    
    try:
        api_key = DesktopAuthManager.authenticate_interactive()
        print("\n🎉 Token gained successfully!")
    except Exception as e:
        print(f"\n💥 Interactive authentication failed: {e}")
        return

    # 3. Test and verify the token immediately
    print("\n🔍 Verifying token validity against active backend APIs...")
    is_valid = await test_api_key(api_key)
    
    if is_valid:
        print("✅ Verification Successful! Token is fully authorized.")
    else:
        print("❌ Token generated but rejected by active backend. Please check your credentials.")
        return

    # 4. Save to .env for scripts and tooling
    env_vars["ADEU_API_KEY"] = api_key
    env_vars["ADEU_BACKEND_URL"] = os.environ.get("ADEU_BACKEND_URL")
    env_vars["ADEU_FRONTEND_URL"] = os.environ.get("ADEU_FRONTEND_URL")
    
    save_env_file(env_path, env_vars)
    print(f"📝 Persisted configurations and API token to: {env_path.relative_to(repo_root)}")
    print("\n🚀 You can now safely execute search verification via:")
    print("   uv run python scripts/verify_staging_polling.py")


if __name__ == "__main__":
    asyncio.run(main())