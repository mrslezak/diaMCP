"""Built-in tools for the MCP server."""

import json
import os
import re
import shutil
import subprocess
from itertools import islice
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from mcp.server.fastmcp import Context
from execution import (
    ToolFailure, ToolPayload, default_timeout, run_isolated, run_process, run_sync, timeout_seconds,
)

from base import (
    WorkspacePathError,
    get_workspace_dir,
    tool,
    workspace_path,
    workspace_relative,
)

IS_WINDOWS = os.name == "nt"

# Finance and news sites 429/time out the default python-httpx User-Agent.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


DANGEROUS_PATTERNS = [
    (r"\brm\s+-rf\s+/\s", "rm -rf / with trailing path"),
    (r"\brm\s+-rf\s+\*\s", "rm -rf * with trailing space"),
    (r"\brm\s+-rf\s+\.\s", "rm -rf . with trailing space"),
    (r"\brm\s+-rf\s+/", "rm -rf /"),
    (r"\brm\s+-rf\s+\*", "rm -rf *"),
    (r"\brm\s+-rf\s+\.", "rm -rf ."),
    (r"\brm\s+-rf\s", "rm -rf with any path"),
    (r"\brm\s+-r\s+/\s", "rm -r / with trailing path"),
    (r"\brm\s+-r\s+\*\s", "rm -r * with trailing space"),
    (r"\brm\s+-r\s+\.\s", "rm -r . with trailing space"),
    (r"\brm\s+-r\s+/\s", "rm -r / with trailing path"),
    (r"\brm\s+-r\s+\*", "rm -r *"),
    (r"\brm\s+-r\s+\.", "rm -r ."),
    (r"\brm\s+-r\s", "rm -r with any path"),
    (r"\bdd\s+.*of=/dev/sd", "dd to disk device"),
    (r"\bdd\s+.*of=/dev/hd", "dd to disk device"),
    (r"\bdd\s+.*of=/dev/null", "dd to null (data loss)"),
    (r"\bdd\s+.*of=/", "dd to root device"),
    (r"\bmkfs", "filesystem creation"),
    (r"\bshutdown", "shutdown command"),
    (r"\breboot", "reboot command"),
    (r"\bhalt", "halt command"),
    (r"\bpoweroff", "poweroff command"),
    (r"\binit\s+0", "init 0 (halt)"),
    (r"\bchmod\s+-R\s+777", "chmod -R 777"),
    (r"\bchmod\s+777", "chmod 777 (wide open)"),
    (r"\bcurl.*\|.*sh", "curl piped to shell"),
    (r"\bwget.*\|.*sh", "wget piped to shell"),
    (r":\(\)\{", "fork bomb"),
    (r">\s*/etc/passwd", "writing to passwd"),
    (r">\s*/etc/shadow", "writing to shadow"),
    (r"\.\/.*\s*>", "redirecting output to file"),
]

