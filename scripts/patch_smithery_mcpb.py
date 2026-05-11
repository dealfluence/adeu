import json
import os
import subprocess
import sys
import traceback
import zipfile


def get_tools_from_server():
    """Boots the Adeu Node MCP server and extracts live tool schemas via JSON-RPC."""
    server_script = "desktop-extension/index.js"
    if not os.path.exists(server_script):
        print(f"❌ {server_script} not found. Run 'npm run build' in node/ first.")
        sys.exit(1)

    print("Booting Adeu Node server to extract live tool schemas...")
    
    # We capture stderr so we can actually see if Node crashes
    try:
        proc = subprocess.Popen(
            ["node", server_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        print(f"❌ Failed to start node process: {e}")
        sys.exit(1)

    def send(msg):
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except Exception as e:
            print(f"❌ Error writing to server: {e}")
            
    def wait_for_id(target_id):
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                data = json.loads(line)
                if data.get("id") == target_id:
                    return data
            except json.JSONDecodeError:
                continue
        
        # If we break out of the loop without returning, the process died
        _, errs = proc.communicate()
        print(f"❌ Server process died unexpectedly.\nNode Stderr: {errs}")
        return None

    # 1. Initialize
    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smithery-patcher", "version": "1.0"},
            },
        }
    )
    
    init_resp = wait_for_id(1)
    if not init_resp:
        sys.exit(1)

    # 2. Initialized Notification
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # 3. List Tools
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools_resp = wait_for_id(2)
    if not tools_resp:
        sys.exit(1)

    proc.terminate()

    tools = tools_resp.get("result", {}).get("tools", [])
    print(f"✅ Successfully extracted {len(tools)} tools from server.")
    return tools


def main():
    try:
        mcpb_candidates = [
            "desktop-extension/desktop-extension.mcpb",
            "desktop-extension/Adeu.mcpb",
            "desktop-extension.mcpb",
            "Adeu.mcpb",
        ]
        src_mcpb = None

        for path in mcpb_candidates:
            if os.path.exists(path):
                src_mcpb = path
                break

        if not src_mcpb:
            print("❌ Could not find Adeu.mcpb or desktop-extension.mcpb.")
            print("Please run `npx @anthropic-ai/mcpb pack` inside desktop-extension/ first.")
            sys.exit(1)

        dest_mcpb = "adeu-smithery.mcpb"
        tools_array = get_tools_from_server()

        print(f"Patching {src_mcpb} -> {dest_mcpb}...")

        with zipfile.ZipFile(src_mcpb, "r") as zin:
            with zipfile.ZipFile(dest_mcpb, "w", zipfile.ZIP_DEFLATED) as zout:
                zout.comment = zin.comment
                for item in zin.infolist():
                    if item.filename == "manifest.json":
                        manifest_bytes = zin.read(item.filename)
                        manifest = json.loads(manifest_bytes)

                        # Inject the tools array with schemas
                        manifest["tools"] = tools_array

                        zout.writestr(item, json.dumps(manifest, indent=2))
                    else:
                        zout.writestr(item, zin.read(item.filename))

        print(f"✅ Success! Created Smithery-compatible bundle: {dest_mcpb}")
        
    except Exception as e:
        print(f"❌ Script failed with exception:\n{traceback.format_exc()}")


if __name__ == "__main__":
    # Force stdout/stderr flush to avoid missing logs if process dies
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
    main()