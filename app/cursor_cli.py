from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from .config import Settings

MODEL_LINE_RE = re.compile(r"^(?P<id>\S+)\s+-\s+(?P<label>.+?)(?P<flags>(?:\s+\(.+?\))*)$")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class CursorCLIResult:
    text: str
    session_id: str | None = None
    request_id: str | None = None
    duration_ms: int | None = None


class CursorCLIError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class CursorCLIAdapter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _resolve_executable(self) -> str | None:
        candidate = self.settings.cursor_bin
        if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
            path = Path(candidate)
            return str(path) if path.exists() else None
        return shutil.which(candidate)

    def cli_status(self) -> dict[str, str | bool | None]:
        resolved = self._resolve_executable()
        return {
            "configured_bin": self.settings.cursor_bin,
            "resolved_bin": resolved,
            "available": resolved is not None,
        }

    def _ensure_cli_available(self) -> None:
        if self._resolve_executable():
            return
        raise CursorCLIError(
            "Cursor CLI executable was not found. "
            f"Current CURSOR_BIN is '{self.settings.cursor_bin}'. "
            "Please install Cursor Agent CLI and make sure `agent --version` works, "
            "or set CURSOR_BIN to the full executable path."
        )

    def _build_executable_prefix(self) -> list[str]:
        resolved = self._resolve_executable() or self.settings.cursor_bin

        if os.name != "nt":
            return [resolved]

        path = Path(resolved)
        suffix = path.suffix.lower()
        powershell = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
        )
        cmd_exe = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"),
            "System32",
            "cmd.exe",
        )

        if suffix == ".ps1":
            return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]

        if suffix == ".cmd":
            sibling_ps1 = path.with_suffix(".ps1")
            if sibling_ps1.exists():
                return [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(sibling_ps1)]
            return [cmd_exe, "/c", str(path)]

        if suffix == ".bat":
            return [cmd_exe, "/c", str(path)]

        return [resolved]

    def _build_command(self, prompt: str, model: str, *, stream: bool) -> list[str]:
        command = [
            *self._build_executable_prefix(),
            "-p",
            prompt,
            "--output-format",
            "stream-json" if stream else "json",
            "--workspace",
            self.settings.cursor_workspace,
        ]

        if stream:
            command.append("--stream-partial-output")
        if model:
            command.extend(["--model", model])
        if self.settings.trust_workspace:
            command.append("--trust")
        if self.settings.approve_mcps:
            command.append("--approve-mcps")
        if self.settings.force:
            command.append("--force")
        if self.settings.sandbox:
            command.extend(["--sandbox", self.settings.sandbox])

        return command

    def _build_models_command(self) -> list[str]:
        return [*self._build_executable_prefix(), "models"]

    def _parse_models_output(self, output: str) -> list[dict[str, str | bool]]:
        models: list[dict[str, str | bool]] = []
        in_models_section = False

        for raw_line in output.splitlines():
            line = ANSI_ESCAPE_RE.sub("", raw_line).replace("\r", "").strip()
            if not line:
                continue
            if "Available models" in line:
                in_models_section = True
                continue
            if not in_models_section:
                continue
            if line.startswith("Tip:"):
                break

            match = MODEL_LINE_RE.match(line)
            if not match:
                continue

            flags = match.group("flags") or ""
            models.append(
                {
                    "id": match.group("id"),
                    "label": match.group("label").strip(),
                    "is_default": "(default)" in flags,
                    "is_current": "(current)" in flags,
                }
            )

        if not models:
            raise CursorCLIError("Cursor CLI returned an empty model list.", stderr=output)

        return models

    async def list_available_models(self) -> list[dict[str, str | bool]]:
        self._ensure_cli_available()
        command = self._build_models_command()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.cursor_workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CursorCLIError(
                "Cursor CLI executable could not be started. "
                f"Current CURSOR_BIN is '{self.settings.cursor_bin}'."
            ) from exc

        stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            raise CursorCLIError(
                stderr_text or "Cursor CLI model listing failed.",
                exit_code=process.returncode,
                stderr=stderr_text,
            )

        return self._parse_models_output(stdout_text)

    @staticmethod
    def _is_summary_assistant_event(payload: dict) -> bool:
        """Distinguish summary events from streaming delta events.

        With --stream-partial-output, each assistant segment is emitted twice:
        once as a delta (with timestamp_ms) and once as a summary (without).
        We only process summary events to avoid duplication.
        """
        return payload.get("type") == "assistant" and "timestamp_ms" not in payload

    @staticmethod
    def _extract_assistant_text_from_ndjson(raw: str) -> tuple[str, str | None, str | None, int | None]:
        """Parse NDJSON output and return only the last assistant message segment."""
        last_assistant_text = ""
        session_id: str | None = None
        request_id: str | None = None
        duration_ms: int | None = None

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = payload.get("type")

            if CursorCLIAdapter._is_summary_assistant_event(payload):
                parts = payload.get("message", {}).get("content", [])
                text_parts = [p["text"] for p in parts if p.get("type") == "text" and p.get("text")]
                if text_parts:
                    last_assistant_text = "".join(text_parts)

            elif event_type == "result":
                session_id = payload.get("session_id")
                request_id = payload.get("request_id")
                duration_ms = payload.get("duration_ms")

            elif event_type == "system":
                session_id = session_id or payload.get("session_id")

        return last_assistant_text, session_id, request_id, duration_ms

    async def run_chat(self, prompt: str, model: str) -> CursorCLIResult:
        self._ensure_cli_available()
        command = self._build_command(prompt, model, stream=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.cursor_workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CursorCLIError(
                "Cursor CLI executable could not be started. "
                f"Current CURSOR_BIN is '{self.settings.cursor_bin}'."
            ) from exc
        stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            raise CursorCLIError(
                stderr_text or "Cursor CLI execution failed.",
                exit_code=process.returncode,
                stderr=stderr_text,
            )

        if not stdout_text:
            raise CursorCLIError("Cursor CLI returned an empty response.", stderr=stderr_text)

        text, session_id, request_id, duration_ms = self._extract_assistant_text_from_ndjson(stdout_text)

        return CursorCLIResult(
            text=text,
            session_id=session_id,
            request_id=request_id,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _format_tool_call_hint(payload: dict) -> str | None:
        """Build a human-readable hint from a tool_call started event."""
        if payload.get("subtype") != "started":
            return None

        tool_call = payload.get("tool_call", {})

        for key, value in tool_call.items():
            tool_name = key
            args = value.get("args", {})
            break
        else:
            return None

        args_summary = ", ".join(f"{k}: {v}" for k, v in args.items() if k != "toolCallId")
        if args_summary:
            return f"\n\n> 🔧 调用工具: {tool_name} ({args_summary})\n\n"
        return f"\n\n> 🔧 调用工具: {tool_name}\n\n"

    async def stream_chat(self, prompt: str, model: str) -> AsyncIterator[str]:
        """Real-time streaming with tool call hints.

        Uses delta events (with timestamp_ms) for real-time text output.
        Inserts tool call hints when the Agent invokes tools.
        Skips summary events (without timestamp_ms) to avoid duplication.
        """
        self._ensure_cli_available()
        command = self._build_command(prompt, model, stream=True)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.cursor_workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CursorCLIError(
                "Cursor CLI executable could not be started. "
                f"Current CURSOR_BIN is '{self.settings.cursor_bin}'."
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(process.stderr.read())
        thinking_started = False

        try:
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = payload.get("type")
                subtype = payload.get("subtype", "")

                if event_type == "thinking" and subtype == "delta":
                    if not thinking_started:
                        thinking_started = True
                        yield "> 💭 **思考中：**\n> "
                    text = payload.get("text", "")
                    if text:
                        yield text.replace("\n", "\n> ")

                elif event_type == "thinking" and subtype == "completed":
                    if thinking_started:
                        thinking_started = False
                        yield "\n\n"

                elif event_type == "assistant" and "timestamp_ms" in payload:
                    if thinking_started:
                        thinking_started = False
                        yield "\n\n"
                    message = payload.get("message", {})
                    content = message.get("content", [])
                    for part in content:
                        if part.get("type") == "text" and part.get("text"):
                            yield part["text"]

                elif event_type == "tool_call":
                    hint = self._format_tool_call_hint(payload)
                    if hint:
                        yield hint

            stderr_bytes = await stderr_task
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            return_code = await process.wait()
            if return_code != 0:
                raise CursorCLIError(
                    stderr_text or "Cursor CLI streaming failed.",
                    exit_code=return_code,
                    stderr=stderr_text,
                )
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