# Windows / PowerShell equivalents of the patterns above.
DANGEROUS_PATTERNS += [
    (r"\bremove-item\b[^|;]*-recurse", "Remove-Item -Recurse"),
    (r"\bremove-item\b[^|;]*-force", "Remove-Item -Force"),
    (r"\b(rd|rmdir)\s+/s", "rd /s (recursive directory delete)"),
    (r"\bdel\s+/[a-z\s/]*s\b", "del /s (recursive file delete)"),
    (r"\bformat\s+[a-z]:", "format drive"),
    (r"\b(format-volume|clear-disk|initialize-disk|remove-partition)\b", "disk wipe cmdlet"),
    (r"\bdiskpart\b", "diskpart"),
    (r"\bbcdedit\b", "bcdedit (boot configuration)"),
    (r"\bvssadmin\s+delete\s+shadows", "vssadmin delete shadows"),
    (r"\bwbadmin\s+delete\b", "wbadmin delete (backup destruction)"),
    (r"\bcipher\s+/w", "cipher /w (free space wipe)"),
    (r"\bfsutil\s+(file|volume)\b", "fsutil volume/file operation"),
    (r"\btakeown\s+/f", "takeown /f (ownership change)"),
    (r"\bicacls\b[^|;]*/grant", "icacls /grant (permission change)"),
    (r"\b(stop-computer|restart-computer)\b", "Stop-Computer / Restart-Computer"),
    (
        r"\b(iwr|curl|wget|invoke-webrequest|invoke-restmethod)\b[^|]*\|[^|]*\b(iex|invoke-expression)\b",
        "web download piped to Invoke-Expression",
    ),
    (r"\b(iex|invoke-expression)\s*\(", "Invoke-Expression (arbitrary code execution)"),
    (r"\bdownloadstring\b", "WebClient DownloadString"),
    (r"\breg\s+delete\b", "reg delete"),
    (r"\bremove-item\b[^|;]*\bhk(lm|cu|cr|u|cc):", "registry hive deletion"),
    (r"\bset-executionpolicy\b", "Set-ExecutionPolicy"),
    (r"\bset-mppreference\b[^|;]*-disable", "disabling Microsoft Defender"),
    (r"\badd-mppreference\b[^|;]*-exclusion", "adding a Defender exclusion"),
    (r"\bnet\s+user\b[^|;]*/add", "net user /add"),
    (r"\bnet\s+localgroup\s+administrators\b", "administrators group change"),
    (r"\b(new-localuser|add-localgroupmember)\b", "local account creation"),
    (r"-verb\s+runas", "privilege elevation via Start-Process -Verb RunAs"),
    (r"\bstop-process\b[^|;]*-force", "Stop-Process -Force"),
]


class CommandApprovalSchema(BaseModel):
    confirm: bool = Field(description="Confirm execution of this command")
    remember: bool = Field(default=False, description="Remember for this session")


approved_commands: set[str] = set()
blacklisted_commands: set[str] = set()


def is_command_dangerous(command: str) -> tuple[bool, str]:
    """Check if command matches dangerous patterns."""
    cmd_lower = command.lower()
    for pattern, description in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return True, f"Dangerous command: {description}"
    if command.strip() in blacklisted_commands:
        return True, "Command is blacklisted"

    if ".." in command and not command.strip().startswith(str(get_workspace_dir())):
        return True, "Path traversal detected"

    return False, ""


def approve_command(command: str):
    """Add command to session-approved set."""
    approved_commands.add(command.strip())


def is_command_approved(command: str) -> bool:
    """Check if command is in approved set."""
    return command.strip() in approved_commands


def blacklist_add(command: str):
    """Add command to permanent blacklist."""
    blacklisted_commands.add(command.strip())


def blacklist_remove(command: str):
    """Remove command from permanent blacklist."""
    blacklisted_commands.discard(command.strip())


def get_blacklist() -> set[str]:
    """Get current blacklist."""
    return blacklisted_commands.copy()


def get_approved() -> set[str]:
    """Get current approved commands."""
    return approved_commands.copy()


def _shell_invocation(command: str):
    """Return (args, use_shell) for running a command on the current platform."""
    if IS_WINDOWS:
        shell_exe = (
            shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        )
        return [shell_exe, "-NoProfile", "-NonInteractive", "-Command", command], False
    return command, True


@tool(
    name="read_file",
    description="Read contents of a file from the workspace",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file"}
        },
        "required": ["path"],
    },
)
def read_file(path: str) -> str | dict:
    """Read a file from the workspace."""
    try:
        full_path = workspace_path(path)
    except WorkspacePathError as e:
        return f"Error: {e}"
    if not full_path.exists():
        return f"Error: File '{path}' not found"
    if not full_path.is_file():
        return f"Error: '{path}' is not a file"
    try:
        with full_path.open(encoding="utf-8") as source:
            from execution import MAX_OUTPUT

            content = source.read(MAX_OUTPUT + 1)
        return ToolPayload(ok=True, result=content[:MAX_OUTPUT], truncated=len(content) > MAX_OUTPUT)
    except Exception as e:
        return f"Error reading file: {e}"


