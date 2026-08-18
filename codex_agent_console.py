# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
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
import tkinter as tk
from tkinter import messagebox, ttk


APP_NAME = "Codex Agent Console"
APP_VERSION = "1.0.1"
AUTO_REFRESH_MS = 1000
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

        root.title(f"{APP_NAME} {APP_VERSION}")
        root.geometry("1040x720")
        root.minsize(900, 620)
        root.configure(bg="#111418")
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
        style.configure("Title.TLabel", foreground="#ffffff", font=("Segoe UI Semibold", 19))
        style.configure("Section.TLabel", background="#171b20", foreground="#ffffff", font=("Segoe UI Semibold", 12))
        style.configure("Value.TLabel", background="#171b20", foreground="#8ecbff", font=("Segoe UI Semibold", 15))
        style.configure("TButton", padding=(12, 8), font=("Segoe UI Semibold", 9))
        style.configure("Primary.TButton", background="#2f81f7", foreground="#ffffff")
        style.map("Primary.TButton", background=[("active", "#4793ff")])
        style.configure("TRadiobutton", background="#171b20", foreground="#e7e9ec")
        style.configure("TCombobox", fieldbackground="#22272e", foreground="#ffffff")
        style.configure("TSpinbox", fieldbackground="#22272e", foreground="#ffffff")

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
