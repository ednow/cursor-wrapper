from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator

from .config import Settings

logger = logging.getLogger("cursor-wrapper")

AGENT_CURSOR = "cursor"
AGENT_CLAUDE = "claude"


def agent_cli_label(agent: str) -> str:
    """日志/错误文案用的 CLI 展示名。"""
    if agent.strip().lower() == AGENT_CLAUDE:
        return "Claude CLI"
    return "Cursor CLI"


class CLIError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


async def iter_ndjson_lines(
    stream: asyncio.StreamReader,
    *,
    boot_timings: dict[str, float] | None = None,
    subprocess_pid: int | None = None,
    chunk_size: int = 64 * 1024,
) -> AsyncIterator[str]:
    buffer = bytearray()
    first_read_done = False
    idle_log_s = _stdout_read_idle_log_seconds()
    while True:
        if idle_log_s > 0:
            try:
                chunk = await asyncio.wait_for(stream.read(chunk_size), timeout=idle_log_s)
            except asyncio.TimeoutError:
                logger.info(
                    "cli stdout read still waiting idle_s>=%.0f pid=%s pending_buffer=%d",
                    idle_log_s,
                    subprocess_pid if subprocess_pid is not None else "?",
                    len(buffer),
                )
                continue
        else:
            chunk = await stream.read(chunk_size)
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


def _stdout_read_idle_log_seconds() -> float:
    raw = os.getenv("CURSOR_CLI_STDOUT_READ_IDLE_LOG_SECONDS", "30")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 30.0


def resolve_executable(bin_name: str) -> str | None:
    if os.path.sep in bin_name or (os.path.altsep and os.path.altsep in bin_name):
        path = Path(bin_name)
        return str(path) if path.exists() else None
    return shutil.which(bin_name)


def build_prefix_for_executable(executable: str) -> list[str]:
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


def format_spawn_failure_message(
    agent_label: str,
    bin_name: str,
    command: list[str],
    workspace: str,
    exc: OSError,
) -> str:
    parts = [
        f"{agent_label} CLI executable could not be started.",
        f"Current bin is '{bin_name}'.",
        f"command={command!r}",
        f"cwd='{workspace}'",
        f"errno={getattr(exc, 'errno', None)!r}",
        f"winerror={getattr(exc, 'winerror', None)!r}",
        f"filename={getattr(exc, 'filename', None)!r}",
        f"filename2={getattr(exc, 'filename2', None)!r}",
        f"strerror={getattr(exc, 'strerror', None)!r}",
    ]
    return " ".join(parts)


class PromptFileMixin:
    """长 prompt 外置到 workspace/prompt/。"""

    _PROMPT_FILE_THRESHOLD = 4000

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _prompt_dir(self) -> Path:
        d = Path(self.settings.cursor_workspace) / "prompt"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _prepare_prompt_input(self, prompt: str) -> tuple[str, str | None]:
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
        logger.info("long prompt externalized (%d chars) -> %s", len(prompt), file_path)
        return bridged_prompt, file_path

    def persist_full_prompt_for_bridge(self, content: str) -> str:
        prompt_dir = self._prompt_dir()
        fd, file_path = tempfile.mkstemp(
            prefix="cursor-wrapper-prompt-",
            suffix=".txt",
            dir=str(prompt_dir),
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(content)
        logger.info(
            "cli last-user bridge: full context externalized (%d chars) -> %s",
            len(content),
            file_path,
        )
        return file_path


# Cursor 模型别名；传给 Claude CLI 会触发「模型不存在」类 API 错误。
CLAUDE_SKIP_MODEL_VALUES = frozenset({"auto", "cursor-agent", ""})


def claude_cli_model_arg(resolved_model: str) -> str | None:
    """Claude CLI 可接受的 ``--model``；``None`` 表示省略，使用本机 Claude 默认模型。"""
    model = (resolved_model or "").strip()
    if not model or model in CLAUDE_SKIP_MODEL_VALUES:
        return None
    return model


async def _reap_after_stream_json_result_event(
    process: asyncio.subprocess.Process, ended_on_result: bool, *, agent_label: str
) -> None:
    if not ended_on_result or process.returncode is not None:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning(
            "%s still running after stream-json result event (pid=%s); terminating",
            agent_label,
            process.pid,
        )
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def finish_stream_subprocess(
    process: asyncio.subprocess.Process,
    stderr_task: asyncio.Task,
    ended_on_result: bool,
    session_id: str | None,
    request_id: str | None,
    *,
    agent_label: str,
    raise_on_error: bool,
) -> None:
    """NDJSON 读完后收尾：reap、stderr、wait；非零退出时记录或抛出 ``CLIError``。"""
    try:
        await _reap_after_stream_json_result_event(process, ended_on_result, agent_label=agent_label)
        stderr_bytes = await stderr_task
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        return_code = await process.wait()
        if return_code != 0:
            detail = stderr_text or f"{agent_label} exited with status {return_code} (stderr empty)."
            if raise_on_error:
                raise CLIError(detail, exit_code=return_code, stderr=stderr_text)
            logger.error(
                "%s exited with status %s after stream (session_id=%s request_id=%s): %s",
                agent_label,
                return_code,
                session_id,
                request_id,
                stderr_text or "(empty stderr)",
            )
    except asyncio.CancelledError:
        raise
    except CLIError:
        raise
    except Exception:
        logger.exception(
            "%s stream subprocess cleanup failed session_id=%s request_id=%s",
            agent_label,
            session_id,
            request_id,
        )


def extract_claude_assistant_text(payload: dict) -> str:
    """从 Claude ``type=assistant`` 行提取文本（含 API 错误信息）。"""
    message = payload.get("message") or {}
    parts = message.get("content") or []
    return "".join(
        p.get("text", "")
        for p in parts
        if p.get("type") == "text" and p.get("text")
    )


def claude_result_error_message(payload: dict) -> str | None:
    if payload.get("type") != "result":
        return None
    if not payload.get("is_error"):
        return None
    return str(payload.get("result") or payload.get("subtype") or "Claude CLI returned an error.")


def parse_ndjson_assistant_summary(raw: str) -> tuple[str, str | None, str | None, int | None]:
    """解析 NDJSON 中最后一段 assistant 摘要与 result 元数据。"""
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

        if event_type == "assistant" and "timestamp_ms" not in payload:
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