@tool(
    name="write_file",
    description="Write content to a file in the workspace",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    },
)
def write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace."""
    try:
        full_path = workspace_path(path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return f"Successfully wrote to '{path}'"
    except Exception as e:
        return f"Error writing file: {e}"


@tool(
    name="list_directory",
    description="List contents of a directory in the workspace",
    schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to directory (default: root)",
            }
        },
        "required": [],
    },
)
def list_directory(path: str = ".") -> str:
    """List directory contents."""
    try:
        full_path = workspace_path(path)
    except WorkspacePathError as e:
        return f"Error: {e}"
    if not full_path.exists():
        return f"Error: Directory '{path}' not found"
    if not full_path.is_dir():
        return f"Error: '{path}' is not a directory"
    try:
        items = []
        entries = list(islice(full_path.iterdir(), 1001))
        for item in sorted(entries[:1000]):
            suffix = "/" if item.is_dir() else ""
            items.append(f"{item.name}{suffix}")
        return ToolPayload(ok=True, result=items, truncated=len(entries) > 1000)
    except Exception as e:
        return f"Error listing directory: {e}"


@tool(
    name="search_files",
    description="Search for files matching a glob pattern in the workspace",
    schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g., '*.py', '**/*.txt')",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: workspace root)",
            },
        },
        "required": ["pattern"],
    },
)
def search_files(pattern: str, path: str = ".") -> str:
    """Search for files matching a glob pattern."""
    try:
        full_path = workspace_path(path)
    except WorkspacePathError as e:
        return f"Error: {e}"
    if not full_path.exists():
        return f"Error: Directory '{path}' not found"
    try:
        matches = list(islice(full_path.glob(pattern), 1001))
        if not matches:
            return f"No files matching '{pattern}' found in '{path}'"
        return ToolPayload(ok=True, result=[workspace_relative(m) for m in matches[:1000]], truncated=len(matches) > 1000)
    except Exception as e:
        return f"Error searching files: {e}"


@tool(
    name="run_command",
    description="Execute a shell command in the workspace (PowerShell on Windows, sh on Linux/macOS). Dangerous commands require confirmation. On Windows use curl.exe, not curl (curl is a PowerShell alias). For web pages prefer web_fetch.",
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 30)",
            },
        },
        "required": ["command"],
    },
)
async def run_command(command: str, timeout: int = default_timeout(30), ctx: Context | None = None) -> str | dict:
    """Execute a shell command with danger checking and optional confirmation."""
    global approved_commands

    timeout_seconds(timeout)
    dangerous, reason = is_command_dangerous(command)
    if dangerous:
        if is_command_approved(command):
            pass
        elif ctx is not None:
            try:
                from mcp.server.fastmcp import Context

                if isinstance(ctx, Context):
                    result = await ctx.elicit(
                        message=f"⚠️ DANGEROUS COMMAND DETECTED\n\nCommand: `{command}`\n\nReason: {reason}\n\nDo you want to execute this?",
                        schema=CommandApprovalSchema,
                    )
                    if result.action != "accept":
                        return f"[BLOCKED] Command blocked by user"
                    if not result.data or not result.data.confirm:
                        return "[BLOCKED] Command was not confirmed"
                    if result.data and result.data.remember:
                        approve_command(command)
                else:
                    return f"[BLOCKED] {reason}"
            except Exception:
                return f"[BLOCKED] {reason}"
        else:
            return f"[BLOCKED] {reason}"

    try:
        args, use_shell = _shell_invocation(command)
        result = await run_sync(run_process, args, timeout, shell=use_shell, cwd=str(get_workspace_dir()))
        if result["exit_code"]:
            return ToolPayload(ok=False, result=result, error={"code": "command_failed", "message": f"Command exited with code {result['exit_code']}", "retryable": False})
        return ToolPayload(ok=True, result=result, truncated=result["truncated"])
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds"
    except ToolFailure:
        raise
    except Exception as e:
        return f"Error executing command: {e}"


def _ddg_html_search(query: str, num_results: int) -> list[dict]:
    """Scrape DuckDuckGo's HTML endpoint via httpx (uses the OS resolver and cert store)."""
    import httpx
    from urllib.parse import parse_qs, unquote, urlparse

    try:
        from selectolax.lexbor import LexborHTMLParser as HTMLParser
    except ImportError:
        from selectolax.parser import HTMLParser

    response = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers=BROWSER_HEADERS,
        timeout=15,
        follow_redirects=True,
    )
    response.raise_for_status()

    results = []
    for node in HTMLParser(response.text).css("div.result"):
        link = node.css_first("a.result__a")
        if link is None:
            continue
        href = link.attributes.get("href") or ""
        # Result links are redirects: //duckduckgo.com/l/?uddg=<real url>
        target = parse_qs(urlparse(href).query).get("uddg")
        snippet = node.css_first(".result__snippet")
        results.append(
            {
                "title": link.text().strip(),
                "href": unquote(target[0]) if target else href,
                "body": snippet.text().strip() if snippet else "",
            }
        )
        if len(results) >= num_results:
            break
    return results


