"""Regression tests: run with python -m unittest discover -s tests -v."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import base
import builtin
import execution


class RobustnessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        server.register_builtin_tools()
        server.register_tools()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name).resolve()
        self.paths = patch.object(base, "_WORKSPACE_DIR", self.root)
        self.env = patch.dict(os.environ, {"DIAMCP_WORKSPACE": str(self.root)})
        self.paths.start()
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.paths.stop)
        self.addCleanup(self.env.stop)

    async def call(self, name, arguments):
        payload = await server.mcp.run_tool(name, arguments)
        result = await server.mcp.call_tool(name, arguments)
        text = result.content[0].text
        self.assertEqual(result.isError, not payload["ok"])
        self.assertLessEqual(len(text), execution.MAX_OUTPUT + 200)
        if not payload["ok"]:
            self.assertTrue(text.startswith("[ERROR: "), text)
        return payload

    async def test_timeout_text_tells_model_what_to_do(self):
        text = execution.render_response(execution.failure("timeout", "grep timed out", True))
        self.assertIn("[ERROR: timeout]", text)
        self.assertIn("Do NOT repeat", text)
        text = execution.render_response(execution.failure("busy", "busy", True))
        self.assertIn("retry once", text)

    async def test_plain_text_rendering(self):
        (self.root / "a.txt").write_text("hello\n")
        result = await server.mcp.call_tool("grep", {"pattern": "hello"})
        self.assertIn("a.txt:1: hello", result.content[0].text)
        result = await server.mcp.call_tool("run_command", {"command": "Write-Output hi"})
        self.assertIn("STDOUT:", result.content[0].text)
        self.assertIn("Exit code: 0", result.content[0].text)

    async def test_internal_ctx_parameter_is_hidden_and_bad_args_are_concise(self):
        tools = {t.name: t for t in await server.mcp.list_tools()}
        self.assertNotIn("ctx", tools["run_command"].inputSchema["properties"])
        for tool in tools.values():
            self.assertNotIn("null", json.dumps(tool.inputSchema), tool.name)
        result = await self.call("web_fetch", {"max_length": "abc"})
        message = result["error"]["message"]
        self.assertIn("url", message)
        self.assertIn("Expected: url, max_length (optional)", message)
        self.assertLess(len(message), 400)

    async def test_large_command_output_is_truncated_not_failed(self):
        command = f"'x' * {execution.MAX_OUTPUT * 3}" if os.name == "nt" else f"head -c {execution.MAX_OUTPUT * 3} /dev/zero | tr '\\0' x"
        result = await self.call("run_command", {"command": command})
        self.assertTrue(result["ok"])
        self.assertTrue(result["truncated"])
        text = (await server.mcp.call_tool("run_command", {"command": command})).content[0].text
        self.assertIn("[TRUNCATED", text)
        self.assertNotIn("[ERROR", text)

    async def test_null_optional_arguments_use_defaults(self):
        result = await self.call("grep", {"pattern": "x", "path": None, "timeout": None, "max_results": None})
        self.assertTrue(result["ok"])
        result = await self.call("read_file", {"path": None})
        self.assertEqual(result["error"]["code"], "invalid_arguments")

    async def test_busy_run_command(self):
        with patch.object(execution, "_slots", threading.BoundedSemaphore(1)) as slots:
            slots.acquire()
            try:
                result = await self.call("run_command", {"command": "Write-Output hi"})
                self.assertEqual(result["error"]["code"], "busy")
            finally:
                slots.release()

    @staticmethod
    def _alive(pid):
        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            status = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(status))
            return status.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    async def test_descendants_are_killed_after_parent_exits(self):
        code = ("import subprocess, sys; "
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "print(c.pid)")
        result = await execution.run_sync(execution.run_process, [sys.executable, "-c", code], 10)
        pid = int(result["stdout"].strip())
        for _ in range(40):
            if not self._alive(pid):
                break
            await asyncio.sleep(0.1)
        self.assertFalse(self._alive(pid), "descendant outlived the tool call")

    async def test_huge_calculation_is_terminated_promptly(self):
        start = time.monotonic()
        result = await self.call("calculate", {"expression": "factorial(2000000)", "timeout": 1})
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual((await self.call("calculate", {"expression": "sqrt(16) + 2"}))["result"]["value"], "6.0")

    async def test_builtin_defaults_respect_configured_maximum(self):
        self.assertEqual(execution.default_timeout(10), 10)
        with patch.object(execution, "MAX_TIMEOUT", 5.0):
            self.assertEqual(execution.default_timeout(10), 5)
            self.assertEqual(execution.timeout_seconds(execution.default_timeout(10)), 5.0)

    async def test_success_and_empty_search(self):
        self.assertTrue((await self.call("get_time", {}))["ok"])
        result = await self.call("grep", {"pattern": "absent"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["matches"], [])

    async def test_invalid_regex(self):
        result = await self.call("grep", {"pattern": "["})
        self.assertEqual(result["error"]["code"], "invalid_regex")

    async def test_invalid_arguments_and_unknown_tool(self):
        for name, args, code in (("read_file", {}, "invalid_arguments"),
                                 ("missing", {}, "unknown_tool"),
                                 ("grep", {"pattern": "x", "max_results": 0}, "invalid_arguments")):
            self.assertEqual((await self.call(name, args))["error"]["code"], code)

    async def test_invalid_timeouts(self):
        for timeout in (0, -1, True, "forever", float("inf"), float("nan"), 999999):
            result = await self.call("grep", {"pattern": "x", "timeout": timeout})
            self.assertEqual(result["error"]["code"], "invalid_arguments")

    async def test_grep_file_and_early_limit(self):
        (self.root / "sample.txt").write_text("hello\nhello again\n", encoding="utf-8")
        result = await self.call("grep", {"pattern": "hello", "path": "sample.txt", "max_results": 1})
        self.assertEqual(len(result["result"]["matches"]), 1)
        self.assertEqual(result["result"]["matches"][0]["line"], 1)
        self.assertTrue(result["truncated"])

    async def test_grep_skips_large_and_reports_unreadable_files(self):
        (self.root / "large.txt").write_text("x" * 100)
        (self.root / "invalid.txt").write_bytes(b"\xff\xfe")
        result = await self.call("grep", {"pattern": "x", "max_file_bytes": 10})
        self.assertEqual(result["result"]["skipped_files"], 2)
        self.assertTrue(result["result"]["warnings"])

    async def test_catastrophic_regex_is_terminated(self):
        (self.root / "regex.txt").write_text("a" * 10000 + "!")
        start = time.monotonic()
        result = await self.call("grep", {"pattern": "(a+)+$", "timeout": 1})
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue((await self.call("get_time", {}))["ok"])

    async def test_python_value_print_and_exception(self):
        result = await self.call("python_eval", {"code": "1 + 2"})
        self.assertEqual(result["result"]["value"], "3")
        result = await self.call("python_eval", {"code": "print('hello')"})
        self.assertEqual(result["result"]["output"], "hello\n")
        result = await self.call("python_eval", {"code": "1 / 0"})
        self.assertEqual(result["error"]["code"], "execution_error")

    async def test_python_infinite_loop_is_killed(self):
        result = await self.call("python_eval", {"code": "while True: pass", "timeout": 1})
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertTrue((await self.call("python_eval", {"code": "2 + 2"}))["ok"])

    async def test_worker_deadline_kills_actual_process(self):
        pid_file = self.root / "worker.pid"
        with self.assertRaises(execution.ToolFailure):
            await execution.run_sync(builtin.python_eval,
                "import os\nfrom pathlib import Path\nPath('worker.pid').write_text(str(os.getpid()))\nwhile True: pass", 1)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    status = ctypes.c_ulong()
                    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(status))
                    self.assertNotEqual(status.value, 259, "worker is still running")
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_read_file_content_is_not_mistaken_for_error(self):
        (self.root / "message.txt").write_text("Error: this is file content")
        result = await self.call("read_file", {"path": "message.txt"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], "Error: this is file content")

    async def test_workspace_escape_and_missing_file(self):
        for path in ("../outside.txt", "does-not-exist.txt"):
            self.assertFalse((await self.call("read_file", {"path": path}))["ok"])

    async def test_output_is_bounded_and_json_valid(self):
        (self.root / "large.txt").write_text("\x01" * (execution.MAX_OUTPUT * 2))
        result = await self.call("read_file", {"path": "large.txt"})
        self.assertTrue(result["truncated"])
        result = await self.call("python_eval", {"code": "print('x' * 100000)"})
        self.assertTrue(result["truncated"])

    async def test_nonzero_command_is_error(self):
        command = "exit 7"
        result = await self.call("run_command", {"command": command})
        self.assertEqual(result["error"]["code"], "command_failed")
        self.assertEqual(result["result"]["exit_code"], 7)

    async def test_command_deadline_does_not_block_server(self):
        command = "Start-Sleep -Seconds 30" if os.name == "nt" else "sleep 30"
        task = asyncio.create_task(self.call("run_command", {"command": command, "timeout": 1}))
        other = await asyncio.wait_for(self.call("get_time", {}), 0.75)
        self.assertTrue(other["ok"])
        self.assertEqual((await task)["error"]["code"], "timeout")

    async def test_git_failure_and_deadline(self):
        with patch.object(builtin, "run_process", return_value={"stdout": "", "stderr": "fatal", "exit_code": 128, "truncated": False}):
            result = await self.call("git_pull", {"path": "."})
            self.assertEqual(result["error"]["code"], "command_failed")
        with patch.object(builtin, "run_process", side_effect=subprocess.TimeoutExpired("git", 1)):
            self.assertEqual((await self.call("git_pull", {"path": ".", "timeout": 1}))["error"]["code"], "timeout")

    async def test_custom_sync_timeout_and_exception(self):
        release = threading.Event()
        def custom_wait():
            release.wait(5)
            return "done"
        def custom_error():
            raise RuntimeError("custom failure")
        for fn in (custom_wait, custom_error):
            base.ToolRegistry.register(fn.__name__, "Test tool", fn, {})
            server.mcp.add_tool(execution.async_tool(fn), name=fn.__name__, structured_output=False)
        try:
            with patch.object(execution, "DEFAULT_TIMEOUT", 0.05):
                self.assertEqual((await self.call("custom_wait", {}))["error"]["code"], "timeout")
            self.assertEqual((await self.call("custom_error", {}))["error"]["code"], "tool_error")
        finally:
            release.set()

    async def test_busy_workers_fail_immediately(self):
        with patch.object(execution, "_slots", threading.BoundedSemaphore(1)) as slots:
            slots.acquire()
            try:
                result = await self.call("get_time", {})
                self.assertEqual(result["error"]["code"], "busy")
            finally:
                slots.release()

    async def test_blocked_command(self):
        result = await self.call("run_command", {"command": "shutdown /s"})
        self.assertEqual(result["error"]["code"], "blocked")

    async def test_process_output_limit(self):
        result = await execution.run_sync(execution.run_process,
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"], 5)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"]), execution.MAX_OUTPUT)

    async def test_http_mcp_wire_contract(self):
        import uvicorn
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        host = uvicorn.Server(uvicorn.Config(server.build_app(), log_level="error", lifespan="on"))
        task = asyncio.create_task(host.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not host.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (reader, writer, _):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    grep = next(tool for tool in tools.tools if tool.name == "grep")
                    self.assertIn("timeout", grep.inputSchema["properties"])
                    for name, args, failed in (("grep", {"pattern": "["}, True), ("read_file", {}, True), ("get_time", {}, False)):
                        response = await session.call_tool(name, args)
                        self.assertEqual(response.isError, failed)
                        self.assertEqual(response.content[0].text.startswith("[ERROR: "), failed)
        finally:
            host.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()


if __name__ == "__main__":
    unittest.main()