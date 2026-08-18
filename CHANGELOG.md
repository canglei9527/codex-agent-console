# Changelog

## 1.1.0 - 2026-08-18

- Add a standalone API diagnostics window for main and subagent models.
- Add configurable targets, attempts, and request timeouts.
- Report connectivity rate, first response, API duration, CLI cleanup, Token usage, and output throughput.
- Classify authentication, unavailable-model, rate-limit, timeout, network, and CLI failures with per-request details.
- Discover the bundled Codex executable without reading authentication files or API keys.

## 1.0.1 - 2026-08-18

- Reduce automatic Token statistics refresh from five seconds to one second.
- Keep configuration changes immediate while clearly retaining new-task scope.

## 1.0.0 - 2026-08-18

- Add a native Windows GUI for Codex main-model and subagent defaults.
- Add dual-model orchestration and one-click normal-mode restoration.
- Add main/subagent token, cache-hit, and actual-model statistics.
- Add atomic `config.toml` updates with automatic backups.
- Add a portable one-file Windows executable build.
