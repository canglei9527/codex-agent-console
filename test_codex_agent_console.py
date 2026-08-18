# -*- coding: utf-8 -*-
import json
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from codex_agent_console import (
    AgentSettings,
    ConfigStore,
    DiagnosticResult,
    DUAL_MODE_POLICY_START,
    SessionTokenTail,
    SessionStatsReader,
    classify_diagnostic_error,
    find_codex_executable,
    has_dual_mode_policy,
    merge_dual_mode_policy,
    parse_codex_json_event,
    summarize_diagnostics,
    update_toml_values,
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

    def test_dual_mode_policy_is_added_once_and_removed_without_losing_user_text(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"
            config.write_text(
                'developer_instructions = "Keep my existing instruction."\n',
                encoding="utf-8",
            )
            store = ConfigStore(config)
            dual = AgentSettings(
                "gpt-5.6-sol", "high", "gpt-5.6-terra", "medium", True, 4
            )
            store.save(dual)
            store.save(dual)
            enabled_data = tomllib.loads(config.read_text(encoding="utf-8"))
            enabled_instructions = enabled_data["developer_instructions"]
            self.assertEqual(enabled_instructions.count(DUAL_MODE_POLICY_START), 1)
            self.assertIn("Keep my existing instruction.", enabled_instructions)
            self.assertIn("model to `gpt-5.6-terra`", enabled_instructions)
            self.assertIn("reasoning_effort to `medium`", enabled_instructions)
            self.assertTrue(has_dual_mode_policy(enabled_instructions))
            self.assertTrue(store.load().agents_enabled)

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
            self.assertFalse(disabled_data["agents"]["enabled"])
            self.assertFalse(store.load().agents_enabled)

    def test_policy_merge_handles_empty_and_unmanaged_instructions(self):
        enabled = merge_dual_mode_policy(None, True, "gpt-5.6-terra", "medium")
        self.assertTrue(has_dual_mode_policy(enabled))
        self.assertEqual(merge_dual_mode_policy(enabled, False), "")


class SessionStatsTests(unittest.TestCase):
    @staticmethod
    def _write_session(path: Path, thread_source: str, totals: list[dict]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        records = [
            {
                "timestamp": now,
                "type": "session_meta",
                "payload": {
                    "timestamp": now,
                    "thread_source": thread_source,
                    "model": "gpt-5.6-terra" if thread_source == "subagent" else "gpt-5.6-sol",
                    "collaboration_mode": {
                        "settings": {
                            "reasoning_effort": "medium" if thread_source == "subagent" else "high"
                        }
                    },
                },
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


if __name__ == "__main__":
    unittest.main()