@tool(
    name="web_search",
    description="Search the web for information",
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "num_results": {
                "type": "integer",
                "description": "Number of results (default: 5)",
            },
        },
        "required": ["query"],
    },
)
def web_search(query: str, num_results: int = 5) -> str:
    """Search the web using DuckDuckGo."""
    errors = []
    found = []
    # httpx goes through the OS resolver and cert store; ddgs's Rust client does not,
    # so it fails on networks with unusual DNS or TLS interception.
    try:
        found = _ddg_html_search(query, num_results)
    except Exception as e:
        errors.append(f"duckduckgo-html: {str(e)[:150]}")

    if not found:
        try:
            from ddgs import DDGS

            with DDGS(timeout=10) as ddgs:
                found = ddgs.text(query, max_results=num_results)
        except Exception as e:
            errors.append(f"ddgs: {str(e)[:150]}")

    if found:
        return "\n---\n".join(
            f"Title: {r['title']}\nURL: {r['href']}\n{r['body']}\n" for r in found
        )
    if errors:
        return "Error performing search:\n" + "\n".join(errors)
    return "No results found"


@tool(
    name="web_fetch",
    description="Fetch and extract text content from a web page",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "max_length": {
                "type": "integer",
                "description": "Max characters to return (default: 10000)",
            },
        },
        "required": ["url"],
    },
)
def web_fetch(url: str, max_length: int = 10000) -> str:
    """Fetch content from a URL."""
    try:
        import httpx

        # selectolax 1.0 dropped the Modest backend in favour of lexbor.
        try:
            from selectolax.lexbor import LexborHTMLParser as HTMLParser
        except ImportError:
            from selectolax.parser import HTMLParser

        response = httpx.get(
            url, timeout=15, follow_redirects=True, headers=BROWSER_HEADERS
        )
        response.raise_for_status()

        tree = HTMLParser(response.text)
        for tag in tree.css("script, style, nav, footer, header"):
            tag.decompose()

        text = tree.text()
        if len(text) > max_length:
            text = (
                text[:max_length]
                + f"\n... [truncated, full content: {len(text)} chars]"
            )
        return text.strip()
    except ImportError as e:
        return f"Error: missing dependency - {e}"
    except Exception as e:
        return f"Error fetching URL: {e}"


