# Security

## Data access

Codex Agent Console reads only configuration values from
`~/.codex/config.toml` and token/session metadata from
`~/.codex/sessions/**/*.jsonl`. It does not display conversation content and
does not read Codex authentication files or API keys.

API diagnostics launch the installed Codex CLI with the user's existing
authentication and a read-only sandbox. Diagnostics read only JSON lifecycle
events and cumulative `token_count` metadata, never assistant or user message
content. These are real model requests and therefore consume Tokens.

Custom API definitions are saved in
`~/.codex/codex-agent-console-apis.json`. API keys are encrypted with Windows
DPAPI for the current Windows user before they are written to disk; plaintext
keys are never displayed or stored in the configuration file. The application
decrypts a selected key only in memory while constructing its benchmark
request. Remote endpoints must use HTTPS, with HTTP allowed only for localhost.

Custom API benchmarks send only the fixed connectivity prompt and the saved
model name to the configured endpoint. The request format follows the selected
Chat Completions, Responses, or legacy Completions type. Model discovery sends
an authenticated GET request to the conventional `/v1/models` endpoint. These
are real requests and consume that provider's API quota. Codex subscription
diagnostics use the Codex CLI login; they do not expose or reuse subscription
credentials as custom API keys.

Before replacing `config.toml`, the application validates the TOML, creates a
backup named `config.toml.codex-agent-console.bak`, and performs an atomic
write.

## Reporting a vulnerability

Please open a GitHub security advisory for vulnerabilities. Do not include API
keys, access tokens, private prompts, or session files in public issues.
