# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from codex_agent_console import (
    AgentSettings,
    ANTHROPIC_API_VERSION,
    ConfigStore,
    DesktopBackendProcess,
    DesktopBackendReloader,
    CustomApiBenchmarkRunner,
    CustomApiEndpoint,
    CustomApiResult,
    CustomApiStore,
    CUSTOM_API_CACHE_PROMPT,
    CUSTOM_API_PROMPT,
    DiagnosticResult,
    DUAL_MODE_POLICY_START,
    MANAGED_EXECUTOR_AGENT_NAME,
    SessionTokenTail,
    SessionStatsReader,
    build_dual_mode_policy,
    custom_api_base_url,
    custom_api_endpoint_url,
    choose_custom_api_winners,
    classify_diagnostic_error,
    fetch_custom_api_models,
    find_codex_executable,
    has_dual_mode_policy,
    merge_dual_mode_policy,
    parse_codex_json_event,
    protect_secret,
    summarize_diagnostics,
    summarize_custom_apis,
    unprotect_secret,
    update_toml_values,
    validate_custom_api_endpoint,
)


class ConfigStoreTests(unittest.TestCase):
    def test_updates_only_selected_keys_and_preserves_other_sections(self):
        source = (
            'model_provider = "custom"\n'
            'model = "old-main" # keep comment\n'
            'model_reasoning_effort = "low"\n\n'
            '[features]\n'
            'web_search = true\n\n'
            '[agents]\n'
            'enabled = true\n'
            'custom_key = "keep"\n'
        )
        updated = update_toml_values(
            source,
            {
                ("", "model"): "gpt-5.6-sol",
                ("", "model_reasoning_effort"): "high",
                ("agents", "enabled"): False,
                ("agents", "default_subagent_model"): "gpt-5.6-terra",
                ("agents", "default_subagent_reasoning_effort"): "medium",
            },
        )
        self.assertIn('model_provider = "custom"', updated)
        self.assertIn('model = "gpt-5.6-sol" # keep comment', updated)
        self.assertIn("web_search = true", updated)
        self.assertIn('custom_key = "keep"', updated)
        self.assertIn("enabled = false", updated)
        self.assertIn('default_subagent_model = "gpt-5.6-terra"', updated)

    def test_save_is_valid_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text('model = "old"\n[features]\nx = true\n', encoding="utf-8")
            store = ConfigStore(config)
            backup = store.save(
                AgentSettings(
                    "gpt-5.6-sol",
                    "high",
                    "gpt-5.6-luna",
                    "medium",
                    True,
                    3,
                )
            )
            loaded = store.load()
            self.assertEqual(loaded.main_model, "gpt-5.6-sol")
            self.assertEqual(loaded.subagent_model, "gpt-5.6-luna")
            self.assertEqual(loaded.max_threads, 3)
            self.assertTrue(backup.exists())
            self.assertIn('model = "old"', backup.read_text(encoding="utf-8"))

    def test_saves_managed_executor_with_selected_model_and_effort(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            store = ConfigStore(config)
            enabled = AgentSettings(
                "gpt-5.6-sol", "high", "gpt-5.6-luna", "xhigh", True, 4
            )
            store.save(enabled)
            executor = tomllib.loads(
                store.managed_executor_path.read_text(encoding="utf-8")
            )
            self.assertEqual(executor["name"], MANAGED_EXECUTOR_AGENT_NAME)
            self.assertEqual(executor["model"], "gpt-5.6-luna")
            self.assertEqual(executor["model_reasoning_effort"], "xhigh")

            store.save(
                AgentSettings(
                    "gpt-5.6-sol", "high", "gpt-5.6-luna", "xhigh", False, 4
                )
            )
            self.assertFalse(store.managed_executor_path.exists())

    def test_dual_mode_policy_is_added_once_and_removed_without_losing_user_text(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text(
                'developer_instructions = "Keep my existing instruction."\n',
                encoding="utf-8",
            )
            agents = Path(temp) / "AGENTS.md"
            agents.write_text("Keep my global instruction.\n", encoding="utf-8")
            store = ConfigStore(config)
            dual = AgentSettings(
                "gpt-5.6-sol", "high", "gpt-5.6-terra", "medium", True, 4
            )
            store.save(dual)
            store.save(dual)
            enabled_data = tomllib.loads(config.read_text(encoding="utf-8"))
            enabled_instructions = agents.read_text(encoding="utf-8")
            self.assertEqual(enabled_instructions.count(DUAL_MODE_POLICY_START), 1)
            self.assertIn("Keep my global instruction.", enabled_instructions)
            self.assertIn("`model` `gpt-5.6-terra`", enabled_instructions)
            self.assertIn("`reasoning_effort` `medium`", enabled_instructions)
            self.assertTrue(has_dual_mode_policy(enabled_instructions))
            self.assertTrue(store.load().agents_enabled)
            self.assertEqual(
                enabled_data["developer_instructions"], "Keep my existing instruction."
            )

            store.save(
                AgentSettings(
                    "gpt-5.6-sol",
                    "high",
                    "gpt-5.6-terra",
                    "medium",
                    False,
                    4,
                )
            )
            disabled_data = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(
                disabled_data["developer_instructions"],
                "Keep my existing instruction.",
            )
            self.assertEqual(agents.read_text(encoding="utf-8"), "Keep my global instruction.")
            self.assertFalse(disabled_data["agents"]["enabled"])
            self.assertFalse(store.load().agents_enabled)

    def test_prefers_nonempty_global_agents_override(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            agents = Path(temp) / "AGENTS.md"
            override = Path(temp) / "AGENTS.override.md"
            agents.write_text("Base global guidance.", encoding="utf-8")
            override.write_text("Temporary global guidance.", encoding="utf-8")
            store = ConfigStore(config)
            settings = AgentSettings(
                "gpt-5.6-sol", "high", "gpt-5.6-luna", "xhigh", True, 4
            )

            store.save(settings)

            self.assertFalse(has_dual_mode_policy(agents.read_text(encoding="utf-8")))
            self.assertTrue(has_dual_mode_policy(override.read_text(encoding="utf-8")))
            self.assertEqual(store.active_global_instruction_path, override)

    def test_nonempty_override_masks_policy_in_global_agents_file(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            store = ConfigStore(config)
            settings = AgentSettings(
                "gpt-5.6-sol", "high", "gpt-5.6-luna", "xhigh", True, 4
            )
            store.save(settings)
            store.global_agents_override_path.write_text(
                "Temporary guidance without the console policy.", encoding="utf-8"
            )

            self.assertFalse(store.load().agents_enabled)

    def test_load_accepts_legacy_policy_before_first_migration_save(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            legacy = merge_dual_mode_policy(
                "Legacy user instruction.", True, "gpt-5.6-luna", "xhigh"
            )
            config.write_text(
                'developer_instructions = ' + json.dumps(legacy) + '\n[agents]\nenabled = true\n',
                encoding="utf-8",
            )
            self.assertTrue(ConfigStore(config).load().agents_enabled)

    def test_policy_merge_handles_empty_and_unmanaged_instructions(self):
        enabled = merge_dual_mode_policy(None, True, "gpt-5.6-terra", "medium")
        self.assertTrue(has_dual_mode_policy(enabled))
        self.assertEqual(merge_dual_mode_policy(enabled, False), "")

    def test_dual_mode_policy_routes_work_to_a_separate_teacher_guided_executor(self):
        policy = build_dual_mode_policy("gpt-5.6-terra", "low")
        self.assertIn(
            "The primary model is the teacher and accountable owner",
            policy,
        )
        self.assertIn("exactly one `codex_agent_console_executor`", policy)
        self.assertIn(MANAGED_EXECUTOR_AGENT_NAME, policy)
        self.assertIn("student owns inspection, tool use, implementation, validation", policy)
        self.assertIn(
            '`fork_turns` `"none"`',
            policy,
        )
        self.assertIn(
            "Never use `fork_turns` `\"all\"`",
            policy,
        )
        self.assertIn("followup_task", policy)
        self.assertIn("must actively complete the remaining work", policy)
        self.assertIn("Never end with only an intention to act later", policy)
        self.assertIn("`model` `gpt-5.6-terra`", policy)
        self.assertIn("`reasoning_effort` `low`", policy)
        self.assertNotIn("Act as a thin dispatcher", policy)


class DesktopBackendReloaderTests(unittest.TestCase):
    def test_selects_only_desktop_owned_app_servers(self):
        desktop_path = (
            "C:\\Program Files\\WindowsApps\\"
            "OpenAI.Codex_26.814.5167.0_x64__2p2nqsd0c76g0\\app"
        )
        desktop = DesktopBackendProcess(
            10, 1, "ChatGPT.exe", f"{desktop_path}\\ChatGPT.exe", "ChatGPT.exe"
        )
        backend = DesktopBackendProcess(
            11,
            10,
            "codex.exe",
            f"{desktop_path}\\resources\\codex.exe",
            'codex.exe -c features.code_mode_host=true app-server',
        )
        standalone_cli = DesktopBackendProcess(
            12,
            1,
            "codex.exe",
            "C:\\Users\\me\\AppData\\Local\\OpenAI\\Codex\\bin\\codex.exe",
            "codex.exe app-server",
        )
        wrong_parent = DesktopBackendProcess(
            13,
            99,
            "codex.exe",
            f"{desktop_path}\\resources\\codex.exe",
            "codex.exe app-server",
        )

        selected = DesktopBackendReloader.select_desktop_backends(
            [desktop, backend, standalone_cli, wrong_parent]
        )

        self.assertEqual(selected, [backend])

    def test_rejects_desktop_codex_without_app_server_command(self):
        desktop_path = (
            "C:\\Program Files\\WindowsApps\\"
            "OpenAI.Codex_26.814.5167.0_x64__2p2nqsd0c76g0\\app"
        )
        desktop = DesktopBackendProcess(
            1, 0, "ChatGPT.exe", f"{desktop_path}\\ChatGPT.exe", "ChatGPT.exe"
        )
        utility = DesktopBackendProcess(
            2,
            1,
            "codex.exe",
            f"{desktop_path}\\resources\\codex.exe",
            "codex.exe exec status",
        )

        self.assertEqual(
            DesktopBackendReloader.select_desktop_backends([desktop, utility]), []
        )


class SessionStatsTests(unittest.TestCase):
    @staticmethod
    def _write_session(
        path: Path,
        thread_source: str,
        totals: list[dict],
        source: dict | None = None,
        started_at: datetime | None = None,
    ) -> None:
        now = (started_at or datetime.now(timezone.utc)).isoformat()
        metadata = {
            "timestamp": now,
            "thread_source": thread_source,
            "model": "gpt-5.6-terra" if thread_source == "subagent" else "gpt-5.6-sol",
            "collaboration_mode": {
                "settings": {
                    "reasoning_effort": "medium" if thread_source == "subagent" else "high"
                }
            },
        }
        if source is not None:
            metadata["source"] = source
        records = [
            {
                "timestamp": now,
                "type": "session_meta",
                "payload": metadata,
            }
        ]
        for total in totals:
            records.append(
                {
                    "timestamp": now,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": total},
                    },
                }
            )
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_aggregates_main_and_subagent_using_latest_cumulative_value(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp)
            self._write_session(
                sessions / "main.jsonl",
                "user",
                [
                    {"input_tokens": 10, "cached_input_tokens": 2, "total_tokens": 12},
                    {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 120,
                    },
                ],
            )
            self._write_session(
                sessions / "sub.jsonl",
                "subagent",
                [
                    {
                        "input_tokens": 50,
                        "cached_input_tokens": 25,
                        "output_tokens": 10,
                        "total_tokens": 60,
                    }
                ],
            )
            self._write_session(
                sessions / "guardian.jsonl",
                "subagent",
                [{"input_tokens": 999, "total_tokens": 999}],
                {"subagent": {"other": "guardian"}},
            )
            main, sub = SessionStatsReader(sessions).aggregate(None)
            self.assertEqual(main.sessions, 1)
            self.assertEqual(main.input_tokens, 100)
            self.assertEqual(main.cached_input_tokens, 40)
            self.assertEqual(main.cache_hit_rate, 40.0)
            self.assertEqual(sub.sessions, 1)
            self.assertEqual(sub.total_tokens, 60)
            self.assertEqual(sub.cache_hit_rate, 50.0)
            self.assertEqual(sub.models, {"gpt-5.6-terra/medium": 1})
            self.assertEqual(sub.latest_model, "gpt-5.6-terra/medium")

    def test_preserves_execution_subagent_identity_from_forked_history(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp)
            self._write_session(
                sessions / "main.jsonl",
                "user",
                [{"input_tokens": 100, "total_tokens": 100}],
            )
            now = datetime.now(timezone.utc)
            child_started = now.isoformat()
            parent_started = (now - timedelta(seconds=5)).isoformat()
            records = [
                {
                    "timestamp": child_started,
                    "type": "session_meta",
                    "payload": {
                        "timestamp": child_started,
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "agent_role": "codex_agent_console_executor"
                                }
                            }
                        },
                    },
                },
                {
                    "timestamp": parent_started,
                    "type": "session_meta",
                    "payload": {
                        "timestamp": parent_started,
                        "thread_source": "user",
                    },
                },
                {
                    "timestamp": parent_started,
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-terra", "effort": "xhigh"},
                },
                {
                    "timestamp": child_started,
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": "gpt-5.5",
                            "reasoning_effort": "xhigh",
                        },
                    },
                },
                {
                    "timestamp": child_started,
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.5", "effort": "xhigh"},
                },
                {
                    "timestamp": child_started,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 60}},
                    },
                },
            ]
            (sessions / "execution.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            main, sub = SessionStatsReader(sessions).aggregate(None)

            self.assertEqual(main.sessions, 1)
            self.assertEqual(main.latest_model, "gpt-5.6-sol/high")
            self.assertEqual(sub.sessions, 1)
            self.assertEqual(sub.total_tokens, 60)
            self.assertEqual(sub.models, {"gpt-5.5/xhigh": 1})
            self.assertEqual(sub.latest_model, "gpt-5.5/xhigh")
            self.assertEqual(
                SessionStatsReader(sessions)._parse_file(sessions / "execution.jsonl").started_at,
                datetime.fromisoformat(child_started),
            )

    def test_excludes_parent_token_history_from_forked_execution_subagent(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp)
            now = datetime.now(timezone.utc)
            child_started = now.isoformat()
            parent_started = (now - timedelta(seconds=5)).isoformat()

            def token_record(total: dict[str, int]) -> dict:
                return {
                    "timestamp": child_started,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": total},
                    },
                }

            records = [
                {
                    "timestamp": child_started,
                    "type": "session_meta",
                    "payload": {
                        "id": "execution-child",
                        "forked_from_id": "main-parent",
                        "timestamp": child_started,
                        "thread_source": "subagent",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "agent_role": "codex_agent_console_executor"
                                }
                            }
                        },
                    },
                },
                {
                    "timestamp": parent_started,
                    "type": "session_meta",
                    "payload": {
                        "id": "main-parent",
                        "timestamp": parent_started,
                        "thread_source": "user",
                    },
                },
                {
                    "timestamp": parent_started,
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.6-terra", "effort": "max"},
                },
                token_record(
                    {
                        "input_tokens": 90,
                        "cached_input_tokens": 50,
                        "cache_write_input_tokens": 2,
                        "output_tokens": 10,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 100,
                    }
                ),
                token_record(
                    {
                        "input_tokens": 130,
                        "cached_input_tokens": 80,
                        "cache_write_input_tokens": 3,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 9,
                        "total_tokens": 150,
                    }
                ),
                {
                    "timestamp": child_started,
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": "gpt-5.5",
                            "reasoning_effort": "xhigh",
                        },
                    },
                },
                {
                    "timestamp": child_started,
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                },
                {
                    "timestamp": child_started,
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.5", "effort": "xhigh"},
                },
                token_record(
                    {
                        "input_tokens": 160,
                        "cached_input_tokens": 100,
                        "cache_write_input_tokens": 4,
                        "output_tokens": 20,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 180,
                    }
                ),
                {
                    "timestamp": child_started,
                    "type": "event_msg",
                    "payload": {"type": "task_complete"},
                },
                {
                    "timestamp": child_started,
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                },
                {
                    "timestamp": child_started,
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.5", "effort": "xhigh"},
                },
                token_record(
                    {
                        "input_tokens": 220,
                        "cached_input_tokens": 140,
                        "cache_write_input_tokens": 7,
                        "output_tokens": 40,
                        "reasoning_output_tokens": 20,
                        "total_tokens": 260,
                    }
                ),
            ]
            execution_path = sessions / "execution.jsonl"
            execution_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            self._write_session(
                sessions / "direct-subagent.jsonl",
                "subagent",
                [
                    {
                        "input_tokens": 50,
                        "cached_input_tokens": 25,
                        "output_tokens": 10,
                        "total_tokens": 60,
                    }
                ],
            )
            self._write_session(
                sessions / "guardian.jsonl",
                "subagent",
                [{"input_tokens": 999, "total_tokens": 999}],
                {"subagent": {"other": "guardian"}},
            )

            parsed = SessionStatsReader(sessions)._parse_file(execution_path)
            self.assertIsNotNone(parsed)
            self.assertEqual(
                parsed.tokens,
                {
                    "input_tokens": 90,
                    "cached_input_tokens": 60,
                    "cache_write_input_tokens": 4,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 11,
                    "total_tokens": 110,
                },
            )

            _main, subagent = SessionStatsReader(sessions).aggregate(None)
            self.assertEqual(subagent.sessions, 2)
            self.assertEqual(subagent.input_tokens, 140)
            self.assertEqual(subagent.cached_input_tokens, 85)
            self.assertEqual(subagent.total_tokens, 170)

    def test_keeps_clean_forked_execution_usage_without_parent_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp)
            now = datetime.now(timezone.utc).isoformat()
            records = [
                {
                    "timestamp": now,
                    "type": "session_meta",
                    "payload": {
                        "id": "execution-child",
                        "forked_from_id": "main-parent",
                        "timestamp": now,
                        "thread_source": "subagent",
                    },
                },
                {
                    "timestamp": now,
                    "type": "event_msg",
                    "payload": {
                        "type": "thread_settings_applied",
                        "thread_settings": {
                            "model": "gpt-5.5",
                            "reasoning_effort": "xhigh",
                        },
                    },
                },
                {
                    "timestamp": now,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 20}},
                    },
                },
                {
                    "timestamp": now,
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.5", "effort": "xhigh"},
                },
                {
                    "timestamp": now,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"total_tokens": 60}},
                    },
                },
            ]
            execution_path = sessions / "clean-execution.jsonl"
            execution_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            parsed = SessionStatsReader(sessions)._parse_file(execution_path)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.tokens["total_tokens"], 60)

    def test_today_uses_local_calendar_day_not_previous_24_hours(self):
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp)
            local_timezone = timezone(timedelta(hours=8))
            now = datetime(2026, 8, 19, 9, 0, tzinfo=local_timezone)
            yesterday = datetime(2026, 8, 18, 23, 59, 59, tzinfo=local_timezone)
            today = datetime(2026, 8, 19, 0, 0, tzinfo=local_timezone)
            self._write_session(
                sessions / "yesterday-main.jsonl",
                "user",
                [{"total_tokens": 100}],
                started_at=yesterday,
            )
            self._write_session(
                sessions / "today-main.jsonl",
                "user",
                [{"total_tokens": 200}],
                started_at=today,
            )
            self._write_session(
                sessions / "yesterday-subagent.jsonl",
                "subagent",
                [{"total_tokens": 300}],
                started_at=yesterday,
            )
            self._write_session(
                sessions / "today-subagent.jsonl",
                "subagent",
                [{"total_tokens": 400}],
                started_at=today,
            )

            reader = SessionStatsReader(sessions)
            main, subagent = reader.aggregate(1, calendar_day=True, now=now)
            rolling_main, rolling_subagent = reader.aggregate(1, now=now)

            self.assertEqual(main.sessions, 1)
            self.assertEqual(main.total_tokens, 200)
            self.assertEqual(subagent.sessions, 1)
            self.assertEqual(subagent.total_tokens, 400)
            self.assertEqual(rolling_main.sessions, 2)
            self.assertEqual(rolling_subagent.sessions, 2)


