"""
Manual check of a running service with the MCP SDK client.

    BMCP_URL=http://127.0.0.1:18443/mcp BMCP_TOKEN_FILE_PATH=/path/to/x.token \\
        .venv/bin/python -I scripts/smoke_client.py [file.csv|file.xlsx]

Lists the tools, calls health, and when a file is given uploads it,
inspects it and prints the results. The token is read from a file so it
never appears in shell history or the process list.
"""

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def show(label, result):
    body = result.structuredContent if not result.isError else {"error": result.content[0].text}
    print(f"--- {label}:", json.dumps(body, indent=2, default=str))


async def main() -> int:
    url = os.environ["BMCP_URL"]
    token = Path(os.environ["BMCP_TOKEN_FILE_PATH"]).read_text().strip()
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                print("--- server:", init.serverInfo.name, init.serverInfo.version)
                tools = (await session.list_tools()).tools
                print("--- tools:", [t.name for t in tools])
                show("health", await session.call_tool("health", {}))
                if len(sys.argv) > 1:
                    path = Path(sys.argv[1])
                    up = await session.call_tool("upload_dataset", {
                        "filename": path.name,
                        "content_base64": base64.b64encode(path.read_bytes()).decode()})
                    show("upload_dataset", up)
                    if not up.isError:
                        show("inspect_dataset", await session.call_tool(
                            "inspect_dataset", {"dataset_id": up.structuredContent["dataset_id"],
                                                "sample_rows": 3}))
                show("list_datasets", await session.call_tool("list_datasets", {}))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
