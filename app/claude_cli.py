from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator

from .cli_base import (
    AGENT_CLAUDE,
    CLIError,
    PromptFileMixin,
    agent_cli_label,
    build_prefix_for_executable,
    claude_cli_model_arg,
    claude_result_error_message,
    extract_claude_assistant_text,
    finish_stream_subprocess,
    format_spawn_failure_message,
    iter_ndjson_lines,
    parse_ndjson_assistant_summary,
    resolve_executable,
)
from .config import Settings
from .stream_chat_session import StreamChatSession

logger = logging.getLogger("cursor-wrapper")

ClaudeCLIError = CLIError


@dataclass(frozen=True)
class ClaudeCLIResult:
    text: str
    session_id: str | None = None
    request_id: str | None = None
    duration_ms: int | None = None


class ClaudeCLIAdapter(PromptFileMixin):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    def _resolve_executable(self) -> str | None:
        return resolve_executable(self.settings.claude_bin)

    def cli_status(self) -> dict[str, str | bool | None]:
        resolved = self._resolve_executable()
        return {
            "configured_bin": self.settings.claude_bin,
            "resolved_bin": resolved,
            "available": resolved is not None,
        }

    def _ensure_cli_available(self) -> None:
        if self._resolve_executable():
            return
        raise ClaudeCLIError(
            "Claude CLI executable was not found. "
            f"Current CLAUDE_BIN is '{self.settings.claude_bin}'. "
            "Please install Claude Code CLI and ensure `claude` works, "
            "or set CLAUDE_BIN to the full executable path."
        )

    def _build_command(self, prompt: str, model: str, *, stream: bool) -> list[str]:
        resolved = self._resolve_executable() or self.settings.claude_bin
        command = [*build_prefix_for_executable(resolved), "-p", prompt]
        command.extend(["--output-format", "stream-json" if stream else "json"])
        if stream:
            command.extend(["--verbose", "--include-partial-messages"])
        cli_model = claude_cli_model_arg(model)
        if cli_model:
            command.extend(["--model", cli_model])
        return command

    def _build_subprocess_env(self) -> dict[str, str]:
        return dict(os.environ)

    @staticmethod
    def _yield_assistant_delta(streamed: str, text: str) -> tuple[str, str]:
        if not text:
            return streamed, ""
        if text == streamed:
            return streamed, ""
        if streamed and text.startswith(streamed):
            return text, text[len(streamed) :]
        if streamed and streamed.endswith(text):
            return streamed, ""
        if not streamed:
            return text, text
        return streamed + text, text

    async def stream_chat(
        self,
        prompt: str,
        model: str,
        *,
        session: StreamChatSession | None = None,
    ) -> AsyncIterator[str]:
        self._ensure_cli_available()
        prompt_for_cli, _ = self._prepare_prompt_input(prompt)
        command = self._build_command(prompt_for_cli, model, stream=True)
        cli_model = claude_cli_model_arg(model)
        if model and model != (cli_model or ""):
            logger.info(
                "[%s] stream_chat omit --model for resolved=%r (use CLI default)",
                AGENT_CLAUDE,
                model,
            )
        logger.debug("[%s] stream_chat command=%r", AGENT_CLAUDE, command)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.cursor_workspace,
                env=self._build_subprocess_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ClaudeCLIError(
                format_spawn_failure_message(
                    "Claude",
                    self.settings.claude_bin,
                    command,
                    self.settings.cursor_workspace,
                    exc,
                )
            ) from exc
        logger.info(
            "[%s] stream_chat subprocess spawned pid=%s model=%r prompt_cli_len=%d workspace=%s",
            AGENT_CLAUDE,
            process.pid,
            cli_model or "(default)",
            len(prompt_for_cli),
            self.settings.cursor_workspace,
        )

        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        if session is not None:
            session.register(process, stderr_task, agent=AGENT_CLAUDE)

        ended_on_result = False
        cleanup_scheduled = False
        result_session_id: str | None = None
        result_request_id: str | None = None
        assistant_streamed = ""
        streamed_any = False
        last_error_hint: str | None = None

        try:
            async for line in iter_ndjson_lines(process.stdout, subprocess_pid=process.pid):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = payload.get("type")
                result_err = claude_result_error_message(payload)
                if result_err:
                    last_error_hint = result_err

                if event_type == "stream_event":
                    event = payload.get("event") or {}
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text") or ""
                            assistant_streamed, delta_text = self._yield_assistant_delta(
                                assistant_streamed, text
                            )
                            if delta_text:
                                streamed_any = True
                                yield delta_text

                elif event_type == "assistant":
                    text = extract_claude_assistant_text(payload)
                    if text:
                        last_error_hint = last_error_hint or text
                        assistant_streamed, delta_text = self._yield_assistant_delta(
                            assistant_streamed, text
                        )
                        if delta_text:
                            streamed_any = True
                            yield delta_text

                elif event_type == "result":
                    result_session_id = payload.get("session_id")
                    result_request_id = payload.get("request_id")
                    if payload.get("is_error"):
                        raise ClaudeCLIError(
                            claude_result_error_message(payload) or "Claude CLI returned an error."
                        )
                    ended_on_result = True
                    break

            if not streamed_any and last_error_hint:
                raise ClaudeCLIError(last_error_hint)

            if ended_on_result:
                if self.settings.stream_sync_cli_reap:
                    await finish_stream_subprocess(
                        process,
                        stderr_task,
                        ended_on_result,
                        result_session_id,
                        result_request_id,
                        agent_label=agent_cli_label(AGENT_CLAUDE),
                        raise_on_error=not streamed_any,
                    )
                else:
                    cleanup_scheduled = True
                    task = asyncio.create_task(
                        finish_stream_subprocess(
                            process,
                            stderr_task,
                            ended_on_result,
                            result_session_id,
                            result_request_id,
                            agent_label=agent_cli_label(AGENT_CLAUDE),
                            raise_on_error=False,
                        ),
                        name=f"claude-cli-stream-cleanup-pid-{process.pid}",
                    )

                    def _sink(t: asyncio.Task) -> None:
                        try:
                            t.result()
                        except Exception:
                            pass

                    task.add_done_callback(_sink)
            else:
                await finish_stream_subprocess(
                    process,
                    stderr_task,
                    ended_on_result,
                    result_session_id,
                    result_request_id,
                    agent_label=_AGENT_LABEL,
                    raise_on_error=True,
                )
        except asyncio.CancelledError:
            if session is not None:
                await session.terminate_agent(reason=f"{AGENT_CLAUDE}_stream_chat_cancelled")
            raise
        finally:
            if not cleanup_scheduled and not stderr_task.done():
                stderr_task.cancel()

    async def run_chat(self, prompt: str, model: str) -> ClaudeCLIResult:
        self._ensure_cli_available()
        prompt_for_cli, _ = self._prepare_prompt_input(prompt)
        command = self._build_command(prompt_for_cli, model, stream=False)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.settings.cursor_workspace,
                env=self._build_subprocess_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ClaudeCLIError(
                format_spawn_failure_message(
                    "Claude",
                    self.settings.claude_bin,
                    command,
                    self.settings.cursor_workspace,
                    exc,
                )
            ) from exc

        stdout, stderr = await process.communicate()
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            raise ClaudeCLIError(
                stderr_text or f"Claude CLI execution failed (exit {process.returncode}).",
                exit_code=process.returncode,
                stderr=stderr_text,
            )

        if not stdout_text:
            raise ClaudeCLIError("Claude CLI returned an empty response.", stderr=stderr_text)

        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            result_err = claude_result_error_message(payload)
            if result_err:
                raise ClaudeCLIError(result_err)
            if payload.get("type") == "assistant":
                text = extract_claude_assistant_text(payload)
                if text:
                    return ClaudeCLIResult(text=text)

        text, session_id, request_id, duration_ms = parse_ndjson_assistant_summary(stdout_text)
        if not text:
            raise ClaudeCLIError(
                "Claude CLI returned no assistant text. "
                "Check Claude login/API or avoid passing Cursor-only model aliases like 'auto'."
            )
        return ClaudeCLIResult(
            text=text,
            session_id=session_id,
            request_id=request_id,
            duration_ms=duration_ms,
        )
