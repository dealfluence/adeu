# FILE: python/scripts/verify_reasoning_order.py
"""
Introspects the FastMCP server's advertised tool schemas and asserts that
EVERY tool declares `reasoning` as (a) the first property and (b) a required
field of type string.

Run: uv run python scripts/verify_reasoning_order.py
Exit 0 = all good, 1 = at least one violation.
"""

import asyncio
import sys


def _extract_schema(tool) -> dict:
    """
    FastMCP Tool objects expose their JSON schema under different attribute
    names across versions (parameters / inputSchema / input_schema / schema).
    Probe them in order and return the first dict that has 'properties'.
    """
    for attr in ("parameters", "inputSchema", "input_schema", "schema"):
        val = getattr(tool, attr, None)
        if isinstance(val, dict) and "properties" in val:
            return val
    # Fall back: some versions nest it, or expose a model with .model_json_schema()
    for attr in ("parameters", "inputSchema", "input_schema"):
        val = getattr(tool, attr, None)
        if val is not None and hasattr(val, "model_json_schema"):
            return val.model_json_schema()
    return {}


async def main() -> int:
    from adeu.server import mcp

    tools = await mcp.list_tools()
    if not tools:
        print("❌ list_tools returned no tools.", file=sys.stderr)
        return 1

    # Debug aid: on first run, dump the attribute names + schema of one tool so
    # we can confirm the introspection is reading the right field.
    if "--debug" in sys.argv:
        t0 = tools[0]
        print(f"[debug] tool[0]={t0.name}", file=sys.stderr)
        print(
            f"[debug] dir()={[a for a in dir(t0) if not a.startswith('_')]}",
            file=sys.stderr,
        )
        print(f"[debug] extracted schema={_extract_schema(t0)}", file=sys.stderr)

    failures = 0
    for tool in tools:
        schema = _extract_schema(tool)
        props = schema.get("properties", {})
        required = schema.get("required", [])
        keys = list(props.keys())

        first_key = keys[0] if keys else None
        is_first = first_key == "reasoning"
        is_required = "reasoning" in required
        is_string = isinstance(props.get("reasoning"), dict) and props["reasoning"].get("type") == "string"

        if is_first and is_required and is_string:
            print(f"✅ {tool.name}: reasoning is first + required (string)")
        else:
            failures += 1
            print(
                f"❌ {tool.name}: reasoning check failed "
                f"(first_key={first_key}, required={is_required}, string={is_string})",
                file=sys.stderr,
            )

    print(f"\n{len(tools) - failures}/{len(tools)} tools passed.", file=sys.stderr)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
