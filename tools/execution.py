"""Bounded execution and a consistent JSON contract for MCP tool calls."""

import asyncio
import contextvars
import functools
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_TIMEOUT = float(os.environ.get("DIAMCP_TOOL_TIMEOUT", "30"))
MAX_TIMEOUT = float(os.environ.get("DIAMCP_MAX_TOOL_TIMEOUT", "120"))
MAX_OUTPUT = int(os.environ.get("DIAMCP_MAX_OUTPUT_CHARS", "20000"))
MAX_WORKERS = int(os.environ.get("DIAMCP_MAX_WORKERS", "8"))
if (not math.isfinite(DEFAULT_TIMEOUT) or not math.isfinite(MAX_TIMEOUT)
        or not 1 <= DEFAULT_TIMEOUT <= MAX_TIMEOUT or MAX_OUTPUT < 1024 or MAX_WORKERS < 1):
    raise ValueError("Invalid diaMCP execution limits (timeouts must be >= 1 second, default <= max)")


def default_timeout(preferred: int) -> int:
    """A tool's own default deadline, never above the configured maximum."""
    return max(1, min(preferred, int(MAX_TIMEOUT)))

_slots = threading.BoundedSemaphore(MAX_WORKERS)
_process_control = contextvars.ContextVar("diamcp_process_control", default=None)


class ToolPayload(dict):
    """Explicit response envelope, distinct from arbitrary file/JSON contents."""


class ToolFailure(Exception):
    """An expected failure with a machine-readable error code."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def timeout_seconds(value=None) -> float:
    """Validate requested deadlines, including calls made outside MCP."""
    value = DEFAULT_TIMEOUT if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolFailure("invalid_arguments", "timeout must be a positive number")
    if not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT:
        raise ToolFailure("invalid_arguments", f"timeout must be greater than 0 and at most {MAX_TIMEOUT:g} seconds")
    return float(value)


def failure(code: str, message: str, retryable: bool = False) -> dict:
    return {"ok": False, "error": {"code": code, "message": message[:MAX_OUTPUT], "retryable": retryable}}


def normalize_result(value) -> dict:
    """Adapt explicit envelopes and the legacy tools' error prefixes."""
    if isinstance(value, ToolPayload):
        return value
    if isinstance(value, str):
        if value.startswith(("Error", "Invalid regex:", "[BLOCKED]", "Execution timed out", "Unknown action:")):
            code = "tool_error"
            if value.startswith("Invalid regex:"):
                code = "invalid_regex"
            elif value.startswith("[BLOCKED]"):
                code = "blocked"
            elif "timed out" in value.lower():
                code = "timeout"
            return failure(code, value, code == "timeout")
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
    return {"ok": True, "result": value}


