# Changelog

## 1.4.1 - 2026-08-20

- Ignore SSE heartbeat/comment lines during custom API benchmarks so compatible Claude Messages responses are not incorrectly parsed as plain JSON errors.

## 1.4.0 - 2026-08-19

- Add native Claude Messages (Anthropic) API detection with `x-api-key` authentication, Anthropic version headers, model discovery, streaming response parsing, usage parsing, and prompt-cache measurement.

## 1.3.9 - 2026-08-19

- Add an opt-in custom API cache-hit benchmark with a cache warmup request, a repeated measurement request, and cache-hit reporting when the provider exposes input usage.
- Correct execution-agent Token totals for forked sessions that embed a primary-session usage history, while preserving clean forked-session totals.

## 1.3.8 - 2026-08-19

- Fix the “Today” Token filter to start at the local computer's midnight instead of including the previous 24 hours of sessions.
- Keep the 7-day and 30-day filters as rolling windows.

## 1.3.7 - 2026-08-19

- Fix execution-agent model inheritance: delegations now explicitly use `fork_turns = "none"` together with the configured subagent model and reasoning effort, instead of inheriting the primary model through a full-history fork.
- Change dual-model orchestration to a teacher-executor loop: the main model gives concise guidance, reviews the result, requests focused corrections, and only takes over remaining work when the executor cannot finish.
- Add a completion-first rule so implementation tasks must progress through available tools and verification instead of ending with only a plan or advice.
- Add an "Apply to Desktop" action that reloads only the detected Windows Desktop `app-server`, so new tasks can use saved settings without closing the Desktop window.

## 1.3.6 - 2026-08-19

- Correct execution-agent statistics when a forked subagent session embeds parent session metadata.
- Read the current Codex `effort` and `thread_settings.reasoning_effort` fields so actual main and execution model cards show the configured reasoning level.
- Preserve the execution subagent's own start time for accurate latest-model reporting.

## 1.3.5 - 2026-08-18

- Move dual-model routing from `config.toml` developer instructions to the documented global `~/.codex/AGENTS.md` instruction chain, using a non-empty `AGENTS.override.md` when it is active.
- Preserve user-authored global guidance while adding or removing only the console-owned policy block.
- Route clear, low-risk writing and transformation tasks to the execution subagent as well as coding tasks.
- Migrate and remove the ineffective legacy policy block from `developer_instructions` on the next save.

## 1.3.0 - 2026-08-18

- Add Chat Completions, Responses API, and legacy Completions endpoint types.
- Generate type-specific request bodies and parse Chat/Responses/Completions streaming events.
- Replace the required full endpoint URL with a provider base URL and automatic path completion.
- Add background model discovery through the OpenAI-compatible `/v1/models` endpoint.
- Improve entry-field contrast so entered values remain readable in the dark UI.

## 1.2.0 - 2026-08-18

- Add persistent multi-select benchmarking for OpenAI Chat Completions compatible APIs.
- Add Bearer, `x-api-key`, and unauthenticated endpoint support with Windows DPAPI encrypted API keys.
- Report success rate, first response, total latency, latency jitter, output Tokens, and Tokens/s.
- Rank the fastest and most stable APIs with reliability taking priority over raw speed.
- Add streaming SSE parsing, usage fallback estimation, cancellation, and detailed HTTP errors.
- Link official Codex subscription testing to the existing Codex CLI diagnostics flow.
- Clarify that ChatGPT subscription login and OpenAI Platform API keys are separate credentials.

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
