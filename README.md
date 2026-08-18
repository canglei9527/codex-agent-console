# Codex Agent Console

一个非官方的 Codex Windows 桌面控制台，用于管理主模型、子代理默认值和双模型协调策略，并查看本机 Token 与缓存命中统计。

当前版本：`1.1.0`

> 本项目是社区工具，不是 OpenAI 官方产品。它只修改 Codex 官方支持的本地配置项。

## 下载

从 [GitHub Releases](https://github.com/canglei9527/codex-agent-console/releases/latest) 下载最新的 `CodexAgentConsole-v*.exe`，双击即可运行。目标电脑不需要安装 Python，但需要已经安装并使用过 Codex。

## 功能

- 主模型：`model`、`model_reasoning_effort`
- 子代理：`agents.default_subagent_model`、`agents.default_subagent_reasoning_effort`
- 模式：`agents.enabled = true|false`
- 双模型协调策略：启用时通过 `developer_instructions` 要求主模型规划、整合和审查，并在派发执行子代理时显式传入 GUI 选择的模型和思考级别
- 一键按钮：“启用双模型”与“恢复普通模式”
- 并发：`agents.max_concurrent_threads_per_session`
- 统计：主任务与子代理分别显示输入、缓存输入、输出、推理输出、总 Token、缓存命中率，以及会话实际使用的模型/思考级别
- API 诊断：可测试主模型、子代理或两者，支持 1/3/5 次采样与 10–600 秒超时
- 诊断指标：连通成功率、首响应、API 总耗时、CLI 收尾耗时、输入/缓存/输出 Token 和 Tokens/s
- 错误定位：区分 401 认证失败、404 模型不可用、429 请求受限、超时、网络错误和其他 CLI 故障，并保留每次请求的完整错误详情

配置保存前会备份为 `~/.codex/config.toml.codex-agent-console.bak`。控制台只增删自己带标记的协调策略，不会覆盖已有的 `developer_instructions`。修改只对新任务生效。

## API 诊断

点击主窗口的“API 诊断”打开独立诊断窗口。测试会通过本机 Codex CLI 和现有认证发起真实、只读、禁用子代理的最小请求，不读取 API Key。每次测试都会产生 Token，并进入正常的本机会话统计；默认只执行 1 次。

“API 总耗时”取自 Codex 的 `turn.completed` 事件，“CLI 收尾”单独记录请求完成后本地进程退出所需时间。Codex CLI 的 JSONL 输出不是逐 Token 流；窗口会尽可能从会话 `token_count` 事件刷新累计输出与实时吞吐，最终 Token 数以 `turn.completed.usage` 为准。

CLI 调用方式依据 [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)。

## 系统要求

- Windows 10 或 Windows 11
- 已安装 Codex，并存在可读写的 `~/.codex` 目录
- 从源码运行或构建时需要 Python 3.11+

## 运行

双击 `start.bat`，或运行：

```powershell
py -3.11 codex_agent_console.py
```

## 打包

```powershell
powershell -ExecutionPolicy Bypass -File .\build_windows.ps1
```

生成的 `dist\CodexAgentConsole.exe` 可复制到其他已安装 Codex 的 Windows 电脑使用，不要求目标电脑安装 Python。

## 隐私与安全

统计只读取 `~/.codex/sessions/**/*.jsonl` 中的会话元数据和 `token_count` 事件，不读取或展示对话正文、API Key 或认证文件。详细说明见 [SECURITY.md](SECURITY.md)。

## 测试

```powershell
py -3.11 -m unittest -v
```

## 许可证

[MIT](LICENSE)