def encode_response(payload: dict) -> str:
    """Limit serialized responses while always producing valid JSON."""
    encoded = json.dumps(payload, ensure_ascii=True, default=str, allow_nan=False)
    if len(encoded) > MAX_OUTPUT:
        # A JSON preview avoids emitting an arbitrarily large nested object.
        payload = {
            "ok": payload["ok"], "truncated": True,
            "original_chars": len(encoded),
            "result": "Response exceeded the output limit; narrow the request.",
            "preview": encoded[:MAX_OUTPUT // 8],
            **({"error": payload["error"]} if not payload["ok"] else {}),
        }
        if "error" in payload:
            payload["error"] = {**payload["error"], "message": payload["error"]["message"][:MAX_OUTPUT // 8]}
        encoded = json.dumps(payload, ensure_ascii=True, default=str)
    return encoded


_HINTS = {
    "timeout": "STOPPED: the time limit was reached and the work was terminated. Do NOT repeat the same call. Narrow the request (smaller path, simpler pattern, fewer files) or try a different approach.",
    "busy": "WAIT: the server is busy. Wait a moment, then retry once. If it is still busy, tell the user.",
    "invalid_arguments": "FIX: correct the arguments and call again.",
    "invalid_regex": "FIX: the regex is invalid; correct it and call again.",
    "blocked": "STOP: this command is not allowed. Do not retry it or a variant; ask the user.",
    "output_limit": "NARROW: the output was too large and the work was stopped. Narrow the request.",
    "not_found": "CHECK: the path does not exist. List the directory to find the right path.",
    "unknown_tool": "STOP: no such tool. Use one of the listed tools.",
    "command_failed": "The command ran but failed. Read the output, fix the cause, or try a different approach.",
}


def _render_body(result) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and "exit_code" in result:
        parts = []
        if result.get("stdout"):
            parts.append(f"STDOUT:\n{result['stdout']}")
        if result.get("stderr"):
            parts.append(f"STDERR:\n{result['stderr']}")
        parts.append("Exit code: " + ("n/a (output limit reached; command was stopped)" if result["exit_code"] is None else str(result["exit_code"])))
        return "\n".join(parts)
    if isinstance(result, dict) and "matches" in result:
        lines = [f"{m['path']}:{m['line']}: {m['text']}" for m in result["matches"]]
        lines.append(f"({len(result['matches'])} matches, {result['scanned_files']} files scanned, {result['skipped_files']} skipped)")
        lines += [f"warning: {w}" for w in result.get("warnings", [])]
        return "\n".join(lines)
    if isinstance(result, dict) and "output" in result and "value" in result:
        parts = [result["output"]] if result["output"] else []
        if result["value"] is not None:
            parts.append(result["value"])
        return "\n".join(parts) or "Executed successfully"
    if isinstance(result, list) and all(isinstance(i, str) for i in result):
        return "\n".join(result) or "(empty)"
    return json.dumps(result, default=str, ensure_ascii=False)


def render_response(payload: dict) -> str:
    """Plain text for the model: a status line plus what to do next, bounded in size."""
    body = _render_body(payload.get("result"))
    notes = []
    if payload.get("ok"):
        text = body
    else:
        err = payload["error"]
        head = f"[ERROR: {err['code']}] {err['message']}"
        hint = _HINTS.get(err["code"])
        if hint is None and err.get("retryable"):
            hint = "You may retry once."
        text = "\n".join(p for p in (head, hint, body) if p)
    if payload.get("truncated") or len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT]
        notes.append(f"[TRUNCATED: output limited to {MAX_OUTPUT} characters; narrow the request]")
    return "\n".join([text, *notes]) if text else "(no output)"


async def run_sync(func, *args, **kwargs):
    """Offload blocking calls without an unbounded queue or executor shutdown wait.

    Python cannot kill a thread. Slots stay occupied until the actual work exits;
    risky CPU work uses killable subprocesses instead.
    """
    if not _slots.acquire(blocking=False):
        raise ToolFailure("busy", "All tool workers are occupied; try again later.", True)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    context = contextvars.copy_context()
    control = {"cancelled": threading.Event(), "active": threading.Event()}
    context.run(_process_control.set, control)

    def deliver(value, error):
        if not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

    def worker():
        try:
            try:
                value, error = context.run(func, *args, **kwargs), None
            except BaseException as exc:
                value, error = None, RuntimeError(str(exc)) if not isinstance(exc, Exception) else exc
            try:
                loop.call_soon_threadsafe(deliver, value, error)
            except RuntimeError:
                pass  # The client/server event loop has already closed.
        finally:
            _slots.release()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except BaseException:
        _slots.release()
        raise
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        control["cancelled"].set()
        # A subprocess needs bounded cleanup before replying, especially on
        # Windows where open files/cwd prevent subsequent operations and deletion.
        if control["active"].is_set():
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=7)
            except Exception:
                pass
        future.cancel()
        raise


def async_tool(func):
    """Preserve the tool's input signature while keeping sync work off the loop."""
    @functools.wraps(func)
    async def wrapped(*args, **kwargs):
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return await run_sync(func, *args, **kwargs)
    return wrapped


def _kill_tree(process):
    """Fallback used when no Job Object is available (Windows) before the root exits."""
    if process.poll() is None:
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _Accounting(ctypes.Structure):
        _fields_ = [("Times", ctypes.c_int64 * 4), ("TotalPageFaultCount", wintypes.DWORD),
                    ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD),
                    ("TotalTerminatedProcesses", wintypes.DWORD)]

    def _job_active(job) -> int:
        info = _Accounting()
        if not _k32.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            return 0
        return info.ActiveProcesses


