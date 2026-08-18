# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import messagebox, ttk


APP_NAME = "Codex Agent Console"
APP_VERSION = "1.1.0"
AUTO_REFRESH_MS = 1000
DIAGNOSTIC_CLEANUP_GRACE_SECONDS = 5.0
DIAGNOSTIC_PROMPT = (
    "This is an API connectivity diagnostic. Do not use tools. "
    "Reply with exactly API_OK and nothing else."
)
MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.2",
)
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL_EFFORTS = {
    "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-terra": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
    "gpt-5.5": ("low", "medium", "high", "xhigh"),
    "gpt-5.2": ("low", "medium", "high", "xhigh"),
}
DUAL_MODE_POLICY_START = "[Codex Agent Console: dual-model orchestration]"
DUAL_MODE_POLICY_END = "[/Codex Agent Console: dual-model orchestration]"
_DUAL_MODE_POLICY_RE = re.compile(
    rf"{re.escape(DUAL_MODE_POLICY_START)}.*?{re.escape(DUAL_MODE_POLICY_END)}",
    re.DOTALL,
)


def _policy_value(value: str) -> str:
    return value.replace("`", "'").replace("\r", " ").replace("\n", " ").strip()


def build_dual_mode_policy(subagent_model: str, subagent_effort: str) -> str:
    model = _policy_value(subagent_model)
    effort = _policy_value(subagent_effort)
    return f"""{DUAL_MODE_POLICY_START}
For non-trivial implementation work, use the primary agent for planning, decomposition, integration, and final review. Delegate bounded implementation and execution tasks to subagents through the available multi-agent tools. When spawning an execution subagent, explicitly set model to `{model}` and reasoning_effort to `{effort}`; do not rely on inherited defaults. Only use a different subagent model or effort when the user explicitly requests it or the configured combination is unavailable. Keep trivial work in the primary agent, and do not delegate when coordination overhead outweighs the benefit.
{DUAL_MODE_POLICY_END}"""


def has_dual_mode_policy(instructions: object) -> bool:
    return isinstance(instructions, str) and bool(
        _DUAL_MODE_POLICY_RE.search(instructions)
    )


def merge_dual_mode_policy(
    instructions: object,
    enabled: bool,
    subagent_model: str = "",
    subagent_effort: str = "",
) -> str:
    existing = instructions if isinstance(instructions, str) else ""
    preserved = _DUAL_MODE_POLICY_RE.sub("", existing).strip()
    if not enabled:
        return preserved
    policy = build_dual_mode_policy(subagent_model, subagent_effort)
    return f"{preserved}\n\n{policy}".strip()


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"Unsupported TOML value: {type(value).__name__}")


_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


