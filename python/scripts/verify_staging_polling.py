import asyncio
import os
import sys
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
from adeu.mcp_components.tools.email import search_and_fetch_emails

class ConsoleContext:
    """Mock FastMCP context to cleanly output trace events to stdout."""

    async def info(self, msg, extra=None):
        print(f"🟢 [INFO] {msg} {f'| {extra}' if extra else ''}")

    async def debug(self, msg, extra=None):
        print(f"🔵 [DEBUG] {msg} {f'| {extra}' if extra else ''}")

    async def warning(self, msg, extra=None):
        print(f"🟡 [WARN] {msg} {f'| {extra}' if extra else ''}")


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
    
    # Write back and append newly added values
    for key, val in env_vars.items():
        if key not in existing_keys:
            lines.append(f"{key}={val}\n")
            
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


async def main():
    print("====================================================")
    print("Adeu Python Verification Tool")
    print("====================================================")
    print(f"Target Backend: {os.environ.get('ADEU_BACKEND_URL')}")
    
    api_key = os.environ.get("ADEU_API_KEY")
    
    # 2. Keyring Fallback check
    if not api_key:
        api_key = DesktopAuthManager.get_api_key()
        if api_key:
            print("🔑 API key verified via native system keychain.")

    # 3. Interactive Web Auth Trigger
    if not api_key:
        print("❌ No API key found in .env or platform keyring.")
        print("Starting interactive authentication flow...")
        print(f"Opening web browser. Complete validation inside {os.environ.get('ADEU_FRONTEND_URL')}.")
        
        try:
            api_key = DesktopAuthManager.authenticate_interactive()
            print("✅ Successfully authenticated!")
            
            # Persist variables back to .env
            env_vars["ADEU_API_KEY"] = api_key
            env_vars["ADEU_BACKEND_URL"] = os.environ.get("ADEU_BACKEND_URL")
            env_vars["ADEU_FRONTEND_URL"] = os.environ.get("ADEU_FRONTEND_URL")
            save_env_file(env_path, env_vars)
            print(f"📝 Saved credentials to {env_path.relative_to(repo_root)}")
        except Exception as e:
            print(f"💥 Authentication failed: {e}")
            return

    # 4. Trigger Email Search Verification
    print(f"\n🚀 Initiating search request on {os.environ.get('ADEU_BACKEND_URL')}...")
    ctx = ConsoleContext()
    
    try:
        # Phase 1: Fire base search without task_id
        result = await search_and_fetch_emails(
            ctx=ctx,
            subject="test",  # Generic filter keywords
            limit=5,
            api_key=api_key
        )
        
        content_text = result.content[0].text if hasattr(result, "content") else str(result)
        structured = getattr(result, "structured_content", {}) or {}
        
        print("\n--- Phase 1 Response ---")
        print(f"Response Text Preview:\n{content_text[:400]}...")
        print(f"Structured Payload keys: {list(structured.keys())}")
        
        status = structured.get("status")
        task_id = structured.get("task_id")
        
        # Phase 2: If task is pending, trigger stateful polling loop
        if status == "pending" and task_id:
            print(f"\n⚡ Stateful Task Created Successfully (task_id: {task_id})")
            print("Beginning Phase 2 (Time-Bounded Polling) loop...")
            
            poll_result = await search_and_fetch_emails(
                ctx=ctx,
                task_id=task_id,
                api_key=api_key
            )
            
            final_text = poll_result.content[0].text if hasattr(poll_result, "content") else str(poll_result)
            final_struct = getattr(poll_result, "structured_content", {}) or {}
            
            print("\n--- Stateful Polling Finished ---")
            print(f"Final Status: {final_struct.get('status') or 'COMPLETED'}")
            print(f"Result Body Preview:\n{final_text[:600]}...")
        else:
            print("\n✅ Execution completed synchronously on backend.")
            
    except Exception as e:
        print(f"💥 Verification execution failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())