def _make_job(process):
    """Put the child in a kill-on-close Job Object so descendants can be reaped (Windows)."""
    if os.name != "nt":
        return None
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    if (_k32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            and _k32.AssignProcessToJobObject(job, int(process._handle))):
        return job
    _k32.CloseHandle(job)
    return None


def _reap(process, job):
    """Kill the child and everything it started, including after a normal exit."""
    if os.name == "nt":
        if job:
            _k32.TerminateJobObject(job, 1)
        else:
            _kill_tree(process)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    if job:
        deadline = time.monotonic() + 3
        while _job_active(job) and time.monotonic() < deadline:
            time.sleep(0.02)
        _k32.CloseHandle(job)


def run_process(args, timeout=None, *, cwd=None, shell=False, input_text=None) -> dict:
    """Associate subprocess cleanup with cancellation of its async tool call."""
    control = _process_control.get()
    if control is not None:
        control["active"].set()
    try:
        if control is not None and control["cancelled"].is_set():
            raise subprocess.TimeoutExpired(args, timeout)
        return _run_process(args, timeout, cwd=cwd, shell=shell, input_text=input_text)
    finally:
        if control is not None:
            control["active"].clear()


def _run_process(args, timeout=None, *, cwd=None, shell=False, input_text=None) -> dict:
    """Run a child with a wall deadline, tree cleanup, and bounded captured output."""
    seconds = timeout_seconds(timeout)
    # Files, rather than PIPEs, prevent descendants holding stdout open forever
    # and keep noisy subprocesses from exhausting the server's memory.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
        if input_text is not None:
            stdin.write(input_text.encode("utf-8"))
            stdin.seek(0)
        process = subprocess.Popen(
            args, shell=shell, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=os.name != "nt",
        )
        output_limit = False
        deadline = time.monotonic() + seconds
        job = _make_job(process)
        try:
            while True:
                remaining = deadline - time.monotonic()
                control = _process_control.get()
                if remaining <= 0 or (control is not None and control["cancelled"].is_set()):
                    raise subprocess.TimeoutExpired(args, seconds)
                try:
                    process.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    if os.fstat(stdout.fileno()).st_size > MAX_OUTPUT or os.fstat(stderr.fileno()).st_size > MAX_OUTPUT:
                        output_limit = True
                        break
        finally:
            _reap(process, job)
        output = {}
        truncated = output_limit
        for key, stream in (("stdout", stdout), ("stderr", stderr)):
            stream.seek(0)
            data = stream.read(MAX_OUTPUT + 1)
            truncated |= len(data) > MAX_OUTPUT
            output[key] = data[:MAX_OUTPUT].decode("utf-8", errors="replace")
        # Killed at the output cap: the exit code is the kill's, not the command's.
        output.update(exit_code=None if output_limit else process.returncode, truncated=truncated)
        return output


def run_isolated(operation: str, arguments: dict, timeout=None) -> ToolPayload:
    """Execute built-in CPU work in a disposable, cross-platform interpreter."""
    seconds = timeout_seconds(timeout)
    try:
        result = run_process(
            [sys.executable, "-X", "utf8", str(Path(__file__).with_name("worker.py")), operation],
            seconds, input_text=json.dumps(arguments),
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolFailure("timeout", f"{operation} timed out after {seconds:g} seconds; worker terminated.", True) from exc
    if result["exit_code"]:
        raise ToolFailure("worker_error", result["stderr"] or f"Worker exited with code {result['exit_code']}")
    if result["truncated"]:
        raise ToolFailure("output_limit", "Worker response exceeded the output limit; narrow the request.")
    return ToolPayload(json.loads(result["stdout"]))