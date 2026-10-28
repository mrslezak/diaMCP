"""Tool decorator and base classes for dynamic MCP tools."""

import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONTAINER_WORKSPACE = Path("/workspace")


class WorkspacePathError(ValueError):
    """Raised when a requested path resolves outside the workspace root."""


def _detect_workspace_dir() -> Path:
    """Resolve the workspace root for the current platform."""
    override = os.environ.get("DIAMCP_WORKSPACE")
    if override:
        return Path(override).expanduser().resolve()
    # Keep the container layout working when running inside Docker.
    if os.name != "nt" and _CONTAINER_WORKSPACE.is_dir():
        return _CONTAINER_WORKSPACE.resolve()
    return (_REPO_ROOT / "workspace").resolve()


_WORKSPACE_DIR = _detect_workspace_dir()


def get_workspace_dir() -> Path:
    """Absolute workspace root (repo-relative ./workspace unless overridden)."""
    return _WORKSPACE_DIR


def ensure_workspace_dirs() -> Path:
    """Create the workspace and its tools directory if they do not exist."""
    (_WORKSPACE_DIR / "tools").mkdir(parents=True, exist_ok=True)
    return _WORKSPACE_DIR


def workspace_path(path: str = "") -> Path:
    """Resolve a user-supplied path inside the workspace root.

    Raises WorkspacePathError if the result escapes the workspace.
    """
    base = get_workspace_dir()
    candidate = Path(str(path).strip().strip('"')).expanduser()
    full = candidate if candidate.is_absolute() else base / candidate
    resolved = full.resolve()
    if resolved != base and not resolved.is_relative_to(base):
        raise WorkspacePathError(f"path '{path}' is outside the workspace ({base})")
    return resolved


def workspace_relative(path) -> str:
    """Display form of a path, relative to the workspace root when possible."""
    p = Path(path)
    try:
        return str(p.relative_to(get_workspace_dir()))
    except ValueError:
        return str(p)


@dataclass
class ToolDefinition:
    name: str
    description: str
    func: Callable
    input_schema: dict


class ToolRegistry:
    _tools: dict[str, ToolDefinition] = {}

    @classmethod
    def register(cls, name: str, description: str, func: Callable, input_schema: dict):
        cls._tools[name] = ToolDefinition(
            name=name, description=description, func=func, input_schema=input_schema
        )

    @classmethod
    def get_all(cls) -> dict[str, ToolDefinition]:
        return cls._tools.copy()

    @classmethod
    def get(cls, name: str) -> Optional[ToolDefinition]:
        return cls._tools.get(name)

    @classmethod
    def clear(cls):
        cls._tools.clear()


def tool(name: str, description: str, schema: dict):
    """Decorator to register a function as an MCP tool.

    Usage:
        @tool(
            name="my_tool",
            description="Does something useful",
            schema={
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "The input"}
                },
                "required": ["input"]
            }
        )
        def my_tool(input: str) -> str:
            return f"Processed: {input}"
    """

    def decorator(func: Callable) -> Callable:
        ToolRegistry.register(name, description, func, schema)
        return func

    return decorator


def get_tools_from_module(module) -> list[ToolDefinition]:
    """Extract tool definitions from a module."""
    tools = []
    for name, obj in inspect.getmembers(module):
        if inspect.isfunction(obj) and ToolRegistry.get(name):
            tool_def = ToolRegistry.get(name)
            if tool_def and tool_def.func is obj:
                tools.append(tool_def)
    return tools
