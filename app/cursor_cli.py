from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from .config import Settings

logger = logging.getLogger("cursor-wrapper")

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


async def _reap_after_stream_json_result_event(
    process: asyncio.subprocess.Process, ended_on_result: bool
) -> None:
    """若已收到 ``result`` 行但子进程仍存活，则等待其退出（必要时 terminate/kill）。"""
    if not ended_on_result or process.returncode is not None:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning(
            "cursor CLI still running after stream-json result event (pid=%s); terminating",
            process.pid,
        )
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def _finish_cursor_cli_stream_subprocess(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task,
    ended_on_result: bool,
    session_id: str | None,
    request_id: str | None,
    *,
    raise_on_error: bool,
) -> None:
    """``stream_chat`` 在 NDJSON 读完后调用：reap、读 stderr、``wait()`` 取退出码。"""
    try:
        await _reap_after_stream_json_result_event(process, ended_on_result)
        stderr_bytes = await stderr_task
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        return_code = await process.wait()
        if return_code != 0:
            if raise_on_error:
                raise CursorCLIError(
                    stderr_text or "Cursor CLI streaming failed.",
                    exit_code=return_code,
                    stderr=stderr_text,
                )
            logger.error(
                "cursor CLI exited with status %s after stream (session_id=%s request_id=%s): %s",
                return_code,
                session_id,
                request_id,
                stderr_text or "(empty stderr)",
            )
    except asyncio.CancelledError:
        raise
    except CursorCLIError:
        raise
    except Exception:
        logger.exception(
            "cursor CLI stream subprocess cleanup failed session_id=%s request_id=%s",
            session_id,
            request_id,
        )