class DiagnosticLogicTests(unittest.TestCase):
    def test_parses_codex_json_lifecycle_and_usage(self):
        self.assertEqual(
            parse_codex_json_event(
                {"type": "thread.started", "thread_id": "thread-123"}
            ),
            {"thread_id": "thread-123"},
        )
        self.assertTrue(
            parse_codex_json_event(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "API_OK"},
                }
            )["first_response"]
        )
        completed = parse_codex_json_event(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "output_tokens": 8,
                },
            }
        )
        self.assertTrue(completed["completed"])
        self.assertEqual(completed["usage"]["cached_input_tokens"], 60)

    def test_classifies_common_api_and_transport_errors(self):
        cases = {
            "HTTP 401 Unauthorized": "401 认证失败",
            "model alpha returned 404 not found": "404 模型不可用",
            "429 Too Many Requests": "429 请求受限",
            "request timed out": "请求超时",
            "TLS connection failed": "网络错误",
            "unexpected CLI exit": "CLI 失败",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify_diagnostic_error(message), expected)

    def test_summarizes_success_latency_and_output_speed(self):
        results = [
            DiagnosticResult(
                "主模型", "a", "high", 1, True, "成功", 1.0, 3.0,
                output_tokens=20, tokens_per_second=10.0,
            ),
            DiagnosticResult(
                "子代理", "b", "medium", 1, False, "请求超时", None, 5.0,
            ),
        ]
        summary = summarize_diagnostics(results)
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.succeeded, 1)
        self.assertEqual(summary.success_rate, 50.0)
        self.assertEqual(summary.average_first_response, 1.0)
        self.assertEqual(summary.average_total_time, 4.0)
        self.assertEqual(summary.average_tokens_per_second, 10.0)

    def test_finds_bundled_codex_executable_before_path(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp)
            executable = local / "OpenAI" / "Codex" / "bin" / "version" / "codex.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            found = find_codex_executable(local, lambda _name: None)
            self.assertEqual(found, executable)

    def test_diagnostic_passes_model_to_codex_once(self):
        from codex_agent_console import CodexDiagnosticsRunner

        runner = CodexDiagnosticsRunner(
            Path("codex.exe"), Path(".codex"), Path("workspace")
        )
        with mock.patch("codex_agent_console.subprocess.Popen") as popen:
            popen.side_effect = OSError("stop after command capture")
            runner._run_one("主模型", "gpt-test", "high", 1, 10, lambda _value: None)

        command = popen.call_args.args[0]
        self.assertEqual(command.count("-m"), 1)
        self.assertEqual(command[command.index("-m") + 1], "gpt-test")

    def test_tails_cumulative_token_events_incrementally(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session.jsonl"

            def token_line(input_tokens: int, cached: int, output: int) -> str:
                return json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": input_tokens,
                                    "cached_input_tokens": cached,
                                    "output_tokens": output,
                                }
                            },
                        },
                    }
                ) + "\n"

            session.write_text(token_line(100, 50, 2), encoding="utf-8")
            tail = SessionTokenTail(session)
            self.assertEqual(tail.read_latest()["output_tokens"], 2)
            with session.open("a", encoding="utf-8") as handle:
                handle.write(token_line(100, 50, 12))
            latest = tail.read_latest()
            self.assertEqual(latest["input_tokens"], 100)
            self.assertEqual(latest["cached_input_tokens"], 50)
            self.assertEqual(latest["output_tokens"], 12)


