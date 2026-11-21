"""Real stdio initialize/list/status acceptance. No mutable tool is called."""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def inspect():
    root = Path(__file__).resolve().parent
    env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    for name in ("SSCNC_INSTALL_DIR", "SSCNC_WORKSPACE"):
        if name in os.environ:
            env[name] = os.environ[name]
    params = StdioServerParameters(command=sys.executable, args=["-B", "-u", str(root / "server.py")],
                                  cwd=str(root), env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialization = await session.initialize()
            catalog = await session.list_tools()
            names = {tool.name for tool in catalog.tools}
            expected = {
                "launch_simulator", "status", "start", "pause", "resume", "set_view", "save_workpiece",
                "setup_status", "read_program", "load_program", "open_workpiece", "set_mode", "set_emergency",
                "initialize_controller", "reset_program", "open_setup", "inspect_setup_dialog", "setup_action",
                "controller_keys", "capture_screenshot", "show_program_page", "controller_program_page",
                "rotate_view", "set_display", "inspect_program", "inspect_workpiece", "validate_pocket",
                "completion_monitor",
            }
            if names != expected: raise AssertionError(f"Unexpected tool set: {sorted(names)}")
            status_result = await session.call_tool("status", {})
            if status_result.isError: raise AssertionError("Protocol tool call returned isError")
            status = status_result.structuredContent
            if status is None:
                status = json.loads(next(content.text for content in status_result.content if content.type == "text"))
            return dict(at=datetime.now(timezone.utc).isoformat(), protocol_ok=True,
                        server=initialization.serverInfo.model_dump(mode="json"),
                        tools=[tool.model_dump(mode="json") for tool in catalog.tools], status=status,
                        calls=["initialize", "tools/list", "status"], simulator_changes_requested=False)


if __name__ == "__main__":
    result = asyncio.run(asyncio.wait_for(inspect(), timeout=30))
    print(json.dumps({"protocol_ok": result["protocol_ok"], "tool_count": len(result["tools"]),
                      "state": result["status"]["state"]}))