@tool(
    name="python_eval",
    description="Evaluate a Python expression or statement",
    schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python code to evaluate"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 10)",
            },
        },
        "required": ["code"],
    },
)
def python_eval(code: str, timeout: int = default_timeout(10)) -> dict:
    """Evaluate Python in a killable child process (not a security sandbox)."""
    return run_isolated("python_eval", {"code": code}, timeout)


@tool(
    name="git_clone",
    description="Clone a git repository into the workspace",
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Git repository URL"},
            "path": {
                "type": "string",
                "description": "Destination path in workspace (optional)",
            },
        },
        "required": ["url"],
    },
)
def git_clone(url: str, path: str = "", timeout: int = default_timeout(30)) -> str | dict:
    """Clone a git repository."""
    try:
        dest = workspace_path(path)
    except WorkspacePathError as e:
        return f"Error: {e}"
    if dest.exists() and any(dest.iterdir()):
        return f"Error: Destination '{path}' already exists and is not empty"

    try:
        result = run_process(
            ["git", "clone", url, str(dest)],
            timeout,
            cwd=str(get_workspace_dir()),
        )
        if result["exit_code"]:
            return ToolPayload(ok=False, result=result, error={"code": "command_failed", "message": "git clone failed", "retryable": False})
        return f"Successfully cloned to '{path or dest.name}'"
    except subprocess.CalledProcessError as e:
        return f"Error cloning repo: {e.stderr}"
    except subprocess.TimeoutExpired:
        return f"Error: git clone timed out after {timeout} seconds; destination may be incomplete"
    except FileNotFoundError:
        return "Error: git is not installed or not on PATH"
    except Exception as e:
        return f"Error: {e}"


@tool(
    name="git_pull",
    description="Pull latest changes from a git repository",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the git repository"}
        },
        "required": ["path"],
    },
)
def git_pull(path: str, timeout: int = default_timeout(30)) -> str | dict:
    """Pull changes from a git repository."""
    try:
        full_path = workspace_path(path)
    except WorkspacePathError as e:
        return f"Error: {e}"
    try:
        result = run_process(
            ["git", "pull", "origin", "HEAD"],
            timeout,
            cwd=str(full_path),
        )
        if result["exit_code"]:
            return ToolPayload(ok=False, result=result, error={"code": "command_failed", "message": "git pull failed", "retryable": False})
        return ToolPayload(ok=True, result=result)
    except subprocess.TimeoutExpired:
        return f"Error: git pull timed out after {timeout} seconds; repository may have partial changes"
    except FileNotFoundError:
        return "Error: git is not installed or not on PATH"
    except Exception as e:
        return f"Error: {e}"


@tool(
    name="get_system_info",
    description="Get system information",
    schema={"type": "object", "properties": {}, "required": []},
)
def get_system_info() -> str:
    """Get basic system information."""
    import platform
    import datetime

    info = {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "hostname": platform.node(),
        "cwd": str(Path.cwd()),
        "time": datetime.datetime.now().isoformat(),
    }
    return json.dumps(info, indent=2)


@tool(
    name="grep",
    description="Search for text patterns in files within the workspace",
    schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {
                "type": "string",
                "description": "Directory to search in (default: .)",
            },
            "file_pattern": {
                "type": "string",
                "description": "File glob pattern (e.g., '*.py')",
            },
        },
        "required": ["pattern"],
    },
)
def grep(pattern: str, path: str = ".", file_pattern: str = "*", timeout: int = default_timeout(10),
         max_results: int = 100, max_file_bytes: int = 2_000_000) -> dict:
    """Search in a killable process; stop at result/output limits and skip large files."""
    if not 1 <= max_results <= 1000 or not 1 <= max_file_bytes <= 10_000_000:
        raise ToolFailure("invalid_arguments", "max_results must be 1..1000 and max_file_bytes 1..10000000")
    return run_isolated("grep", {"pattern": pattern, "path": path, "file_pattern": file_pattern,
                                "max_results": max_results, "max_file_bytes": max_file_bytes}, timeout)


