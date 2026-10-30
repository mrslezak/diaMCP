"""MCP Server with dynamic tool discovery - HTTP/streamable-http for llama.cpp webui."""

import sys
import os
import logging
import datetime
import platform
import asyncio
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("diamcp")

# Trust the OS certificate store so TLS-intercepting corporate proxies work.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

APP_DIR = Path(__file__).resolve().parent
APP_TOOLS_DIR = APP_DIR / "tools"
APP_EXAMPLES_DIR = APP_DIR / "examples"

sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_TOOLS_DIR))

from base import ToolRegistry, ensure_workspace_dirs
from builtin import register_builtin_tools
from execution import (
    ToolFailure, async_tool, failure, normalize_result, render_response, timeout_seconds,
)

WORKSPACE_DIR = ensure_workspace_dirs()
WORKSPACE_TOOLS_DIR = WORKSPACE_DIR / "tools"

sys.path.insert(0, str(WORKSPACE_DIR))
sys.path.insert(0, str(WORKSPACE_TOOLS_DIR))

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))
# Browser clients (llama.cpp webui) are a different origin when not using the
# llama-server proxy. Default to loopback only - this server can run shell commands.
MCP_CORS_ORIGINS = os.environ.get("MCP_CORS_ORIGINS", "")
LOOPBACK_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

class RobustFastMCP(FastMCP):
    """Report timeouts and failures to the model as short text with a next-step hint."""

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        payload = await self.run_tool(name, arguments)
        return CallToolResult(
            content=[TextContent(type="text", text=render_response(payload))],
            isError=not payload["ok"],
        )

    async def run_tool(self, name: str, arguments: dict) -> dict:
        # Local models often send null for optional parameters; treat it as "not provided".
        arguments = {k: v for k, v in (arguments or {}).items() if v is not None}
        logger.info("Tool call %s %s", name, json.dumps(arguments, default=str)[:300])
        try:
            definition = ToolRegistry.get(name)
            if definition is None:
                raise ToolFailure("unknown_tool", f"Unknown tool: {name}")
            seconds = timeout_seconds(arguments.get("timeout"))
            # FastMCP still validates/coerces arguments and injects Context.
            # convert_result is disabled so arbitrary success values remain intact.
            value = await asyncio.wait_for(
                self._tool_manager.call_tool(
                    name, arguments, context=self.get_context(), convert_result=False,
                ), timeout=seconds,
            )
            payload = normalize_result(value)
        except TimeoutError:
            payload = failure("timeout", f"Tool '{name}' exceeded its deadline. Blocking custom tool work may still be running; do not blindly retry writes.", True)
        except Exception as exc:
            cause = exc
            while cause.__cause__ is not None:
                cause = cause.__cause__
            if isinstance(cause, ToolFailure):
                payload = failure(cause.code, str(cause), cause.retryable)
            else:
                from pydantic import ValidationError

                if isinstance(cause, ValidationError):
                    problems = "; ".join(
                        f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in cause.errors()
                    )
                    tool = self._tool_manager.get_tool(name)
                    expected = ", ".join(
                        f"{p}{'' if p in tool.parameters.get('required', []) else ' (optional)'}"
                        for p in tool.parameters.get("properties", {})
                    ) if tool else ""
                    payload = failure("invalid_arguments", f"Bad arguments for '{name}': {problems}. Expected: {expected}.")
                else:
                    code = "invalid_arguments" if isinstance(cause, TypeError) else "tool_error"
                    payload = failure(code, f"{type(cause).__name__}: {cause}")
            logger.warning("Tool %s failed: %s", name, payload["error"]["message"])
        return payload


mcp = RobustFastMCP(
    "do-it-all-mcp",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
    json_response=True,
)


@mcp.resource("diamcp://context/time")
def get_time_resource() -> str:
    """Current date and time in ISO format."""
    return datetime.datetime.now().isoformat()


@mcp.resource("diamcp://context/system")
def get_system_resource() -> str:
    """System information for context."""
    info = {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "hostname": platform.node(),
    }
    return "\n".join(f"{k}: {v}" for k, v in info.items())


@mcp.resource("diamcp://workspace/summary")
def get_workspace_summary() -> str:
    """Summary of workspace contents."""
    if not WORKSPACE_DIR.exists():
        return "Workspace is empty"

    items = []
    for item in sorted(WORKSPACE_DIR.iterdir())[:20]:
        suffix = "/" if item.is_dir() else ""
        items.append(f"{item.name}{suffix}")

    total = sum(1 for _ in WORKSPACE_DIR.rglob("*") if _.is_file())
    summary = f"Workspace root contents ({total} total files):\n" + "\n".join(items)
    if total > 20:
        summary += f"\n... and {total - 20} more files"
    return summary


