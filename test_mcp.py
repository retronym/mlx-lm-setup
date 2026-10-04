"""Smoke-test mlx-mcp-server over stdio: list tools, call health_check, list_models and chat."""
import asyncio, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv/bin/mlx-mcp-server"),
    env={**os.environ, "MLX_BASE_URL": "http://127.0.0.1:8080", "MLX_TIMEOUT": "300",
         "MLX_DEFAULT_MODEL": "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"},
)

async def main():
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            for name, args in [("health_check", {}), ("list_models", {}),
                               ("chat", {"message": "Reply with exactly: pong"})]:
                res = await s.call_tool(name, args)
                print(f"--- {name}\n" + "".join(c.text for c in res.content if hasattr(c, "text"))[:600])

asyncio.run(main())
