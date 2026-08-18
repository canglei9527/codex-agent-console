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

Before replacing `config.toml`, the application validates the TOML, creates a
backup named `config.toml.codex-agent-console.bak`, and performs an atomic
write.

## Reporting a vulnerability

Please open a GitHub security advisory for vulnerabilities. Do not include API
keys, access tokens, private prompts, or session files in public issues.