def update_toml_values(text: str, updates: dict[tuple[str, str], object]) -> str:
    """Update selected TOML keys while preserving unrelated text and comments."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    pending = dict(updates)
    section = ""
    seen_sections: set[str] = set()
    output: list[str] = []

    def flush_missing(current_section: str) -> None:
        for (target_section, key), value in list(pending.items()):
            if target_section == current_section:
                output.append(f"{key} = {_toml_value(value)}")
                del pending[(target_section, key)]

    for line in lines:
        table_match = _TABLE_RE.match(line)
        if table_match:
            flush_missing(section)
            section = table_match.group(1).strip()
            seen_sections.add(section)
            output.append(line)
            continue

        replaced = False
        for (target_section, key), value in list(pending.items()):
            if target_section != section:
                continue
            key_match = re.match(
                rf"^(\s*){re.escape(key)}\s*=.*?(\s+#.*)?$", line
            )
            if key_match:
                suffix = key_match.group(2) or ""
                output.append(
                    f"{key_match.group(1)}{key} = {_toml_value(value)}{suffix}"
                )
                del pending[(target_section, key)]
                replaced = True
                break
        if not replaced:
            output.append(line)

    flush_missing(section)

    grouped: dict[str, list[tuple[str, object]]] = {}
    for (target_section, key), value in pending.items():
        grouped.setdefault(target_section, []).append((key, value))
    for target_section, values in grouped.items():
        if output and output[-1].strip():
            output.append("")
        if target_section and target_section not in seen_sections:
            output.append(f"[{target_section}]")
        for key, value in values:
            output.append(f"{key} = {_toml_value(value)}")

    return newline.join(output).rstrip() + newline


@dataclass(frozen=True)
class AgentSettings:
    main_model: str
    main_effort: str
    subagent_model: str
    subagent_effort: str
    agents_enabled: bool
    max_threads: int


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> AgentSettings:
        if not self.path.exists():
            return AgentSettings(
                "gpt-5.6-sol", "high", "gpt-5.6-terra", "medium", False, 4
            )
        text = self.path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
        dual_mode_enabled = bool(agents.get("enabled", True)) and has_dual_mode_policy(
            data.get("developer_instructions")
        )
        return AgentSettings(
            main_model=str(data.get("model") or "gpt-5.6-sol"),
            main_effort=str(data.get("model_reasoning_effort") or "high"),
            subagent_model=str(
                agents.get("default_subagent_model") or data.get("model") or "gpt-5.6-terra"
            ),
            subagent_effort=str(
                agents.get("default_subagent_reasoning_effort") or "medium"
            ),
            agents_enabled=dual_mode_enabled,
            max_threads=max(
                1,
                min(
                    16,
                    int(
                        agents.get("max_concurrent_threads_per_session")
                        or agents.get("max_threads")
                        or 4
                    ),
                ),
            ),
        )

    def save(self, settings: AgentSettings) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        original = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        original_data: dict[str, object] = {}
        if original.strip():
            original_data = tomllib.loads(original)
        developer_instructions = merge_dual_mode_policy(
            original_data.get("developer_instructions"),
            settings.agents_enabled,
            settings.subagent_model,
            settings.subagent_effort,
        )
        updated = update_toml_values(
            original,
            {
                ("", "model"): settings.main_model,
                ("", "model_reasoning_effort"): settings.main_effort,
                ("", "developer_instructions"): developer_instructions,
                ("agents", "enabled"): settings.agents_enabled,
                ("agents", "default_subagent_model"): settings.subagent_model,
                (
                    "agents",
                    "default_subagent_reasoning_effort",
                ): settings.subagent_effort,
                (
                    "agents",
                    "max_concurrent_threads_per_session",
                ): settings.max_threads,
            },
        )
        tomllib.loads(updated)

        backup = self.path.with_name(self.path.name + ".codex-agent-console.bak")
        if self.path.exists():
            shutil.copy2(self.path, backup)
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.remove(temp_name)
        return backup


@dataclass
class Usage:
    sessions: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    models: dict[str, int] = field(default_factory=dict)
    latest_model: str = ""
    latest_started_at: datetime | None = None

    def add(
        self,
        token_data: dict[str, object],
        model: str = "",
        reasoning_effort: str = "",
        started_at: datetime | None = None,
    ) -> None:
        self.sessions += 1
        if model:
            label = f"{model}/{reasoning_effort}" if reasoning_effort else model
            self.models[label] = self.models.get(label, 0) + 1
            if started_at is not None and (
                self.latest_started_at is None or started_at >= self.latest_started_at
            ):
                self.latest_model = label
                self.latest_started_at = started_at
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            try:
                setattr(self, name, getattr(self, name) + int(token_data.get(name) or 0))
            except (TypeError, ValueError):
                pass

    @property
    def cache_hit_rate(self) -> float:
        return (
            self.cached_input_tokens * 100.0 / self.input_tokens
            if self.input_tokens
            else 0.0
        )


@dataclass(frozen=True)
class DiagnosticResult:
    role: str
    model: str
    effort: str
    attempt: int
    success: bool
    status: str
    first_response_seconds: float | None
    total_seconds: float
    process_seconds: float = 0.0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    tokens_per_second: float = 0.0
    error: str = ""

    @property
    def cleanup_seconds(self) -> float:
        return max(0.0, self.process_seconds - self.total_seconds)


@dataclass(frozen=True)
class DiagnosticSummary:
    total: int = 0
    succeeded: int = 0
    success_rate: float = 0.0
    average_first_response: float | None = None
    average_total_time: float | None = None
    average_tokens_per_second: float | None = None


def summarize_diagnostics(results: list[DiagnosticResult]) -> DiagnosticSummary:
    if not results:
        return DiagnosticSummary()
    succeeded = sum(result.success for result in results)
    first_response_values = [
        result.first_response_seconds
        for result in results
        if result.first_response_seconds is not None
    ]
    total_values = [result.total_seconds for result in results]
    speed_values = [
        result.tokens_per_second
        for result in results
        if result.success and result.output_tokens > 0
    ]
    return DiagnosticSummary(
        total=len(results),
        succeeded=succeeded,
        success_rate=succeeded * 100.0 / len(results),
        average_first_response=(
            sum(first_response_values) / len(first_response_values)
            if first_response_values
            else None
        ),
        average_total_time=sum(total_values) / len(total_values),
        average_tokens_per_second=(
            sum(speed_values) / len(speed_values) if speed_values else None
        ),
    )


def classify_diagnostic_error(message: str) -> str:
    normalized = message.casefold()
    if re.search(r"(?:^|\D)401(?:\D|$)", normalized) or any(
        term in normalized
        for term in ("unauthorized", "authentication", "invalid api key")
    ):
        return "401 认证失败"
    if re.search(r"(?:^|\D)404(?:\D|$)", normalized) or (
        "model" in normalized and "not found" in normalized
    ):
        return "404 模型不可用"
    if re.search(r"(?:^|\D)429(?:\D|$)", normalized) or any(
        term in normalized for term in ("rate limit", "too many requests", "quota")
    ):
        return "429 请求受限"
    if "timeout" in normalized or "timed out" in normalized:
        return "请求超时"
    if "用户停止" in message or "cancelled" in normalized or "canceled" in normalized:
        return "已停止"
    if any(
        term in normalized
        for term in (
            "connection",
            "connect error",
            "network",
            "dns",
            "socket",
            "tls",
            "ssl",
            "certificate",
        )
    ):
        return "网络错误"
    return "CLI 失败"


def _safe_token_count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_token_usage(value: object) -> dict[str, int]:
    usage = value if isinstance(value, dict) else {}
    input_details = usage.get("input_tokens_details")
    cached_fallback = (
        input_details.get("cached_tokens")
        if isinstance(input_details, dict)
        else 0
    )
    return {
        "input_tokens": _safe_token_count(usage.get("input_tokens")),
        "cached_input_tokens": _safe_token_count(
            usage.get("cached_input_tokens") or cached_fallback
        ),
        "output_tokens": _safe_token_count(usage.get("output_tokens")),
    }


def _event_error_message(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("message", "detail", "error", "code"):
            message = _event_error_message(value.get(key))
            if message:
                return message
    return ""


def parse_codex_json_event(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    event_type = value.get("type")
    parsed: dict[str, object] = {}
    if event_type == "thread.started" and isinstance(value.get("thread_id"), str):
        parsed["thread_id"] = value["thread_id"]

    item = value.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    if event_type in ("item.started", "item.completed") and item_type in (
        "agent_message",
        "message",
    ):
        parsed["first_response"] = True

    if event_type == "turn.completed":
        parsed["completed"] = True
        parsed["usage"] = normalize_token_usage(value.get("usage"))
    if event_type in ("turn.failed", "error"):
        parsed["failed"] = True
        message = _event_error_message(value.get("error"))
        if not message:
            message = _event_error_message(value.get("message"))
        if message:
            parsed["error"] = message
    return parsed


def find_codex_executable(
    local_appdata: Path | None = None,
    path_lookup: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    local_root = local_appdata
    if local_root is None:
        configured = os.environ.get("LOCALAPPDATA", "").strip()
        local_root = Path(configured) if configured else None

    candidates: list[Path] = []
    if local_root is not None:
        bundled_root = local_root / "OpenAI" / "Codex" / "bin"
        try:
            bundled = sorted(
                bundled_root.glob("*/codex.exe"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            bundled = []
        candidates.extend(bundled)
        candidates.append(local_root / "Microsoft" / "WindowsApps" / "codex.exe")

    for name in ("codex.exe", "codex"):
        located = path_lookup(name)
        if located:
            candidates.append(Path(located))

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and (sys.platform != "win32" or candidate.suffix.lower() == ".exe"):
            return candidate
    return None


def locate_diagnostic_session(
    sessions_dir: Path, thread_id: str
) -> Path | None:
    if not thread_id or not sessions_dir.exists():
        return None
    try:
        matches = list(sessions_dir.rglob(f"*{thread_id}*.jsonl"))
    except OSError:
        return None
    if not matches:
        return None
    try:
        return max(matches, key=lambda path: path.stat().st_mtime_ns)
    except OSError:
        return matches[0]


class SessionTokenTail:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.latest = normalize_token_usage({})

    def read_latest(self) -> dict[str, int]:
        try:
            if self.path.stat().st_size < self.offset:
                self.offset = 0
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                while True:
                    line_offset = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        self.offset = line_offset
                        break
                    self.offset = handle.tell()
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    payload = item.get("payload") if isinstance(item, dict) else None
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    total = (
                        info.get("total_token_usage")
                        if isinstance(info, dict)
                        else None
                    )
                    if isinstance(total, dict):
                        self.latest = normalize_token_usage(total)
        except OSError:
            pass
        return dict(self.latest)


class CodexDiagnosticsRunner:
    def __init__(self, executable: Path, home: Path, workdir: Path):
        self.executable = executable
        self.home = home
        self.workdir = workdir
        self._cancel = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        self._cancel.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(
        self,
        cases: list[tuple[str, str, str]],
        attempts: int,
        timeout_seconds: float,
        on_progress: Callable[[dict[str, object]], None],
        on_result: Callable[[DiagnosticResult], None],
    ) -> None:
        try:
            for attempt in range(1, attempts + 1):
                for role, model, effort in cases:
                    if self._cancel.is_set():
                        return
                    try:
                        result = self._run_one(
                            role,
                            model,
                            effort,
                            attempt,
                            timeout_seconds,
                            on_progress,
                        )
                    except Exception as exc:
                        message = f"诊断器异常：{exc}"
                        result = DiagnosticResult(
                            role=role,
                            model=model,
                            effort=effort,
                            attempt=attempt,
                            success=False,
                            status="CLI 失败",
                            first_response_seconds=None,
                            total_seconds=0.0,
                            error=message,
                        )
                    on_result(result)
        finally:
            on_progress(
                {"phase": "finished", "cancelled": self._cancel.is_set()}
            )

    @staticmethod
    def _read_stdout(
        stream: object, output: queue.Queue[str | None]
    ) -> None:
        try:
            for line in stream:  # type: ignore[union-attr]
                output.put(line)
        finally:
            output.put(None)

    @staticmethod
    def _read_stderr(stream: object, output: list[str]) -> None:
        try:
            for line in stream:  # type: ignore[union-attr]
                output.append(line)
        except (OSError, ValueError):
            pass

    def _run_one(
        self,
        role: str,
        model: str,
        effort: str,
        attempt: int,
        timeout_seconds: float,
        on_progress: Callable[[dict[str, object]], None],
    ) -> DiagnosticResult:
        command = [
            str(self.executable),
            "-a",
            "never",
            "-s",
            "read-only",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-c",
            "agents.enabled=false",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "-C",
            str(self.workdir),
            DIAGNOSTIC_PROMPT,
        ]
        creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            message = str(exc)
            return DiagnosticResult(
                role,
                model,
                effort,
                attempt,
                False,
                classify_diagnostic_error(message),
                None,
                time.monotonic() - started,
                error=message,
            )

        with self._process_lock:
            self._process = process
        stdout_lines: queue.Queue[str | None] = queue.Queue()
        stderr_lines: list[str] = []
        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, stdout_lines),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr, stderr_lines),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        thread_id = ""
        session_tail: SessionTokenTail | None = None
        session_usage = normalize_token_usage({})
        final_usage = normalize_token_usage({})
        first_response: float | None = None
        completed = False
        response_completed_seconds: float | None = None
        event_error = ""
        stdout_done = False
        process_exit_seconds: float | None = None
        forced_error = ""
        termination_started: float | None = None
        speed_samples: list[tuple[float, int]] = []
        live_speed = 0.0
        cleanup_forced = False
        next_session_poll = started
        next_progress = started

        def consume_stdout() -> None:
            nonlocal thread_id, first_response, completed, event_error, stdout_done
            nonlocal response_completed_seconds
            while True:
                try:
                    line = stdout_lines.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    stdout_done = True
                    continue
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                parsed = parse_codex_json_event(event)
                if isinstance(parsed.get("thread_id"), str):
                    thread_id = str(parsed["thread_id"])
                if parsed.get("first_response") and first_response is None:
                    first_response = time.monotonic() - started
                if parsed.get("completed"):
                    completed = True
                    if response_completed_seconds is None:
                        response_completed_seconds = time.monotonic() - started
                    usage = parsed.get("usage")
                    if isinstance(usage, dict):
                        final_usage.update(usage)
                if parsed.get("failed"):
                    event_error = str(parsed.get("error") or "Codex 请求失败")

        while True:
            consume_stdout()
            now = time.monotonic()
            elapsed = now - started
            return_code = process.poll()
            if return_code is not None and process_exit_seconds is None:
                process_exit_seconds = elapsed

            if not completed and not forced_error and self._cancel.is_set():
                forced_error = "用户停止了诊断请求"
            elif not completed and not forced_error and elapsed >= timeout_seconds:
                forced_error = f"请求超时（超过 {timeout_seconds:g} 秒）"

            cleanup_elapsed = (
                elapsed - response_completed_seconds
                if response_completed_seconds is not None
                else 0.0
            )
            if (
                completed
                and return_code is None
                and termination_started is None
                and (
                    self._cancel.is_set()
                    or cleanup_elapsed >= DIAGNOSTIC_CLEANUP_GRACE_SECONDS
                )
            ):
                cleanup_forced = True
                termination_started = now
                try:
                    process.terminate()
                except OSError:
                    pass

            if forced_error and return_code is None and termination_started is None:
                termination_started = now
                try:
                    process.terminate()
                except OSError:
                    pass
            if (
                termination_started is not None
                and return_code is None
                and now - termination_started >= 2.0
            ):
                try:
                    process.kill()
                except OSError:
                    pass

            if now >= next_session_poll:
                if session_tail is None and thread_id:
                    session_path = locate_diagnostic_session(
                        self.home / "sessions", thread_id
                    )
                    if session_path is not None:
                        session_tail = SessionTokenTail(session_path)
                if session_tail is not None:
                    session_usage = session_tail.read_latest()
                    output_tokens = session_usage["output_tokens"]
                    if output_tokens > 0 and first_response is None:
                        first_response = elapsed
                    speed_samples.append((now, output_tokens))
                    speed_samples = [
                        sample for sample in speed_samples if now - sample[0] <= 3.0
                    ]
                    if len(speed_samples) >= 2:
                        duration = speed_samples[-1][0] - speed_samples[0][0]
                        token_delta = speed_samples[-1][1] - speed_samples[0][1]
                        live_speed = max(0.0, token_delta / duration) if duration else 0.0
                    if output_tokens > 0 and live_speed == 0.0:
                        live_speed = output_tokens / max(0.05, elapsed)
                next_session_poll = now + 0.25

            if now >= next_progress:
                on_progress(
                    {
                        "phase": "cleanup" if completed else "running",
                        "role": role,
                        "model": model,
                        "effort": effort,
                        "attempt": attempt,
                        "elapsed": elapsed,
                        "output_tokens": session_usage["output_tokens"],
                        "tokens_per_second": live_speed,
                        "response_seconds": response_completed_seconds,
                        "cleanup_seconds": cleanup_elapsed,
                    }
                )
                next_progress = now + 0.25

            if return_code is not None and (
                stdout_done
                or (
                    process_exit_seconds is not None
                    and elapsed - process_exit_seconds >= 0.35
                )
            ):
                break
            time.sleep(0.05)

        consume_stdout()
        stdout_thread.join(timeout=0.2)
        stderr_thread.join(timeout=0.2)
        loop_seconds = time.monotonic() - started
        process_seconds = process_exit_seconds or loop_seconds
        total_seconds = response_completed_seconds or process_seconds
        with self._process_lock:
            self._process = None

        usage = final_usage if any(final_usage.values()) else session_usage
        output_tokens = usage["output_tokens"]
        if output_tokens > 0:
            average_speed = output_tokens / max(0.05, total_seconds)
        else:
            average_speed = 0.0

        return_code = process.returncode if process.returncode is not None else -1
        success = (
            completed
            and (return_code == 0 or cleanup_forced)
            and not forced_error
            and not event_error
        )
        if success:
            cleanup_slow = (
                cleanup_forced
                or process_seconds - total_seconds
                >= DIAGNOSTIC_CLEANUP_GRACE_SECONDS
            )
            status = "成功/CLI退出慢" if cleanup_slow else "成功"
            error = ""
        else:
            stderr = "".join(stderr_lines).strip()
            error = forced_error or event_error or stderr or f"Codex CLI 退出码 {return_code}"
            status = classify_diagnostic_error(error)
        return DiagnosticResult(
            role=role,
            model=model,
            effort=effort,
            attempt=attempt,
            success=success,
            status=status,
            first_response_seconds=first_response,
            total_seconds=total_seconds,
            process_seconds=process_seconds,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            output_tokens=output_tokens,
            tokens_per_second=average_speed,
            error=error,
        )


@dataclass(frozen=True)
class ParsedSession:
    started_at: datetime
    is_subagent: bool
    tokens: dict[str, object]
    model: str = ""
    reasoning_effort: str = ""


class SessionStatsReader:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self._cache: dict[Path, tuple[int, int, ParsedSession | None]] = {}

    def _parse_file(self, path: Path) -> ParsedSession | None:
        started_at: datetime | None = None
        is_subagent = False
        latest_tokens: dict[str, object] | None = None
        model = ""
        reasoning_effort = ""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    payload = item.get("payload") if isinstance(item, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    if item.get("type") == "session_meta":
                        source = payload.get("source")
                        is_subagent = payload.get("thread_source") == "subagent" or (
                            isinstance(source, dict) and "subagent" in source
                        )
                        stamp = payload.get("timestamp") or item.get("timestamp")
                        if isinstance(stamp, str):
                            try:
                                started_at = datetime.fromisoformat(
                                    stamp.replace("Z", "+00:00")
                                )
                            except ValueError:
                                pass
                    if isinstance(payload.get("model"), str):
                        model = payload["model"]
                    collaboration = payload.get("collaboration_mode")
                    settings = (
                        collaboration.get("settings")
                        if isinstance(collaboration, dict)
                        else None
                    )
                    if isinstance(settings, dict) and isinstance(
                        settings.get("reasoning_effort"), str
                    ):
                        reasoning_effort = settings["reasoning_effort"]
                    if payload.get("type") == "token_count":
                        info = payload.get("info")
                        total = info.get("total_token_usage") if isinstance(info, dict) else None
                        if isinstance(total, dict):
                            latest_tokens = total
        except OSError:
            return None
        if latest_tokens is None:
            return None
        if started_at is None:
            started_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        elif started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        return ParsedSession(
            started_at,
            is_subagent,
            latest_tokens,
            model,
            reasoning_effort,
        )

    def _get_session(self, path: Path) -> ParsedSession | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        cached = self._cache.get(path)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        parsed = self._parse_file(path)
        self._cache[path] = (stat.st_mtime_ns, stat.st_size, parsed)
        return parsed

    def aggregate(self, days: int | None = 7) -> tuple[Usage, Usage]:
        main = Usage()
        subagent = Usage()
        if not self.sessions_dir.exists():
            return main, subagent
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
            if days is not None
            else None
        )
        paths = list(self.sessions_dir.rglob("*.jsonl"))
        live_paths = set(paths)
        for stale in set(self._cache) - live_paths:
            del self._cache[stale]
        for path in paths:
            parsed = self._get_session(path)
            if parsed is None or (cutoff and parsed.started_at < cutoff):
                continue
            (subagent if parsed.is_subagent else main).add(
                parsed.tokens,
                parsed.model,
                parsed.reasoning_effort,
                parsed.started_at,
            )
        return main, subagent


def _fmt_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


class CodexAgentConsole:
    def __init__(self, root: tk.Tk, home: Path | None = None):
        self.root = root
        self.home = home or codex_home()
        self.config_store = ConfigStore(self.home / "config.toml")
        self.stats_reader = SessionStatsReader(self.home / "sessions")
        self._refresh_running = False
        self._diagnostic_window: tk.Toplevel | None = None
        self._diagnostic_runner: CodexDiagnosticsRunner | None = None
        self._diagnostic_generation = 0
        self._diagnostic_results: list[DiagnosticResult] = []
        self._diagnostic_errors: dict[str, str] = {}

        root.title(f"{APP_NAME} {APP_VERSION}")
        root.geometry("1040x720")
        root.minsize(900, 620)
        root.configure(bg="#111418")
        root.protocol("WM_DELETE_WINDOW", self._close_app)
        self._configure_style()
        self._build_ui()
        self._load_settings()
        self.refresh_stats()
        self.root.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background="#171b20", foreground="#e7e9ec")
        style.configure("TFrame", background="#111418")
        style.configure("Panel.TFrame", background="#171b20")
        style.configure("TLabel", background="#111418", foreground="#e7e9ec", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground="#9aa3ad", font=("Segoe UI", 9))
        style.configure(
            "PanelMuted.TLabel",
            background="#171b20",
            foreground="#9aa3ad",
            font=("Segoe UI", 9),
        )
        style.configure("Title.TLabel", foreground="#ffffff", font=("Segoe UI Semibold", 19))
        style.configure("Section.TLabel", background="#171b20", foreground="#ffffff", font=("Segoe UI Semibold", 12))
        style.configure("Value.TLabel", background="#171b20", foreground="#8ecbff", font=("Segoe UI Semibold", 15))
        style.configure("TButton", padding=(12, 8), font=("Segoe UI Semibold", 9))
        style.configure("Primary.TButton", background="#2f81f7", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#4793ff")])
        style.configure("TRadiobutton", background="#171b20", foreground="#e7e9ec")
        style.configure("TCombobox", fieldbackground="#22272e", foreground="#ffffff")
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", "#22272e"),
                ("disabled", "#1c2128"),
            ],
            foreground=[
                ("readonly", "#ffffff"),
                ("disabled", "#7d8590"),
            ],
            selectbackground=[("readonly", "#22272e")],
            selectforeground=[("readonly", "#ffffff")],
        )
        style.configure("TSpinbox", fieldbackground="#22272e", foreground="#ffffff")
        style.configure(
            "Treeview",
            background="#171b20",
            fieldbackground="#171b20",
            foreground="#e7e9ec",
            rowheight=28,
        )
        style.configure(
            "Treeview.Heading",
            background="#22272e",
            foreground="#ffffff",
            font=("Segoe UI Semibold", 9),
        )
        style.map("Treeview", background=[("selected", "#2f81f7")])

    def _build_ui(self) -> None:
        shell = ttk.Frame(self.root, padding=20)
        shell.pack(fill="both", expand=True)

        header = ttk.Frame(shell)
        header.pack(fill="x")
        ttk.Label(
            header,
            text=f"{APP_NAME} {APP_VERSION}",
            style="Title.TLabel",
        ).pack(side="left")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(header, textvariable=self.status_var, style="Muted.TLabel").pack(side="right")

        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True, pady=(18, 0))
        body.columnconfigure(0, weight=5)
        body.columnconfigure(1, weight=6)
        body.rowconfigure(0, weight=1)

        settings_panel = ttk.Frame(body, style="Panel.TFrame", padding=18)
        settings_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        stats_panel = ttk.Frame(body, style="Panel.TFrame", padding=18)
        stats_panel.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        self._build_settings(settings_panel)
        self._build_stats(stats_panel)

    def _build_settings(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="运行设置", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 14)
        )

        self.mode_var = tk.StringVar(value="multi")
        mode_frame = ttk.Frame(parent, style="Panel.TFrame")
        mode_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        ttk.Radiobutton(
            mode_frame,
            text="双模型协调（主规划 + 子执行）",
            variable=self.mode_var,
            value="multi",
        ).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(
            mode_frame,
            text="单代理（普通）",
            variable=self.mode_var,
            value="single",
        ).pack(side="left")

        self.main_model_var = tk.StringVar()
        self.main_effort_var = tk.StringVar()
        self.sub_model_var = tk.StringVar()
        self.sub_effort_var = tk.StringVar()
        self.max_threads_var = tk.IntVar(value=4)

        row = 2
        row = self._add_combo_row(parent, row, "主模型", self.main_model_var, MODELS)
        row = self._add_combo_row(parent, row, "主模型思考", self.main_effort_var, EFFORTS)
        ttk.Separator(parent).grid(row=row, column=0, columnspan=2, sticky="ew", pady=13)
        row += 1
        row = self._add_combo_row(parent, row, "子代理模型", self.sub_model_var, MODELS)
        row = self._add_combo_row(parent, row, "子代理思考", self.sub_effort_var, EFFORTS)

        ttk.Label(parent, text="并发子代理").grid(row=row, column=0, sticky="w", pady=7)
        ttk.Spinbox(
            parent,
            from_=1,
            to=16,
            textvariable=self.max_threads_var,
            width=8,
        ).grid(row=row, column=1, sticky="ew", pady=7)
        row += 1

        button_row = ttk.Frame(parent, style="Panel.TFrame")
        button_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        ttk.Button(
            button_row,
            text="保存设置",
            command=self.save_settings,
        ).pack(side="left")
        ttk.Button(
            button_row,
            text="启用双模型",
            style="Primary.TButton",
            command=self.enable_dual_mode,
        ).pack(side="left", padx=8)
        ttk.Button(
            button_row,
            text="恢复普通模式",
            command=self.restore_normal_mode,
        ).pack(side="left")

        ttk.Button(parent, text="打开配置文件", command=self.open_config).grid(
            row=row + 1, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

        ttk.Label(
            parent,
            text="双模型会注入协调策略；设置仅对新任务生效。",
            style="Muted.TLabel",
        ).grid(row=row + 2, column=0, columnspan=2, sticky="w", pady=(14, 0))

    def _add_combo_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...],
    ) -> int:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=7)
        ttk.Combobox(parent, textvariable=variable, values=values, state="normal").grid(
            row=row, column=1, sticky="ew", pady=7
        )
        return row + 1

    def _build_stats(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        top = ttk.Frame(parent, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Token 统计", style="Section.TLabel").pack(side="left")
        self.period_var = tk.StringVar(value="7天")
        period = ttk.Combobox(
            top,
            textvariable=self.period_var,
            values=("今天", "7天", "30天", "全部"),
            state="readonly",
            width=7,
        )
        period.pack(side="right")
        period.bind("<<ComboboxSelected>>", lambda _event: self.refresh_stats())

        self.main_stats_vars = self._build_usage_block(parent, 1, "主模型")
        ttk.Separator(parent).grid(row=2, column=0, sticky="ew", pady=16)
        self.sub_stats_vars = self._build_usage_block(parent, 3, "子代理")

        bottom = ttk.Frame(parent, style="Panel.TFrame")
        bottom.grid(row=4, column=0, sticky="ew", pady=(20, 0))
        ttk.Button(bottom, text="刷新", command=self.refresh_stats).pack(side="left")
        ttk.Button(bottom, text="打开会话目录", command=self.open_sessions).pack(
            side="left", padx=8
        )
        ttk.Button(
            bottom,
            text="API 诊断",
            style="Primary.TButton",
            command=self.open_diagnostics,
        ).pack(side="left")
        self.updated_var = tk.StringVar(value="尚未刷新")
        ttk.Label(bottom, textvariable=self.updated_var, style="Muted.TLabel").pack(
            side="right"
        )

    def _build_usage_block(
        self, parent: ttk.Frame, row: int, title: str
    ) -> dict[str, tk.StringVar]:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=row, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        vars_ = {
            "total": tk.StringVar(value="0"),
            "input": tk.StringVar(value="输入 0"),
            "cached": tk.StringVar(value="缓存 0"),
            "output": tk.StringVar(value="输出 0"),
            "reasoning": tk.StringVar(value="推理 0"),
            "hit": tk.StringVar(value="命中率 0%"),
            "sessions": tk.StringVar(value="0 个会话"),
            "actual": tk.StringVar(value="实际模型：尚无记录"),
        }
        ttk.Label(frame, text=title, style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(frame, textvariable=vars_["sessions"], style="Muted.TLabel").grid(
            row=0, column=1, sticky="e"
        )
        ttk.Label(frame, textvariable=vars_["total"], style="Value.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 8)
        )
        ttk.Label(frame, textvariable=vars_["input"], style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=2
        )
        ttk.Label(frame, textvariable=vars_["cached"], style="Muted.TLabel").grid(
            row=2, column=1, sticky="w", pady=2
        )
        ttk.Label(frame, textvariable=vars_["output"], style="Muted.TLabel").grid(
            row=3, column=0, sticky="w", pady=2
        )
        ttk.Label(frame, textvariable=vars_["reasoning"], style="Muted.TLabel").grid(
            row=3, column=1, sticky="w", pady=2
        )
        ttk.Label(frame, textvariable=vars_["hit"], style="Muted.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(5, 0)
        )
        ttk.Label(
            frame,
            textvariable=vars_["actual"],
            style="Muted.TLabel",
            wraplength=430,
            justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(5, 0))
        return vars_

    def _load_settings(self) -> None:
        try:
            settings = self.config_store.load()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法读取配置：{exc}")
            return
        self.main_model_var.set(settings.main_model)
        self.main_effort_var.set(settings.main_effort)
        self.sub_model_var.set(settings.subagent_model)
        self.sub_effort_var.set(settings.subagent_effort)
        self.max_threads_var.set(settings.max_threads)
        self.mode_var.set("multi" if settings.agents_enabled else "single")

    def _current_settings(self) -> AgentSettings:
        main_model = self.main_model_var.get().strip()
        sub_model = self.sub_model_var.get().strip()
        main_effort = self.main_effort_var.get().strip()
        sub_effort = self.sub_effort_var.get().strip()
        if not main_model or not sub_model:
            raise ValueError("主模型和子代理模型不能为空")
        if main_effort not in EFFORTS or sub_effort not in EFFORTS:
            raise ValueError("思考程度必须从可用级别中选择")
        for model, effort, role in (
            (main_model, main_effort, "主模型"),
            (sub_model, sub_effort, "子代理"),
        ):
            allowed = MODEL_EFFORTS.get(model)
            if allowed and effort not in allowed:
                raise ValueError(f"{role} {model} 不支持 {effort} 思考程度")
        threads = int(self.max_threads_var.get())
        if not 1 <= threads <= 16:
            raise ValueError("并发子代理必须在 1 到 16 之间")
        return AgentSettings(
            main_model,
            main_effort,
            sub_model,
            sub_effort,
            self.mode_var.get() == "multi",
            threads,
        )

    def save_settings(self) -> bool:
        try:
            settings = self._current_settings()
            self.config_store.save(settings)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"保存失败：{exc}")
            return False
        mode = "双模型协调策略已写入" if settings.agents_enabled else "普通模式已恢复"
        self.status_var.set(f"已保存 · {mode} · 新任务生效")
        return True

    def enable_dual_mode(self) -> None:
        self.mode_var.set("multi")
        if self.save_settings():
            messagebox.showinfo(
                APP_NAME,
                "双模型协调策略已写入。\n\n请在 Codex 中新建任务；当前对话不会热切换。",
            )

    def restore_normal_mode(self) -> None:
        self.mode_var.set("single")
        if self.save_settings():
            messagebox.showinfo(
                APP_NAME,
                "已恢复普通单代理模式。\n\n请在 Codex 中新建任务；当前对话不会热切换。",
            )

    def _period_days(self) -> int | None:
        return {"今天": 1, "7天": 7, "30天": 30, "全部": None}.get(
            self.period_var.get(), 7
        )

    def refresh_stats(self) -> None:
        if self._refresh_running:
            return
        self._refresh_running = True
        days = self._period_days()

        def worker() -> None:
            try:
                result = self.stats_reader.aggregate(days)
                self.root.after(0, lambda: self._apply_stats(*result))
            except Exception as exc:
                self.root.after(0, lambda: self.status_var.set(f"统计失败：{exc}"))
                self.root.after(0, self._end_refresh)

        threading.Thread(target=worker, daemon=True).start()

    def _end_refresh(self) -> None:
        self._refresh_running = False

    def _apply_stats(self, main: Usage, subagent: Usage) -> None:
        self._set_usage_vars(self.main_stats_vars, main)
        self._set_usage_vars(self.sub_stats_vars, subagent)
        self.updated_var.set(time.strftime("更新于 %H:%M:%S"))
        self._end_refresh()

    @staticmethod
    def _set_usage_vars(vars_: dict[str, tk.StringVar], usage: Usage) -> None:
        vars_["total"].set(f"{_fmt_tokens(usage.total_tokens)} tokens")
        vars_["input"].set(f"输入 {_fmt_tokens(usage.input_tokens)}")
        vars_["cached"].set(f"缓存 {_fmt_tokens(usage.cached_input_tokens)}")
        vars_["output"].set(f"输出 {_fmt_tokens(usage.output_tokens)}")
        vars_["reasoning"].set(
            f"推理 {_fmt_tokens(usage.reasoning_output_tokens)}"
        )
        vars_["hit"].set(
            "缓存命中率 "
            f"{usage.cache_hit_rate:.1f}% "
            f"({usage.cached_input_tokens:,}/{usage.input_tokens:,})"
        )
        vars_["sessions"].set(f"{usage.sessions} 个会话")
        model_items = sorted(usage.models.items(), key=lambda item: (-item[1], item[0]))
        if not model_items:
            vars_["actual"].set("实际模型：尚无记录")
        else:
            parts = [f"{name} ×{count}" for name, count in model_items[:3]]
            if len(model_items) > 3:
                parts.append(f"另 {len(model_items) - 3} 种")
            latest = f"最新 {usage.latest_model}；" if usage.latest_model else ""
            vars_["actual"].set("实际模型：" + latest + "分布 " + "，".join(parts))

    def open_diagnostics(self) -> None:
        if (
            self._diagnostic_window is not None
            and self._diagnostic_window.winfo_exists()
        ):
            self._diagnostic_window.deiconify()
            self._diagnostic_window.lift()
            self._diagnostic_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self._diagnostic_window = window
        window.title(f"API 诊断 · {APP_NAME} {APP_VERSION}")
        window.geometry("1240x700")
        window.minsize(940, 560)
        window.configure(bg="#111418")
        window.protocol("WM_DELETE_WINDOW", self._close_diagnostics)

        shell = ttk.Frame(window, padding=16)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(3, weight=1)

        controls = ttk.Frame(shell)
        controls.grid(row=0, column=0, sticky="ew")
        self.diag_target_var = tk.StringVar(value="主模型和子代理")
        self.diag_attempts_var = tk.StringVar(value="1")
        self.diag_timeout_var = tk.IntVar(value=120)

        ttk.Label(controls, text="测试目标").pack(side="left")
        self.diag_target_combo = ttk.Combobox(
            controls,
            textvariable=self.diag_target_var,
            values=("主模型", "子代理", "主模型和子代理"),
            state="readonly",
            width=16,
        )
        self.diag_target_combo.pack(side="left", padx=(8, 18))
        ttk.Label(controls, text="次数").pack(side="left")
        self.diag_attempts_combo = ttk.Combobox(
            controls,
            textvariable=self.diag_attempts_var,
            values=("1", "3", "5"),
            state="readonly",
            width=5,
        )
        self.diag_attempts_combo.pack(side="left", padx=(8, 18))
        ttk.Label(controls, text="超时（秒）").pack(side="left")
        self.diag_timeout_spin = ttk.Spinbox(
            controls,
            from_=10,
            to=600,
            textvariable=self.diag_timeout_var,
            width=7,
        )
        self.diag_timeout_spin.pack(side="left", padx=(8, 18))
        self.diag_start_button = ttk.Button(
            controls,
            text="开始测试",
            style="Primary.TButton",
            command=self._start_diagnostics,
        )
        self.diag_start_button.pack(side="left")
        self.diag_stop_button = ttk.Button(
            controls,
            text="停止",
            command=self._stop_diagnostics,
            state="disabled",
        )
        self.diag_stop_button.pack(side="left", padx=8)

        self.diag_cli_var = tk.StringVar(value="正在检测 Codex CLI…")
        ttk.Label(
            controls,
            textvariable=self.diag_cli_var,
            style="Muted.TLabel",
        ).pack(side="right")

        summary = ttk.Frame(shell, style="Panel.TFrame", padding=(14, 12))
        summary.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        for column in range(4):
            summary.columnconfigure(column, weight=1)
        self.diag_summary_vars = {
            "success": tk.StringVar(value="0/0"),
            "first": tk.StringVar(value="--"),
            "total": tk.StringVar(value="--"),
            "speed": tk.StringVar(value="--"),
        }
        for column, (label, key) in enumerate(
            (
                ("连通成功率", "success"),
                ("平均首响应", "first"),
                ("平均总耗时", "total"),
                ("平均输出速度", "speed"),
            )
        ):
            block = ttk.Frame(summary, style="Panel.TFrame")
            block.grid(row=0, column=column, sticky="ew", padx=8)
            ttk.Label(block, text=label, style="PanelMuted.TLabel").pack(anchor="w")
            ttk.Label(
                block,
                textvariable=self.diag_summary_vars[key],
                style="Value.TLabel",
            ).pack(anchor="w", pady=(4, 0))

        self.diag_live_var = tk.StringVar(value="尚未开始测试")
        ttk.Label(shell, textvariable=self.diag_live_var).grid(
            row=2, column=0, sticky="ew", pady=(0, 10)
        )

        table_frame = ttk.Frame(shell)
        table_frame.grid(row=3, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = (
            "role",
            "model",
            "effort",
            "attempt",
            "status",
            "first",
            "total",
            "cleanup",
            "input",
            "cached",
            "output",
            "speed",
            "error",
        )
        self.diag_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "role": "目标",
            "model": "模型",
            "effort": "思考",
            "attempt": "轮次",
            "status": "状态",
            "first": "首响应",
            "total": "API 总耗时",
            "cleanup": "CLI 收尾",
            "input": "输入",
            "cached": "缓存",
            "output": "输出",
            "speed": "Tokens/s",
            "error": "错误详情",
        }
        widths = {
            "role": 72,
            "model": 130,
            "effort": 64,
            "attempt": 54,
            "status": 112,
            "first": 78,
            "total": 92,
            "cleanup": 82,
            "input": 76,
            "cached": 76,
            "output": 70,
            "speed": 84,
            "error": 280,
        }
        for name in columns:
            self.diag_tree.heading(name, text=headings[name])
            self.diag_tree.column(
                name,
                width=widths[name],
                minwidth=50,
                stretch=name == "error",
                anchor="w" if name in ("model", "error") else "center",
            )
        self.diag_tree.tag_configure("success", foreground="#7ee787")
        self.diag_tree.tag_configure("failure", foreground="#ff9b9b")
        self.diag_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.diag_tree.yview
        )
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(
            table_frame, orient="horizontal", command=self.diag_tree.xview
        )
        horizontal.grid(row=1, column=0, sticky="ew")
        self.diag_tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        self.diag_tree.bind("<<TreeviewSelect>>", self._show_diagnostic_error)

        footer = ttk.Frame(shell)
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        self.diag_error_var = tk.StringVar(value="错误详情：无")
        ttk.Label(
            footer,
            textvariable=self.diag_error_var,
            style="Muted.TLabel",
            wraplength=980,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            footer,
            text="诊断会发起真实请求并产生 Token。",
            style="Muted.TLabel",
        ).grid(row=0, column=1, sticky="e", padx=(16, 0))

        executable = find_codex_executable()
        if executable is None:
            self.diag_cli_var.set("未找到 Codex CLI")
            self.diag_start_button.configure(state="disabled")
        else:
            self.diag_cli_var.set(f"CLI：{executable.name}")

    def _diagnostic_window_exists(self) -> bool:
        return bool(
            self._diagnostic_window is not None
            and self._diagnostic_window.winfo_exists()
        )

    def _start_diagnostics(self) -> None:
        executable = find_codex_executable()
        if executable is None:
            messagebox.showerror(APP_NAME, "未找到可执行的 Codex CLI。")
            return
        try:
            settings = self._current_settings()
            attempts = int(self.diag_attempts_var.get())
            timeout_seconds = int(self.diag_timeout_var.get())
            if attempts not in (1, 3, 5):
                raise ValueError("测试次数只能是 1、3 或 5")
            if not 10 <= timeout_seconds <= 600:
                raise ValueError("超时必须在 10 到 600 秒之间")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"无法开始诊断：{exc}")
            return

        target = self.diag_target_var.get()
        cases: list[tuple[str, str, str]] = []
        if target in ("主模型", "主模型和子代理"):
            cases.append(("主模型", settings.main_model, settings.main_effort))
        if target in ("子代理", "主模型和子代理"):
            cases.append(("子代理", settings.subagent_model, settings.subagent_effort))
        if not cases:
            messagebox.showerror(APP_NAME, "请选择测试目标。")
            return

        self._diagnostic_generation += 1
        generation = self._diagnostic_generation
        self._diagnostic_results = []
        self._diagnostic_errors = {}
        for item in self.diag_tree.get_children():
            self.diag_tree.delete(item)
        self.diag_error_var.set("错误详情：无")
        self._update_diagnostic_summary()
        self._set_diagnostic_running(True)
        self.diag_cli_var.set(f"CLI：{executable}")

        runner = CodexDiagnosticsRunner(executable, self.home, Path.home())
        self._diagnostic_runner = runner

        def progress(payload: dict[str, object]) -> None:
            self.root.after(
                0,
                lambda value=payload: self._apply_diagnostic_progress(
                    value, generation
                ),
            )

        def result(value: DiagnosticResult) -> None:
            self.root.after(
                0,
                lambda item=value: self._apply_diagnostic_result(item, generation),
            )

        threading.Thread(
            target=runner.run,
            args=(cases, attempts, float(timeout_seconds), progress, result),
            daemon=True,
        ).start()

    def _stop_diagnostics(self) -> None:
        if self._diagnostic_runner is None:
            return
        self.diag_live_var.set("正在停止当前诊断请求…")
        self.diag_stop_button.configure(state="disabled")
        self._diagnostic_runner.cancel()

    def _set_diagnostic_running(self, running: bool) -> None:
        if not self._diagnostic_window_exists():
            return
        self.diag_target_combo.configure(state="disabled" if running else "readonly")
        self.diag_attempts_combo.configure(state="disabled" if running else "readonly")
        self.diag_timeout_spin.configure(state="disabled" if running else "normal")
        self.diag_start_button.configure(state="disabled" if running else "normal")
        self.diag_stop_button.configure(state="normal" if running else "disabled")

    def _apply_diagnostic_progress(
        self, payload: dict[str, object], generation: int
    ) -> None:
        if generation != self._diagnostic_generation or not self._diagnostic_window_exists():
            return
        if payload.get("phase") == "running":
            self.diag_live_var.set(
                f"{payload.get('role')} · {payload.get('model')}/{payload.get('effort')} · "
                f"第 {payload.get('attempt')} 轮 · {float(payload.get('elapsed') or 0):.1f}s · "
                f"输出 {int(payload.get('output_tokens') or 0):,} · "
                f"实时 {float(payload.get('tokens_per_second') or 0):.1f} Tokens/s"
            )
            return
        if payload.get("phase") == "cleanup":
            self.diag_live_var.set(
                f"{payload.get('role')} · API 已在 "
                f"{float(payload.get('response_seconds') or 0):.2f}s 完成 · "
                f"CLI 正在收尾 {float(payload.get('cleanup_seconds') or 0):.1f}s"
            )
            return
        if payload.get("phase") == "finished":
            cancelled = bool(payload.get("cancelled"))
            self.diag_live_var.set(
                "测试已停止" if cancelled else "测试完成"
            )
            self._diagnostic_runner = None
            self._set_diagnostic_running(False)
            self.refresh_stats()

    def _apply_diagnostic_result(
        self, result: DiagnosticResult, generation: int
    ) -> None:
        if generation != self._diagnostic_generation or not self._diagnostic_window_exists():
            return
        self._diagnostic_results.append(result)
        compact_error = " ".join(result.error.split())
        if len(compact_error) > 180:
            compact_error = compact_error[:177] + "..."
        item_id = self.diag_tree.insert(
            "",
            "end",
            values=(
                result.role,
                result.model,
                result.effort,
                result.attempt,
                result.status,
                (
                    f"{result.first_response_seconds:.2f}s"
                    if result.first_response_seconds is not None
                    else "--"
                ),
                f"{result.total_seconds:.2f}s",
                f"+{result.cleanup_seconds:.2f}s",
                f"{result.input_tokens:,}",
                f"{result.cached_input_tokens:,}",
                f"{result.output_tokens:,}",
                f"{result.tokens_per_second:.1f}",
                compact_error,
            ),
            tags=("success" if result.success else "failure",),
        )
        self._diagnostic_errors[item_id] = result.error
        self.diag_tree.see(item_id)
        self._update_diagnostic_summary()

    def _update_diagnostic_summary(self) -> None:
        summary = summarize_diagnostics(self._diagnostic_results)
        self.diag_summary_vars["success"].set(
            f"{summary.succeeded}/{summary.total} ({summary.success_rate:.1f}%)"
            if summary.total
            else "0/0"
        )
        self.diag_summary_vars["first"].set(
            f"{summary.average_first_response:.2f} s"
            if summary.average_first_response is not None
            else "--"
        )
        self.diag_summary_vars["total"].set(
            f"{summary.average_total_time:.2f} s"
            if summary.average_total_time is not None
            else "--"
        )
        self.diag_summary_vars["speed"].set(
            f"{summary.average_tokens_per_second:.1f} Tokens/s"
            if summary.average_tokens_per_second is not None
            else "--"
        )

    def _show_diagnostic_error(self, _event: object = None) -> None:
        selected = self.diag_tree.selection()
        if not selected:
            self.diag_error_var.set("错误详情：无")
            return
        error = self._diagnostic_errors.get(selected[0], "")
        self.diag_error_var.set(f"错误详情：{error or '无'}")

    def _close_diagnostics(self) -> None:
        self._diagnostic_generation += 1
        if self._diagnostic_runner is not None:
            self._diagnostic_runner.cancel()
            self._diagnostic_runner = None
        if self._diagnostic_window is not None:
            self._diagnostic_window.destroy()
        self._diagnostic_window = None

    def _close_app(self) -> None:
        self._diagnostic_generation += 1
        if self._diagnostic_runner is not None:
            self._diagnostic_runner.cancel()
            self._diagnostic_runner = None
        self.root.destroy()

    def _auto_refresh(self) -> None:
        self.refresh_stats()
        self.root.after(AUTO_REFRESH_MS, self._auto_refresh)

    @staticmethod
    def _open_path(path: Path) -> None:
        path = path.resolve()
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def open_config(self) -> None:
        if not self.config_store.path.exists():
            self.config_store.path.parent.mkdir(parents=True, exist_ok=True)
            self.config_store.path.touch()
        self._open_path(self.config_store.path)

    def open_sessions(self) -> None:
        sessions = self.home / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        self._open_path(sessions)


def main() -> None:
    root = tk.Tk()
    CodexAgentConsole(root)
    root.mainloop()


if __name__ == "__main__":
    main()
