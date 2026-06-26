# parro_mcp.py — stdio transport for local Claude Desktop testing
import asyncio
import os

from supabase import create_client
from mcp.server.stdio import stdio_server
from mcp_tools import create_parro_mcp_server

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_SERVICE_KEY", "")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


async def main():
    server = create_parro_mcp_server(supabase)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
