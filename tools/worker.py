"""Disposable worker for regex matching and arbitrary Python evaluation."""

import contextlib
import io
import json
import os
import re
import sys

from base import workspace_path, workspace_relative
from execution import MAX_OUTPUT, encode_response, failure


def grep_worker(arguments: dict) -> dict:
    root = workspace_path(arguments["path"])
    if not root.exists():
        return failure("not_found", f"Path '{arguments['path']}' not found")
    try:
        regex = re.compile(arguments["pattern"])
    except re.error as exc:
        return failure("invalid_regex", str(exc))
    matches = []
    scanned = skipped = chars = 0
    warnings = []
    candidates = [root] if root.is_file() else root.rglob(arguments["file_pattern"])
    for candidate in candidates:
        # Never follow links outside the workspace (or recurse through linked dirs).
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            candidate = workspace_path(str(candidate))
            if candidate.stat().st_size > arguments["max_file_bytes"]:
                skipped += 1
                continue
            scanned += 1
            with candidate.open("r", encoding="utf-8") as source:
                for number, line in enumerate(source, 1):
                    if "\x00" in line:
                        skipped += 1
                        break
                    if regex.search(line):
                        text = line.rstrip()[:1000]
                        matches.append({"path": workspace_relative(candidate), "line": number, "text": text})
                        chars += len(text) + len(str(candidate)) + 100
                        if len(matches) >= arguments["max_results"] or chars >= MAX_OUTPUT // 8:
                            return {"ok": True, "result": {"matches": matches, "scanned_files": scanned,
                                    "skipped_files": skipped, "warnings": warnings}, "truncated": True}
        except (OSError, UnicodeError, ValueError) as exc:
            skipped += 1
            if len(warnings) < 5:
                warnings.append(f"{candidate.name}: {exc}")
    return {"ok": True, "result": {"matches": matches, "scanned_files": scanned,
                                   "skipped_files": skipped, "warnings": warnings}, "truncated": False}


class LimitedCapture(io.TextIOBase):
    def __init__(self):
        self.parts = []
        self.remaining = MAX_OUTPUT // 8
        self.truncated = False

    def write(self, text):
        if self.remaining:
            self.parts.append(text[:self.remaining])
        self.truncated |= len(text) > self.remaining
        self.remaining = max(0, self.remaining - len(text))
        return len(text)


def eval_worker(arguments: dict) -> dict:
    capture = LimitedCapture()
    namespace = {"__builtins__": __builtins__, "__name__": "__main__"}
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
        try:
            compiled = compile(arguments["code"], "<python_eval>", "eval")
        except SyntaxError:
            exec(compile(arguments["code"], "<python_eval>", "exec"), namespace)
            value = None
        else:
            value = eval(compiled, namespace)
        value_text = str(value)[:MAX_OUTPUT // 8] if value is not None else None
    return {"ok": True, "result": {"value": value_text, "output": "".join(capture.parts)},
            "truncated": capture.truncated}


def calculate_worker(arguments: dict) -> dict:
    import math

    names = {k: v for k, v in math.__dict__.items() if not k.startswith("_")}
    names.update({"abs": abs, "round": round, "min": min, "max": max, "pow": pow})
    value = eval(arguments["expression"], {"__builtins__": {}, **names})
    return {"ok": True, "result": {"value": str(value)[:MAX_OUTPUT // 8], "output": ""}}


def main():
    try:
        arguments = json.load(sys.stdin)
        if sys.argv[1] == "grep":
            payload = grep_worker(arguments)
        elif sys.argv[1] == "python_eval":
            os.chdir(workspace_path())
            payload = eval_worker(arguments)
        elif sys.argv[1] == "calculate":
            payload = calculate_worker(arguments)
        else:
            payload = failure("invalid_arguments", "Unknown worker operation")
    except BaseException as exc:
        payload = failure("execution_error", f"{type(exc).__name__}: {exc}")
    sys.stdout.write(encode_response(payload))
    sys.stdout.flush()


if __name__ == "__main__":
    main()