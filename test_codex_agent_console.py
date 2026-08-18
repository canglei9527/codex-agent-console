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
    DUAL_MODE_POLICY_START,
    SessionStatsReader,
    has_dual_mode_policy,
    merge_dual_mode_policy,
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


if __name__ == "__main__":
    unittest.main()
