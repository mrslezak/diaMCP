# Do It All MCP (diaMCP)

A versatile MCP server designed specifically for **llama.cpp webui** MCP integration. Simple to set up - start the server and point your webui to the URL. Runs in Docker on Linux/macOS, or natively with plain Python on Windows.

## Why diaMCP?

When using llama-server's webui, you want tools without complexity. No servers to manage, no configuration files to write. Just:

1. `docker compose up --build -d` (Linux/macOS) or `.\start.ps1` (Windows)
2. Add the MCP URL to your webui
3. Done.

## Installation

### Option 1: Quick Install (Recommended)

#### Linux / macOS
```bash
curl -fsSL https://raw.githubusercontent.com/chartrambiz/diaMCP/main/install.sh | sh
```

#### Windows (native - no Docker, no WSL2)
```powershell
git clone https://github.com/chartrambiz/diaMCP.git
cd diaMCP
.\start.ps1 -Install
```

`-Install` installs dependencies into the Python environment currently on your PATH.
Afterwards just run `.\start.ps1` (or `python server.py`).

**Using conda/miniforge?** `-Install` detects an active conda environment and installs from
conda-forge instead of pip, since pip-installing into a conda env shadows conda-managed
packages. To install manually:

```powershell
conda install -c conda-forge --file requirements-conda.txt
```

All dependencies are available on conda-forge. Use `.\start.ps1 -Install -ForcePip` to force pip.

The Linux/macOS script will:
- Check Docker and Docker Compose are installed
- Clone the repo if not present
- Build and start the container
- Show next steps

To update an existing installation, run the same command from within the diaMCP directory.

### Option 2: Git Clone

#### Linux / macOS
```bash
# Clone the repository
git clone https://github.com/chartrambiz/diaMCP.git
cd diaMCP

# Start the container
docker compose up --build -d
```

#### Windows (PowerShell, native)
```powershell
# Clone the repository
git clone https://github.com/chartrambiz/diaMCP.git
cd diaMCP

# Install dependencies into your existing Python environment
pip install -r requirements.txt

# Start the server
python server.py
```

To update later:
```bash
git pull origin main 2>/dev/null || git pull origin master
```
then `docker compose up --build -d` (Docker) or restart `.\start.ps1` (native Windows).

## Windows Setup Requirements

diaMCP runs natively on Windows 10/11 - Docker and WSL2 are **not** required.

1. **Python 3.10 or newer** on your PATH (`python --version`)
2. **Git for Windows** - only needed for the `git_clone` / `git_pull` tools
3. **PowerShell** - `run_command` executes through `pwsh` if installed, otherwise `powershell`

If PowerShell blocks `start.ps1`, either run it as
`powershell -ExecutionPolicy Bypass -File .\start.ps1` or use `python server.py` directly.

### Launchers

| Shell | Command |
|-------|---------|
| PowerShell | `.\start.ps1` |
| cmd.exe / Miniforge Prompt | `start.cmd` (optional args: `start.cmd 0.0.0.0 8001`) |
| Any | `python server.py` |

### start.ps1 options

| Option | Default | Description |
|--------|---------|-------------|
| `-Install` | off | Install/update dependencies before starting (conda-forge if in a conda env, else pip) |
| `-ForcePip` | off | Force pip even inside a conda environment |
| `-BindHost` | `127.0.0.1` | Interface to bind |
| `-Port` | `8000` | Port to listen on |
| `-Python` | auto | Specific interpreter (`python`, `py`, full path, ...) |

The server binds to `127.0.0.1` by default so the tool server (which can run shell commands)
is not exposed to your network. Pass `-BindHost 0.0.0.0` only if llama-server runs on a
different machine.

Docker and WSL2 remain supported for Linux/macOS - `Dockerfile`, `docker-compose.yml`,
`install.sh`, `setup.sh` and `restart.sh` are unchanged and are not used by the native path.

## Post-Installation Setup

After starting the MCP server:

1. Start llama-server with MCP proxy enabled:
```bash
llama-server --webui-mcp-proxy -m model.gguf -c 32768 --host 0.0.0.0 --port 8080
```

2. In the llama.cpp webui:
   - Go to MCP Settings
   - Toggle **"Enable llama-server proxy"** ON
   - Add the MCP URL: `http://localhost:8000/mcp`

The MCP tools will be available in your chat.

**Note (Docker only):** Files created in the `workspace/` directory are owned by your user (UID 1000). If you need a different UID, edit `docker-compose.yml` and change the `user:` value. Running natively there is no UID mapping - files are created as the user running `python server.py`.

## Updating

After modifying custom tools or pulling new changes, restart the server:

```powershell
# Windows (native) - Ctrl+C to stop, then
.\start.ps1
```

```bash
# Linux/macOS (Docker)
./restart.sh
```

The Docker script rebuilds the container and verifies it's running.

## Demo

See diaMCP in action with the llama.cpp webui:

### File Operations
![File Operations Demo](./media/demo-1.gif)

### Web Search & Research
![Web Search Demo](./media/demo-2.gif)

## Features

- **21+ Built-in Tools**: File operations, web search, git, shell, code execution, and more
- **Dangerous Command Blocking**: `run_command` blocks dangerous patterns (rm -rf, dd, mkfs, etc.)
- **Elicitation Support**: Confirmation prompts for risky commands (works with MCP clients that support it)
- **Dynamic Tool Discovery**: Drop Python files into `./workspace/tools/` or use built-in examples
- **HTTP Transport**: No stdio, no extra config - just a URL
- **Persistent Workspace**: Files and tools survive restarts
- **Cross-Platform**: Native Python on Windows 10/11, or Docker on Linux/macOS
- **Works with llama-server webui**: Designed for the MCP integration built into llama.cpp webui