class CustomApiTests(unittest.TestCase):
    def test_base_url_builds_supported_api_paths(self):
        base = "https://api.example.com/v1"
        self.assertEqual(
            custom_api_base_url("https://api.example.com/v1/chat/completions"),
            base,
        )
        self.assertEqual(custom_api_endpoint_url(base), f"{base}/chat/completions")
        self.assertEqual(
            custom_api_endpoint_url(base, "responses"), f"{base}/responses"
        )
        self.assertEqual(
            custom_api_endpoint_url("https://api.example.com", "completions"),
            "https://api.example.com/v1/completions",
        )
        self.assertEqual(
            custom_api_endpoint_url(
                "https://api.anthropic.com/v1", "anthropic_messages"
            ),
            "https://api.anthropic.com/v1/messages",
        )

    def test_fetch_models_uses_models_endpoint_and_auth(self):
        class Handler(BaseHTTPRequestHandler):
            authorization = ""
            path = ""

            def do_GET(self):
                Handler.authorization = self.headers.get("Authorization", "")
                Handler.path = self.path
                body = json.dumps(
                    {"data": [{"id": "alpha"}, {"id": "beta"}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            models = fetch_custom_api_models(
                f"http://127.0.0.1:{server.server_port}/v1",
                "bearer",
                "test-model-key",
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(Handler.path, "/v1/models")
        self.assertEqual(Handler.authorization, "Bearer test-model-key")
        self.assertEqual(models, ["alpha", "beta"])

    def test_fetch_claude_models_uses_anthropic_headers(self):
        class Handler(BaseHTTPRequestHandler):
            api_key = ""
            version = ""
            path = ""

            def do_GET(self):
                Handler.api_key = self.headers.get("x-api-key", "")
                Handler.version = self.headers.get("anthropic-version", "")
                Handler.path = self.path
                body = json.dumps(
                    {"data": [{"id": "claude-sonnet"}, {"id": "claude-haiku"}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            models = fetch_custom_api_models(
                f"http://127.0.0.1:{server.server_port}/v1",
                "x-api-key",
                "test-anthropic-key",
                api_type="anthropic_messages",
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(Handler.path, "/v1/models")
        self.assertEqual(Handler.api_key, "test-anthropic-key")
        self.assertEqual(Handler.version, ANTHROPIC_API_VERSION)
        self.assertEqual(models, ["claude-sonnet", "claude-haiku"])

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI only")
    def test_dpapi_round_trip_and_store_does_not_write_plaintext_key(self):
        secret = "temporary-test-key-not-a-real-credential"
        encrypted = protect_secret(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(unprotect_secret(encrypted), secret)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "apis.json"
            endpoint = CustomApiEndpoint(
                "endpoint-a",
                "Local API",
                "http://127.0.0.1:8000/v1/chat/completions",
                "test-model",
                encrypted_api_key=encrypted,
            )
            store = CustomApiStore(path)
            store.save([endpoint])
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
            loaded = store.load()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(unprotect_secret(loaded[0].encrypted_api_key), secret)

    def test_endpoint_validation_requires_https_except_localhost(self):
        validate_custom_api_endpoint(
            CustomApiEndpoint(
                "local",
                "Local",
                "http://localhost:8080/v1/chat/completions",
                "model",
            )
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_custom_api_endpoint(
                CustomApiEndpoint(
                    "remote",
                    "Remote",
                    "http://api.example.com/v1/chat/completions",
                    "model",
                )
            )

    def test_summary_prefers_reliable_api_over_faster_failed_api(self):
        results = [
            CustomApiResult(
                "a", "Reliable", "m", 1, True, "成功", 200, 0.2, 1.0,
                output_tokens=10, tokens_per_second=10.0,
            ),
            CustomApiResult(
                "a", "Reliable", "m", 2, True, "成功", 200, 0.2, 1.1,
                output_tokens=10, tokens_per_second=9.0,
            ),
            CustomApiResult(
                "b", "Flaky", "m", 1, True, "成功", 200, 0.1, 0.4,
                output_tokens=10, tokens_per_second=20.0,
            ),
            CustomApiResult(
                "b", "Flaky", "m", 2, False, "请求超时", None, None, 3.0,
            ),
        ]
        summaries = summarize_custom_apis(results)
        fastest, stable = choose_custom_api_winners(summaries)
        self.assertEqual(fastest.name, "Reliable")
        self.assertEqual(stable.name, "Reliable")
        reliable = next(item for item in summaries if item.name == "Reliable")
        self.assertEqual(reliable.success_rate, 100.0)
        self.assertIsNotNone(reliable.jitter_percent)

    def test_streaming_benchmark_uses_bearer_key_and_parses_usage(self):
        class Handler(BaseHTTPRequestHandler):
            authorization = ""

            def do_POST(self):
                Handler.authorization = self.headers.get("Authorization", "")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = (
                    b'data: {"choices":[{"delta":{"content":"API"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"_OK"}}],'
                    b'"usage":{"prompt_tokens":2000,'
                    b'"prompt_tokens_details":{"cached_tokens":1200},'
                    b'"completion_tokens":2}}\n\n',
                    b"data: [DONE]\n\n",
                )
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = CustomApiEndpoint(
                "local",
                "Local",
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                "test-model",
            )
            results: list[CustomApiResult] = []
            runner = CustomApiBenchmarkRunner()
            runner.run(
                [(endpoint, "test-bearer-key")],
                1,
                10.0,
                lambda _event: None,
                results.append,
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(Handler.authorization, "Bearer test-bearer-key")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].output_tokens, 2)
        self.assertEqual(results[0].input_tokens, 2000)
        self.assertEqual(results[0].cached_input_tokens, 1200)
        self.assertEqual(results[0].cache_hit_rate, 60.0)
        self.assertFalse(results[0].tokens_estimated)
        self.assertIsNotNone(results[0].first_response_seconds)

    def test_cache_benchmark_warms_then_measures_a_reused_prompt(self):
        class Handler(BaseHTTPRequestHandler):
            payloads: list[dict[str, object]] = []

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                Handler.payloads.append(
                    json.loads(self.rfile.read(length).decode("utf-8"))
                )
                cached_tokens = 0 if len(Handler.payloads) == 1 else 1800
                body = json.dumps(
                    {
                        "choices": [{"message": {"content": "API_OK"}}],
                        "usage": {
                            "prompt_tokens": 2000,
                            "prompt_tokens_details": {
                                "cached_tokens": cached_tokens
                            },
                            "completion_tokens": 2,
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = CustomApiEndpoint(
                "cache",
                "Cache",
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                "test-model",
            )
            results: list[CustomApiResult] = []
            CustomApiBenchmarkRunner().run(
                [(endpoint, "")],
                1,
                10.0,
                lambda _event: None,
                results.append,
                cache_test=True,
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(len(Handler.payloads), 2)
        self.assertEqual(Handler.payloads[0], Handler.payloads[1])
        self.assertEqual(
            Handler.payloads[0]["messages"][0]["content"],
            CUSTOM_API_CACHE_PROMPT,
        )
        self.assertFalse(Handler.payloads[0]["stream"])
        self.assertEqual([result.cache_phase for result in results], ["warmup", "measure"])
        self.assertEqual(results[0].cache_hit_rate, 0.0)
        self.assertEqual(results[1].cache_hit_rate, 90.0)
        summary = summarize_custom_apis(results)[0]
        self.assertEqual(summary.total, 1)
        self.assertEqual(summary.cache_hit_rate, 90.0)

    def test_claude_messages_streaming_benchmark_parses_sse_and_usage(self):
        class Handler(BaseHTTPRequestHandler):
            headers = {}
            payload = {}

            def do_POST(self):
                Handler.headers = dict(self.headers.items())
                length = int(self.headers.get("Content-Length", "0"))
                Handler.payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = (
                    b": keep-alive\n\n",
                    b"event: message_start\n\n"
                    b'data: {"type":"message_start","message":{"usage":'
                    b'{"input_tokens":120,"cache_creation_input_tokens":0,'
                    b'"cache_read_input_tokens":0}}}\n\n',
                    b"event: content_block_start\n\n"
                    b'data: {"type":"content_block_start","content_block":'
                    b'{"type":"text","text":""}}\n\n',
                    b"event: content_block_delta\n\n"
                    b'data: {"type":"content_block_delta","delta":'
                    b'{"type":"text_delta","text":"API"}}\n\n',
                    b"event: content_block_delta\n\n"
                    b'data: {"type":"content_block_delta","delta":'
                    b'{"type":"text_delta","text":"_OK"}}\n\n',
                    b"event: message_delta\n\n"
                    b'data: {"type":"message_delta","usage":{"output_tokens":3}}\n\n',
                    b"event: message_stop\n\n"
                    b'data: {"type":"message_stop"}\n\n',
                )
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = CustomApiEndpoint(
                "claude",
                "Claude",
                f"http://127.0.0.1:{server.server_port}/v1/messages",
                "claude-sonnet",
                auth_mode="x-api-key",
                api_type="anthropic_messages",
            )
            results: list[CustomApiResult] = []
            CustomApiBenchmarkRunner().run(
                [(endpoint, "test-claude-key")],
                1,
                10.0,
                lambda _event: None,
                results.append,
            )
        finally:
            server.shutdown()
            server.server_close()

        headers = {key.casefold(): value for key, value in Handler.headers.items()}
        self.assertEqual(headers["x-api-key"], "test-claude-key")
        self.assertEqual(headers["anthropic-version"], ANTHROPIC_API_VERSION)
        self.assertNotIn("authorization", headers)
        self.assertEqual(Handler.payload["max_tokens"], 16)
        self.assertTrue(Handler.payload["stream"])
        self.assertEqual(Handler.payload["messages"][0]["role"], "user")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].api_type, "anthropic_messages")
        self.assertEqual(results[0].output_tokens, 3)
        self.assertEqual(results[0].input_tokens, 120)
        self.assertEqual(results[0].cached_input_tokens, 0)

    def test_claude_cache_benchmark_uses_cache_control_and_reads_cache_usage(self):
        class Handler(BaseHTTPRequestHandler):
            payloads: list[dict[str, object]] = []
            headers: list[dict[str, str]] = []

            def do_POST(self):
                Handler.headers.append(dict(self.headers.items()))
                length = int(self.headers.get("Content-Length", "0"))
                Handler.payloads.append(
                    json.loads(self.rfile.read(length).decode("utf-8"))
                )
                is_warmup = len(Handler.payloads) == 1
                body = json.dumps(
                    {
                        "id": "msg_test",
                        "type": "message",
                        "content": [{"type": "text", "text": "API_OK"}],
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 1900 if is_warmup else 0,
                            "cache_read_input_tokens": 0 if is_warmup else 1900,
                            "output_tokens": 2,
                        },
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = CustomApiEndpoint(
                "claude-cache",
                "Claude Cache",
                f"http://127.0.0.1:{server.server_port}/v1/messages",
                "claude-sonnet",
                auth_mode="x-api-key",
                api_type="anthropic_messages",
            )
            results: list[CustomApiResult] = []
            CustomApiBenchmarkRunner().run(
                [(endpoint, "test-claude-key")],
                1,
                10.0,
                lambda _event: None,
                results.append,
                cache_test=True,
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(len(Handler.payloads), 2)
        self.assertEqual(Handler.payloads[0], Handler.payloads[1])
        self.assertFalse(Handler.payloads[0]["stream"])
        content = Handler.payloads[0]["messages"][0]["content"]
        self.assertEqual(content[0]["text"], CUSTOM_API_CACHE_PROMPT)
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        cache_headers = {
            key.casefold(): value for key, value in Handler.headers[0].items()
        }
        self.assertEqual(
            cache_headers["anthropic-beta"], "prompt-caching-2024-07-31"
        )
        self.assertEqual(results[0].input_tokens, 2000)
        self.assertEqual(results[0].cache_hit_rate, 0.0)
        self.assertEqual(results[1].input_tokens, 2000)
        self.assertEqual(results[1].cached_input_tokens, 1900)
        self.assertEqual(results[1].cache_hit_rate, 95.0)
        summary = summarize_custom_apis(results)[0]
        self.assertEqual(summary.cache_hit_rate, 95.0)

    def test_responses_streaming_benchmark_parses_response_events(self):
        class Handler(BaseHTTPRequestHandler):
            payload = {}

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                Handler.payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = (
                    b"event: response.created\n\n"
                    b'data: {"type":"response.created"}\n\n',
                    b"event: response.output_text.delta\n\n"
                    b'data: {"type":"response.output_text.delta","delta":"API"}\n\n',
                    b"event: response.output_text.delta\n\n"
                    b'data: {"type":"response.output_text.delta","delta":"_OK"}\n\n',
                    b"event: response.completed\n\n"
                    b'data: {"type":"response.completed","response":{"usage":'
                    b'{"input_tokens":1600,"input_tokens_details":'
                    b'{"cached_tokens":1280},"output_tokens":2}}}\n\n',
                )
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = CustomApiEndpoint(
                "responses",
                "Responses",
                f"http://127.0.0.1:{server.server_port}/v1/responses",
                "response-model",
                api_type="responses",
            )
            results: list[CustomApiResult] = []
            CustomApiBenchmarkRunner().run(
                [(endpoint, "")],
                1,
                10.0,
                lambda _event: None,
                results.append,
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(Handler.payload["input"], CUSTOM_API_PROMPT)
        self.assertNotIn("messages", Handler.payload)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].api_type, "responses")
        self.assertEqual(results[0].output_tokens, 2)
        self.assertEqual(results[0].input_tokens, 1600)
        self.assertEqual(results[0].cached_input_tokens, 1280)
        self.assertEqual(results[0].cache_hit_rate, 80.0)
        self.assertFalse(results[0].tokens_estimated)


if __name__ == "__main__":
    unittest.main()