@mcp.resource("diamcp://tools/list")
def get_tools_list() -> str:
    """List of all available tools with descriptions."""
    tools = ToolRegistry.get_all()
    lines = ["Available tools:"]
    for name, tool_def in sorted(tools.items()):
        lines.append(f"- {name}: {tool_def.description[:60]}")
    return "\n".join(lines)


@mcp.prompt(title="Startup Context")
def startup_context() -> str:
    """Get the LLM to call essential tools on startup."""
    return (
        "When starting a new task, first call the following tools to understand your environment:\n"
        "1. get_time - to know the current date/time\n"
        "2. get_system_info - to understand the system you're running on\n"
        "Then proceed with the user's request."
    )


@mcp.prompt(title="File Search First")
def file_search_first() -> str:
    """Guide for effective file searching."""
    return (
        "Before reading files, use search_files or list_directory to understand the project structure. "
        "Use grep to find specific content across files."
    )


@mcp.prompt(title="Web Research")
def web_research() -> str:
    """Guide for web research tasks."""
    return (
        "For web research tasks:\n"
        "1. Use web_search to find relevant pages\n"
        "2. Use web_fetch to extract content from promising URLs\n"
        "3. Use calculate for any math needed\n"
        "4. Summarize findings for the user\n"
        "5. Format beautifully, use markdown, cite sources with links"
    )


@mcp.prompt(title="Code Review")
def code_review() -> str:
    """Guide for reviewing code."""
    return (
        "When asked to review code:\n"
        "1. Use search_files to find relevant code files\n"
        "2. Use read_file to examine the code\n"
        "3. Use grep to find specific patterns or issues\n"
        "4. Use count_lines to understand project size\n"
        "5. Provide constructive feedback"
    )


def discover_tools_from_dir(tools_dir: Path, source_name: str):
    """Discover and load Python tools from a directory."""
    if not tools_dir.exists():
        return

    for filepath in tools_dir.glob("*.py"):
        if filepath.name.startswith("_"):
            continue
        if filepath.name in {"base.py", "execution.py", "worker.py"}:
            continue

        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(filepath.stem, filepath)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[filepath.stem] = module
                spec.loader.exec_module(module)
                logger.info(f"Loaded tools from {source_name}/{filepath.name}")
        except Exception as e:
            logger.error(f"Failed to load {source_name}/{filepath.name}: {e}")


def discover_user_tools():
    """Discover and load Python tools from tools/ and workspace directories."""
    discover_tools_from_dir(APP_TOOLS_DIR, "tools")
    discover_tools_from_dir(APP_EXAMPLES_DIR, "examples")
    discover_tools_from_dir(WORKSPACE_TOOLS_DIR, "workspace/tools")


def register_tools():
    """Register all tools from registry with FastMCP."""
    for name, tool_def in ToolRegistry.get_all().items():
        try:
            mcp.add_tool(
                async_tool(tool_def.func), name=tool_def.name,
                description=tool_def.description + " Time and output are limited; failures start with [ERROR: code] followed by what to do next.",
                structured_output=False,
            )
            logger.info(f"Registered tool: {tool_def.name}")
        except Exception as e:
            logger.error(f"Failed to register tool {tool_def.name}: {e}")


def build_app():
    """Streamable-HTTP app with CORS so browser clients can connect directly."""
    from starlette.middleware.cors import CORSMiddleware

    app = mcp.streamable_http_app()
    cors = {
        "allow_methods": ["GET", "POST", "DELETE", "OPTIONS"],
        "allow_headers": ["*"],
        "expose_headers": ["Mcp-Session-Id"],
    }
    if MCP_CORS_ORIGINS.strip() == "*":
        logger.warning("CORS is open to any origin - only do this on a trusted network")
        cors["allow_origins"] = ["*"]
    elif MCP_CORS_ORIGINS.strip():
        cors["allow_origins"] = [o.strip() for o in MCP_CORS_ORIGINS.split(",") if o.strip()]
    else:
        cors["allow_origin_regex"] = LOOPBACK_ORIGIN_REGEX
    app.add_middleware(CORSMiddleware, **cors)
    return app


def main():
    """Start the MCP server."""
    import uvicorn

    register_builtin_tools()
    discover_user_tools()
    register_tools()

    logger.info(f"diaMCP Server starting on {MCP_HOST}:{MCP_PORT}")
    logger.info(f"Workspace: {WORKSPACE_DIR}")
    logger.info(f"Tools directory: {WORKSPACE_TOOLS_DIR}")
    logger.info(f"Tools loaded: {len(ToolRegistry.get_all())}")

    uvicorn.run(build_app(), host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