## Tools

### File Operations
| Tool | Description |
|------|-------------|
| `read_file` | Read file contents |
| `write_file` | Write content to files |
| `list_directory` | List directory contents |
| `search_files` | Find files by glob pattern |
| `grep` | Search for patterns in files |
| `file_info` | Get file metadata (size, dates, permissions) |
| `delete_file` | Delete a file (with safety checks) |
| `create_directory` | Create directories |

### Web & Research
| Tool | Description |
|------|-------------|
| `web_search` | Search the web (DuckDuckGo) |
| `web_fetch` | Extract text from web pages |
| `download_file` | Download a file from URL to workspace |

### Git Operations
| Tool | Description |
|------|-------------|
| `git_clone` | Clone repositories |
| `git_pull` | Pull latest changes |

### System & Code
| Tool | Description |
|------|-------------|
| `run_command` | Execute shell commands (PowerShell on Windows, sh on Linux; dangerous pattern blocking) |
| `python_eval` | Run Python snippets |
| `run_python_script` | Execute Python script files |
| `get_system_info` | System information |
| `get_time` | Current date/time |
| `calculate` | Mathematical expressions |
| `count_lines` | Count lines of code by file type |

### Security Management
| Tool | Description |
|------|-------------|
| `manage_blacklist` | Add/remove commands from blacklist |
| `manage_approved` | List/clear approved commands |
| `get_dangerous_patterns` | Show blocked dangerous patterns |

### Custom Tools (./workspace/tools/)
| Tool | Description |
|------|-------------|
| `greeting` | Generate personalized greetings |
| `add_numbers` | Add two numbers |
| `word_reverser` | Reverse words in a sentence |

### Meta Tools
| Tool | Description |
|------|-------------|
| `create_tool` | Create a new tool dynamically (for LLMs) |

See [AGENTS.md](AGENTS.md) for detailed documentation on creating tools.

## MCP Resources

Resources expose context data that the LLM can read on demand:

| Resource | Description |
|----------|-------------|
| `diamcp://context/time` | Current date and time |
| `diamcp://context/system` | System information |
| `diamcp://workspace/summary` | Workspace contents overview |
| `diamcp://tools/list` | List of all available tools |

## MCP Prompts

Reusable prompt templates for common workflows:

| Prompt | Description |
|--------|-------------|
| `startup_context` | Call get_time and get_system_info first |
| `file_search_first` | Use search_files before reading files |
| `web_research` | Web search then fetch workflow |
| `code_review` | Steps for reviewing code |

## Adding Custom Tools

Create a Python file in `./workspace/tools/`:

```python
from base import tool

@tool(
    name="my_tool",
    description="What my tool does",
    schema={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Input"}
        },
        "required": ["input"]
    }
)
def my_tool(input: str) -> str:
    return f"Processed: {input}"
```

Restart the server to load new tools (`.\start.ps1` natively, `./restart.sh` with Docker).

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Host to bind to |
| `MCP_PORT` | `8000` | Port to listen on |
| `MCP_CORS_ORIGINS` | loopback only | Comma-separated allowed browser origins, or `*` for any |
| `DIAMCP_WORKSPACE` | `./workspace` (`/workspace` in Docker) | Workspace root |

### Browser (CORS) access

The webui runs on a different port than diaMCP, so a direct browser connection is
cross-origin. The server sends CORS headers for `localhost` / `127.0.0.1` origins on any
port, which covers the webui without extra configuration. To allow a different origin:

```powershell
$env:MCP_CORS_ORIGINS = "http://my-host:8080"
```

Setting `*` allows any website in your browser to reach the server - and `run_command`
executes shell commands - so only do that on a trusted, isolated machine.

### Docker Compose

```yaml
services:
  diamcp:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./workspace:/workspace
```

The workspace volume persists your files and custom tools across restarts.

## Architecture

```
diamcp/
├── server.py          # Main MCP server (FastMCP)
├── base.py            # Tool decorator & registry (copy to /app/tools/)
├── builtin.py         # Built-in tools (copy to /app/tools/)
├── tools/             # Internal diaMCP tools (copied to /app/tools/)
│   ├── examples/      # Reference examples - copy to workspace/tools/ to use
│   │   ├── useful_tools.py
│   │   ├── weather_tool.py
│   │   └── example_tools.py
├── install.sh         # One-line install/update script (Linux/macOS)
├── install.ps1        # Legacy Windows installer (Docker + WSL2 path)
├── start.ps1          # Native Windows launcher (no Docker/WSL2)
├── start.cmd          # Same, for cmd.exe / Miniforge Prompt
├── restart.sh         # Rebuild and restart script (Docker)
├── config/            # Configuration
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-conda.txt  # Same deps as conda-forge package names
└── workspace/         # Persistent workspace (./workspace natively, /workspace in Docker)
    └── tools/         # Your custom tools go here
```

## Tool Locations

| Location | Purpose | Persists? |
|----------|---------|-----------|
| `tools/` | Built-in diaMCP tools | No - internal to the server |
| `tools/examples/` | Reference examples | No - copy to workspace to use |
| `workspace/tools/` | Your custom tools | Yes - survives updates |

To use an example tool, copy it from `tools/examples/` to `workspace/tools/`, then restart the server.

## Requirements

### Native (Windows/Linux/macOS)

- Python 3.10+
- `pip install -r requirements.txt`
- Git on PATH (only for the git tools)
- llama.cpp webui with MCP support (`--webui-mcp-proxy` flag on llama-server)

### Docker (Linux/macOS)

- Docker
- Docker Compose
- llama.cpp webui with MCP support (`--webui-mcp-proxy` flag on llama-server)

## License

MIT License - see LICENSE file for details.
