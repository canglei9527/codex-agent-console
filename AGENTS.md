# Codex Agent Console project instructions

## Release workflow

- Repository: `canglei9527/codex-agent-console`; publish from `main`.
- Ask for explicit confirmation before `git push`, creating a tag, creating a GitHub Release, or uploading a release asset. A prior confirmation covers only the explicitly named release action.
- Before publishing, run the focused tests (`py -3.11 -m unittest -v`), syntax/build checks, and a GUI smoke check when the executable changed. Keep build output and running executables out of the source commit.
- Push source first with `git push origin main`, then create the version tag and Release with the GitHub CLI. Verify that the remote branch and tag point to the intended commit.
- Prefer the existing Git Credential Manager for GitHub API access. Do not start browser login or `gh auth login` first when `git push` already works. Obtain the GitHub credential through `git credential fill` only inside the release command, pass its password to `GH_TOKEN` for that process, and remove the environment variable immediately afterward. Never print, log, persist, or inspect the token value.
- If Git has an HTTP proxy configured, pass that proxy to the GitHub CLI process without displaying its value so the API uses the same network route as Git.
- Create releases with the versioned asset from `release\CodexAgentConsole-v<tag>.exe`, for example:

  `gh release create v1.3.8 release\CodexAgentConsole-v1.3.8.exe#CodexAgentConsole-v1.3.8.exe --repo canglei9527/codex-agent-console --target main`

- After publishing, verify `gh release view <tag> --json tagName,isDraft,isPrerelease,targetCommitish,assets,url`; confirm the Release is public, the asset state is `uploaded`, and its remote size/digest matches the local file. Report the Release URL and any failed or unrun checks.
- Use browser automation only when the local credential helper cannot authenticate the API and the user explicitly authorizes an alternative login flow.
