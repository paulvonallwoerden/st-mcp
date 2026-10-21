"""End-to-end check: start the server over stdio and call a few tools against live st.com."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALLS = [
    ("search_products", {"query": "STM32G431", "limit": 3}),
    ("get_product", {"part": "LSM6DSO", "include_orderable_parts": True}),
    ("list_product_documents", {"part": "STM32G431KB", "category": "Errata"}),
    ("get_document_outline", {"document": "ES0431", "max_level": 1}),
    ("search_document", {"document": "STM32G431KB", "query": "VBOR0", "max_results": 3}),
    ("read_document", {"document": "STM32G431KB", "pages": "72", "max_chars": 1500}),
]


async def main() -> int:
    params = StdioServerParameters(command="uv", args=["run", "--quiet", "st-mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))
        failures = 0
        for name, args in CALLS:
            result = await session.call_tool(name, args)
            text = "".join(getattr(c, "text", "") for c in result.content)
            status = "ERROR" if result.is_error else "ok"
            failures += bool(result.is_error)
            print(f"\n--- {name} [{status}] {len(text)} chars\n{text[:400]}")
        return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
