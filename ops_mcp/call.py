"""
Try the Ops MCP server from the terminal, in memory, with no AI involved.

  uv run python -m ops_mcp.call --list
  uv run python -m ops_mcp.call service_overview minutes=5
  uv run python -m ops_mcp.call compare_windows 2 3            (positional values follow the tool's inputs)
  uv run python -m ops_mcp.call --resource ops://runbook
  uv run python -m ops_mcp.call --prompt investigate symptom="bookings are failing"
"""
import asyncio
import json
import sys

from mcp import Client

from ops_mcp.server import mcp


def parse(value: str) -> int | str:
    return int(value) if value.lstrip("-").isdigit() else value


async def main(argv: list[str]) -> None:
    async with Client(mcp) as client:
        if not argv or argv[0] == "--list":
            for tool in (await client.list_tools()).tools:
                kind = "read-only" if tool.annotations and tool.annotations.read_only_hint else "ACTION"
                first_line = (tool.description or "").strip().splitlines()[0]
                print(f"{tool.name:<20} {kind:<10} {first_line}")
            return
        if argv[0] == "--resource":
            print((await client.read_resource(argv[1])).contents[0].text)
            return
        if argv[0] == "--prompt":
            args = dict(a.split("=", 1) for a in argv[2:])
            for message in (await client.get_prompt(argv[1], args)).messages:
                print(message.content.text)
            return

        name, rest = argv[0], argv[1:]
        tools = {t.name: t for t in (await client.list_tools()).tools}
        params = list(tools[name].input_schema.get("properties", {})) if name in tools else []
        args: dict = {}
        for i, item in enumerate(rest):
            if "=" in item:
                key, value = item.split("=", 1)
                args[key] = parse(value)
            elif i < len(params):
                args[params[i]] = parse(item)
        result = await client.call_tool(name, args)
        if result.is_error:
            print("ERROR: " + " ".join(getattr(c, "text", "") for c in result.content))
        elif result.structured_content is not None:
            print(json.dumps(result.structured_content, indent=2, ensure_ascii=False))
        else:
            print("\n".join(getattr(c, "text", "") for c in result.content))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")                  # Windows consoles default to a legacy code page
    asyncio.run(main(sys.argv[1:]))