class CursorCLIAdapter:
    _PROMPT_FILE_THRESHOLD = 4000
    _STDOUT_READ_CHUNK_SIZE = 64 * 1024

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @classmethod
    async def _iter_ndjson_lines(
        cls,
        stream: asyncio.StreamReader,
        *,
        boot_timings: dict[str, float] | None = None,
    ) -> AsyncIterator[str]:
        buffer = bytearray()
        first_read_done = False
        while True:
            chunk = await stream.read(cls._STDOUT_READ_CHUNK_SIZE)
            if not first_read_done:
                first_read_done = True
                if boot_timings is not None:
                    boot_timings["first_stdout_read_perf"] = time.perf_counter()
            if not chunk:
                break

            buffer.extend(chunk)
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    break

                raw_line = bytes(buffer[:newline_index])
                del buffer[: newline_index + 1]
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    if boot_timings is not None and "first_stdout_line_perf" not in boot_timings:
                        boot_timings["first_stdout_line_perf"] = time.perf_counter()
                    yield line

        if buffer:
            line = bytes(buffer).decode("utf-8", errors="replace").strip()
            if line:
                if boot_timings is not None and "first_stdout_line_perf" not in boot_timings:
                    boot_timings["first_stdout_line_perf"] = time.perf_counter()
                yield line

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
            "cursor_api_key_configured": bool(
                self.settings.cursor_api_key or os.environ.get("CURSOR_API_KEY")
            ),
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

    def _build_prefix_for_executable(self, executable: str) -> list[str]:
        if os.name != "nt":
            return [executable]

        path = Path(executable)
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

        return [executable]

    def _build_executable_prefix(self) -> list[str]:
        resolved = self._resolve_executable() or self.settings.cursor_bin
        return self._build_prefix_for_executable(resolved)

    @staticmethod
    def _basename_without_suffix(command: str) -> str:
        return Path(command).stem.lower()

    def _build_agent_entry_prefix(self) -> tuple[list[str], bool]:
        """Return (prefix, uses_cursor_frontend)."""
        resolved = self._resolve_executable() or self.settings.cursor_bin
        configured_name = self._basename_without_suffix(resolved)

        if configured_name == "cursor":
            return self._build_prefix_for_executable(resolved), True

        return self._build_prefix_for_executable(resolved), False

    def _build_command(self, prompt: str, model: str, *, stream: bool) -> list[str]:
        prefix, uses_cursor_frontend = self._build_agent_entry_prefix()
        command = [*prefix]
        if uses_cursor_frontend:
            command.append("agent")
        command.extend(
            [
                "-p",
                prompt,
                "--output-format",
                "stream-json" if stream else "json",
                "--workspace",
                self.settings.cursor_workspace,
            ]
        )
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

    def _build_subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.cursor_api_key:
            env["CURSOR_API_KEY"] = self.settings.cursor_api_key
        return env

    def _prompt_dir(self) -> Path:
        """Directory for persisted long prompts (not deleted after the run)."""
        d = Path(self.settings.cursor_workspace) / "prompt"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _prepare_prompt_input(self, prompt: str) -> tuple[str, str | None]:
        """Avoid oversized Windows command lines by externalizing large prompts."""
        if len(prompt) <= self._PROMPT_FILE_THRESHOLD:
            return prompt, None

        prompt_dir = self._prompt_dir()
        fd, file_path = tempfile.mkstemp(
            prefix="cursor-wrapper-prompt-",
            suffix=".txt",
            dir=str(prompt_dir),
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(prompt)

        bridged_prompt = (
            "The full prompt is stored in a local file. "
            "Read the file content and treat it as the complete user prompt/instructions.\n"
            f"Prompt file: {file_path}"
        )
        logger.info(
            "long prompt externalized (%d chars) -> %s",
            len(prompt),
            file_path,
        )
        return bridged_prompt, file_path

    def _build_models_command(self) -> list[str]:
        prefix, uses_cursor_frontend = self._build_agent_entry_prefix()
        command = [*prefix]
        if uses_cursor_frontend:
            command.append("agent")
        command.append("models")
        return command

    def _format_spawn_failure_message(self, command: list[str], exc: OSError) -> str:
        parts = [
            "Cursor CLI executable could not be started.",
            f"Current CURSOR_BIN is '{self.settings.cursor_bin}'.",
            f"command={command!r}",
            f"cwd='{self.settings.cursor_workspace}'",
            f"errno={getattr(exc, 'errno', None)!r}",
            f"winerror={getattr(exc, 'winerror', None)!r}",
            f"filename={getattr(exc, 'filename', None)!r}",
            f"filename2={getattr(exc, 'filename2', None)!r}",
            f"strerror={getattr(exc, 'strerror', None)!r}",
        ]
        return " ".join(parts)

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
                env=self._build_subprocess_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CursorCLIError(self._format_spawn_failure_message(command, exc)) from exc

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
        prompt_for_cli, _ = self._prepare_prompt_input(prompt)
        command = self._build_command(prompt_for_cli, model, stream=True)
        t_spawn = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.cursor_workspace,
                env=self._build_subprocess_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CursorCLIError(self._format_spawn_failure_message(command, exc)) from exc
        spawn_elapsed_s = time.perf_counter() - t_spawn
        logger.info(
            "cli run_chat subprocess spawned pid=%s model=%r spawn_elapsed_s=%.3f",
            process.pid,
            model,
            spawn_elapsed_s,
        )
        t_comm = time.perf_counter()
        stdout, stderr = await process.communicate()
        communicate_elapsed_s = time.perf_counter() - t_comm
        logger.info(
            "cli run_chat communicate done returncode=%s communicate_elapsed_s=%.3f total_since_spawn_s=%.3f",
            process.returncode,
            communicate_elapsed_s,
            time.perf_counter() - t_spawn,
        )
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

    # Arg keys that are noisy nested structures, skipped when compact mode is on.
    _HINT_SKIP_KEYS: frozenset[str] = frozenset(
        {
            "toolCallId",
            "parsingResult",
            "simpleCommands",
            "hasInputRedirect",
            "hasOutputRedirect",
            "fileOutputThresholdBytes",
            "isBackground",
            "skipApproval",
            "timeoutBehavior",
            "hardTimeout",
            "closeStdin",
            "timeout",
        }
    )

    def _format_tool_call_hint(self, payload: dict) -> str | None:
        """Build a human-readable hint from a tool_call started event.

        When ``settings.tool_hint_compact`` is True (default), nested / noisy
        arg keys are skipped and long values are truncated to
        ``settings.tool_hint_max_value_len`` characters, keeping the hint well
        below typical downstream block-size limits.  Set
        ``TOOL_HINT_COMPACT=false`` (or config) to disable and show full args.
        """
        if payload.get("subtype") != "started":
            return None

        tool_call = payload.get("tool_call", {})

        for key, value in tool_call.items():
            tool_name = key
            args = value.get("args", {})
            break
        else:
            return None

        compact = self.settings.tool_hint_compact
        max_len = self.settings.tool_hint_max_value_len

        parts: list[str] = []
        for k, v in args.items():
            if compact and k in CursorCLIAdapter._HINT_SKIP_KEYS:
                continue
            if compact and isinstance(v, (dict, list)):
                continue
            s = str(v)
            if compact and len(s) > max_len:
                s = s[: max_len - 3] + "..."
            parts.append(f"{k}: {s}")

        args_summary = ", ".join(parts)
        if args_summary:
            return f"\n\n> 🔧 调用工具: {tool_name} ({args_summary})\n\n"
        return f"\n\n> 🔧 调用工具: {tool_name}\n\n"

    async def stream_chat(self, prompt: str, model: str) -> AsyncIterator[str]:
        """Real-time streaming with tool call hints.

        Uses delta events (with timestamp_ms) for real-time text output.
        Inserts tool call hints when the Agent invokes tools.
        Skips summary events (without timestamp_ms) to avoid duplication.

        After the NDJSON loop ends (typically right after a ``type=result`` line), the
        generator returns immediately so callers can emit ``[DONE]`` without waiting
        for subprocess reap / ``wait()``. Cleanup runs in a background task unless
        ``Settings.stream_sync_cli_reap`` is True (``CONFIG_CURSOR_STREAM_SYNC_CLI_REAP`` /
        env ``CURSOR_STREAM_SYNC_CLI_REAP``), in which case behavior matches the old
        synchronous wait + ``CursorCLIError`` on non-zero exit.

        If stdout ends without a ``result`` event (e.g. ``agent login`` / API key errors),
        cleanup is awaited with ``raise_on_error=True`` so the stream fails fast instead
        of appearing as an empty successful completion.
        """
        self._ensure_cli_available()
        t_prepare = time.perf_counter()
        prompt_for_cli, _ = self._prepare_prompt_input(prompt)
        prepare_elapsed_s = time.perf_counter() - t_prepare
        command = self._build_command(prompt_for_cli, model, stream=True)
        t_spawn = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.cursor_workspace,
                env=self._build_subprocess_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CursorCLIError(self._format_spawn_failure_message(command, exc)) from exc
        spawn_elapsed_s = time.perf_counter() - t_spawn
        t_after_popen = time.perf_counter()
        logger.info(
            "cli stream_chat subprocess spawned pid=%s model=%r spawn_elapsed_s=%.3f "
            "prepare_prompt_elapsed_s=%.4f prompt_cli_len=%d workspace=%s",
            process.pid,
            model,
            spawn_elapsed_s,
            prepare_elapsed_s,
            len(prompt_for_cli),
            self.settings.cursor_workspace,
        )

        assert process.stdout is not None
        assert process.stderr is not None

        stderr_task = asyncio.create_task(process.stderr.read())
        thinking_started = False
        ended_on_result = False
        cleanup_scheduled = False
        result_session_id: str | None = None
        result_request_id: str | None = None
        first_ndjson_logged = False
        first_thinking_text_logged = False
        first_assistant_text_logged = False
        boot_timings: dict[str, float] = {}

        try:
            async for line in self._iter_ndjson_lines(process.stdout, boot_timings=boot_timings):
                if not first_ndjson_logged:
                    first_ndjson_logged = True
                    since_spawn_s = time.perf_counter() - t_spawn
                    since_popen_s = time.perf_counter() - t_after_popen
                    fr = boot_timings.get("first_stdout_read_perf")
                    fl = boot_timings.get("first_stdout_line_perf")
                    since_popen_first_read_s = (fr - t_after_popen) if fr is not None else None
                    read_to_first_line_ms = (
                        (fl - fr) * 1000.0 if fr is not None and fl is not None else None
                    )
                    try:
                        peek = json.loads(line)
                        logger.info(
                            "cli stream_chat first ndjson since_spawn_s=%.3f since_popen_s=%.3f "
                            "since_popen_first_stdout_read_s=%s read_to_first_line_ms=%s "
                            "line_len=%d event_type=%r subtype=%r",
                            since_spawn_s,
                            since_popen_s,
                            f"{since_popen_first_read_s:.3f}"
                            if since_popen_first_read_s is not None
                            else "n/a",
                            f"{read_to_first_line_ms:.1f}"
                            if read_to_first_line_ms is not None
                            else "n/a",
                            len(line),
                            peek.get("type"),
                            peek.get("subtype"),
                        )
                    except json.JSONDecodeError:
                        logger.info(
                            "cli stream_chat first stdout line since_spawn_s=%.3f since_popen_s=%.3f "
                            "since_popen_first_stdout_read_s=%s read_to_first_line_ms=%s "
                            "line_len=%d non-json preview=%r",
                            since_spawn_s,
                            since_popen_s,
                            f"{since_popen_first_read_s:.3f}"
                            if since_popen_first_read_s is not None
                            else "n/a",
                            f"{read_to_first_line_ms:.1f}"
                            if read_to_first_line_ms is not None
                            else "n/a",
                            len(line),
                            line[:160],
                        )
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
                        if not first_thinking_text_logged:
                            first_thinking_text_logged = True
                            now = time.perf_counter()
                            fl = boot_timings.get("first_stdout_line_perf")
                            since_first_line_s = (now - fl) if fl is not None else None
                            logger.info(
                                "cli stream_chat first thinking text delta since_popen_s=%.3f "
                                "since_first_ndjson_line_s=%s len=%d",
                                now - t_after_popen,
                                f"{since_first_line_s:.3f}"
                                if since_first_line_s is not None
                                else "n/a",
                                len(text),
                            )
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
                    if not first_assistant_text_logged:
                        acc = "".join(
                            p["text"]
                            for p in content
                            if p.get("type") == "text" and p.get("text")
                        )
                        if acc:
                            first_assistant_text_logged = True
                            now = time.perf_counter()
                            fl = boot_timings.get("first_stdout_line_perf")
                            since_first_line_s = (now - fl) if fl is not None else None
                            logger.info(
                                "cli stream_chat first assistant stream text since_popen_s=%.3f "
                                "since_first_ndjson_line_s=%s chars=%d",
                                now - t_after_popen,
                                f"{since_first_line_s:.3f}"
                                if since_first_line_s is not None
                                else "n/a",
                                len(acc),
                            )
                    for part in content:
                        if part.get("type") == "text" and part.get("text"):
                            yield part["text"]

                elif event_type == "tool_call":
                    hint = self._format_tool_call_hint(payload)
                    if hint:
                        yield hint

                elif event_type == "result":
                    # Same terminal signal as ``_extract_assistant_text_from_ndjson``; some CLI
                    # builds keep the subprocess alive after this line without closing stdout.
                    result_session_id = payload.get("session_id")
                    result_request_id = payload.get("request_id")
                    logger.info(
                        "stream-json result event (run finished) session_id=%s request_id=%s",
                        result_session_id,
                        result_request_id,
                    )
                    ended_on_result = True
                    break

            if ended_on_result:
                if self.settings.stream_sync_cli_reap:
                    await _finish_cursor_cli_stream_subprocess(
                        process,
                        stderr_task,
                        ended_on_result,
                        result_session_id,
                        result_request_id,
                        raise_on_error=True,
                    )
                else:
                    cleanup_scheduled = True
                    task = asyncio.create_task(
                        _finish_cursor_cli_stream_subprocess(
                            process,
                            stderr_task,
                            ended_on_result,
                            result_session_id,
                            result_request_id,
                            raise_on_error=False,
                        ),
                        name=f"cursor-cli-stream-cleanup-pid-{process.pid}",
                    )

                    def _sink_background_cleanup(t: asyncio.Task) -> None:
                        try:
                            t.result()
                        except Exception:
                            pass

                    task.add_done_callback(_sink_background_cleanup)
            else:
                # stdout closed without ``result`` (auth errors, crashes): surface stderr
                # to the caller; otherwise ``main.event_stream`` treats EOF as success.
                await _finish_cursor_cli_stream_subprocess(
                    process,
                    stderr_task,
                    ended_on_result,
                    result_session_id,
                    result_request_id,
                    raise_on_error=True,
                )
        finally:
            if not cleanup_scheduled and not stderr_task.done():
                stderr_task.cancel()