@tool(
    name="get_time",
    description="Get the current date and time",
    schema={
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "Timezone (e.g., 'UTC', 'America/New_York')",
            }
        },
        "required": [],
    },
)
def get_time(timezone: str = "UTC") -> str:
    """Get current time in specified timezone."""
    import datetime

    try:
        if timezone != "UTC":
            import zoneinfo

            tz = zoneinfo.ZoneInfo(timezone)
            now = datetime.datetime.now(tz)
        else:
            now = datetime.datetime.utcnow()
        return now.isoformat()
    except Exception as e:
        return f"Error: {e}"


@tool(
    name="calculate",
    description="Perform mathematical calculations",
    schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Math expression (e.g., '2+2', 'sqrt(16)', 'sin(pi/2)')",
            }
        },
        "required": ["expression"],
    },
)
def calculate(expression: str, timeout: int = default_timeout(5)) -> dict:
    """Evaluate a mathematical expression in a killable worker process."""
    return run_isolated("calculate", {"expression": expression}, timeout)


@tool(
    name="manage_blacklist",
    description="Manage the command blacklist (requires confirmation for dangerous ops)",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "add", "remove", "clear"],
                "description": "Action to perform",
            },
            "command": {
                "type": "string",
                "description": "Command to add or remove from blacklist",
            },
        },
        "required": ["action"],
    },
)
def manage_blacklist(action: str, command: str = "") -> str:
    """Manage command blacklist."""
    global blacklisted_commands
    if action == "list":
        if not blacklisted_commands:
            return "Blacklist is empty"
        return "Blacklisted commands:\n" + "\n".join(
            f"  - {c}" for c in sorted(blacklisted_commands)
        )
    elif action == "add":
        if not command:
            return "Error: command required for 'add' action"
        blacklist_add(command)
        return f"Added '{command}' to blacklist"
    elif action == "remove":
        if not command:
            return "Error: command required for 'remove' action"
        if command not in blacklisted_commands:
            return f"Command '{command}' not in blacklist"
        blacklist_remove(command)
        return f"Removed '{command}' from blacklist"
    elif action == "clear":
        count = len(blacklisted_commands)
        blacklisted_commands.clear()
        return f"Cleared {count} commands from blacklist"
    return f"Unknown action: {action}"


@tool(
    name="manage_approved",
    description="Manage session-approved commands",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "clear"],
                "description": "Action to perform",
            },
        },
        "required": ["action"],
    },
)
def manage_approved(action: str) -> str:
    """Manage approved commands for this session."""
    global approved_commands
    if action == "list":
        if not approved_commands:
            return "No approved commands for this session"
        return "Approved commands:\n" + "\n".join(
            f"  - {c}" for c in sorted(approved_commands)
        )
    elif action == "clear":
        count = len(approved_commands)
        approved_commands.clear()
        return f"Cleared {count} approved commands"
    return f"Unknown action: {action}"


@tool(
    name="get_dangerous_patterns",
    description="Get list of dangerous command patterns that trigger confirmation",
    schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
)
def get_dangerous_patterns() -> str:
    """Get dangerous command patterns."""
    descriptions = sorted({description for _, description in DANGEROUS_PATTERNS})
    return "Dangerous patterns:\n" + "\n".join(f"  - {p}" for p in descriptions)


@tool(
    name="create_tool",
    description="Create a new MCP tool dynamically. Writes a Python tool file to workspace/tools/ and returns instructions for enabling it.",
    schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Tool name (lowercase, underscores, no spaces)",
                "pattern": "^[a-z][a-z0-9_]*$",
            },
            "description": {"type": "string", "description": "What the tool does"},
            "schema": {
                "type": "string",
                "description": 'JSON schema for tool parameters as a string (e.g., \'{"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}\')',
            },
            "code": {
                "type": "string",
                "description": "Python function body (just the code, not the decorator or def line). The function should be named matching the tool name. Example for a tool named 'greet': 'def greet(name: str) -> str:\\n    return f\"Hello, {name}!\"'",
            },
            "requirements": {
                "type": "string",
                "description": "Optional. Any new Python package dependencies needed (e.g., 'yfinance>=1.2.0'). If new packages are needed, you MUST tell the user to add them to requirements.txt, reinstall them, and restart the server",
                "default": "",
            },
        },
        "required": ["name", "description", "schema", "code"],
    },
)
def create_tool(
    name: str, description: str, schema: str, code: str, requirements: str = ""
) -> str:
    """Create a new MCP tool by writing a Python file."""
    import json
    import re
    import ast

    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        return f"Error: Invalid tool name '{name}'. Use lowercase letters, numbers, and underscores only. Must start with a letter."

    if not description.strip():
        return "Error: Description cannot be empty"

    try:
        parsed_schema = json.loads(schema)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON schema: {e}"

    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"Error: Invalid Python code: {e.msg} at line {e.lineno}"

    import_lines = [
        l.strip()
        for l in code.split("\n")
        if l.strip().startswith("import ") or l.strip().startswith("from ")
    ]
    func_match = re.search(r"^def\s+\w+\s*\(", code, re.MULTILINE)
    if import_lines and func_match:
        first_import_line = min(
            code.split("\n").index(l)
            for l in code.split("\n")
            if l.strip().startswith("import ") or l.strip().startswith("from ")
        )
        first_func_line = code.split("\n").index(
            next(l for l in code.split("\n") if re.match(r"^\s*def\s+\w+\s*\(", l))
        )
        if first_import_line > first_func_line:
            return "Error: All 'import' statements must come BEFORE the function definition. Place imports at the very top of the code parameter."

    code_without_imports = "\n".join(
        l
        for l in code.split("\n")
        if not (l.strip().startswith("import ") or l.strip().startswith("from "))
    ).strip()

    safe_name = name.strip().lower()
    tools_dir = get_workspace_dir() / "tools"
    filename = tools_dir / f"{safe_name}_tool.py"
    restart_cmd = ".\\start.ps1" if IS_WINDOWS else "./restart.sh"

    try:
        tools_dir.mkdir(parents=True, exist_ok=True)

        all_imports = ""
        if import_lines:
            all_imports = "\n".join(import_lines) + "\n"

        file_content = f'''"""Auto-generated tool: {safe_name}"""

{all_imports}from base import tool

@tool(
    name="{safe_name}",
    description={json.dumps(description)},
    schema={json.dumps(parsed_schema, indent=8)}
)
{code_without_imports}
'''
        with open(filename, "w", encoding="utf-8") as f:
            f.write(file_content)

        msg = f"""Tool '{safe_name}' created successfully!

File: {filename}
"""
        if requirements:
            msg += f"""
IMPORTANT - New dependencies required:
  Add to requirements.txt: {requirements}
  Install them: pip install -r requirements.txt
  Then restart the MCP server: {restart_cmd}
"""
        else:
            msg += f"""
To enable the tool, restart the MCP server:
  {restart_cmd}

Then verify it's available with the get_tools_list resource (diamcp://tools/list)."""

        return msg

    except Exception as e:
        return f"Error creating tool: {e}"


def register_builtin_tools():
    """Register all built-in tools with the registry."""
    from base import ToolRegistry

    builtin_tools = [
        read_file,
        write_file,
        list_directory,
        search_files,
        run_command,
        web_search,
        web_fetch,
        python_eval,
        git_clone,
        git_pull,
        get_system_info,
        grep,
        get_time,
        calculate,
        manage_blacklist,
        manage_approved,
        get_dangerous_patterns,
        create_tool,
    ]

    for t in builtin_tools:
        ToolRegistry.register(
            name=t.__name__,
            description=t.__doc__ or "",
            func=t,
            input_schema=t.__annotations__ if hasattr(t, "__annotations__") else {},
        )
