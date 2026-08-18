# Codex Agent Console

一个非官方的 Codex Windows 桌面控制台，用于管理主模型、子代理默认值和双模型协调策略，并查看本机 Token 与缓存命中统计。

当前版本：`1.3.4`

> 本项目是社区工具，不是 OpenAI 官方产品。它只修改 Codex 官方支持的本地配置项。

## 下载

从 [GitHub Releases](https://github.com/canglei9527/codex-agent-console/releases/latest) 下载最新的 `CodexAgentConsole-v*.exe`，双击即可运行。目标电脑不需要安装 Python，但需要已经安装并使用过 Codex。

## 功能

- 主模型：`model`、`model_reasoning_effort`
- 子代理：`agents.default_subagent_model`、`agents.default_subagent_reasoning_effort`
- 模式：`agents.enabled = true|false`
- 双模型协调策略：启用时通过 `developer_instructions` 将简单、明确、低风险的执行直接交给一个专属子模型全程完成；复杂、模糊、多步骤或跨模块工作仍由主模型规划、拆解、整合和审查
- 专属执行子模型：保存时在 `~/.codex/agents/` 写入受控代理文件，模型和思考级别严格使用 GUI 选择值，优先级高于子代理默认值
- 一键按钮：“启用双模型”与“恢复普通模式”
- 并发：`agents.max_concurrent_threads_per_session`
- 统计：主任务与执行子模型分别显示输入、缓存输入、输出、推理输出、总 Token、缓存命中率，以及会话实际使用的模型/思考级别；系统 guardian 后台会话不混入执行子模型统计
- API 诊断：可测试主模型、子代理或两者，支持 1/3/5 次采样与 10–600 秒超时
- 诊断指标：连通成功率、首响应、API 总耗时、CLI 收尾耗时、输入/缓存/输出 Token 和 Tokens/s
- 错误定位：区分 401 认证失败、404 模型不可用、429 请求受限、超时、网络错误和其他 CLI 故障，并保留每次请求的完整错误详情
- 自定义 API：持久化保存多个 OpenAI 兼容接口，可勾选多个接口顺序测速
- 接口类型：支持 Chat Completions、Responses API 和 Legacy Completions，按类型生成请求体并解析流式事件
- 基础 URL：只需填写例如 `https://api.example.com/v1`，自动补全具体接口路径；可从 `/v1/models` 自动获取模型
- 自定义测速：比较成功率、平均首响应、平均总耗时、耗时波动和 Tokens/s，并标出最快与最稳定接口
- API Key 安全：支持 Bearer、`x-api-key` 和无鉴权；Key 使用 Windows DPAPI 加密，不明文写入配置或显示在界面中

配置保存前会备份为 `~/.codex/config.toml.codex-agent-console.bak`。控制台只增删自己带标记的协调策略，不会覆盖已有的 `developer_instructions`。保存后必须完全退出并重新打开 Codex Desktop，再新建任务；当前会话不会热切换。

## API 诊断

点击主窗口的“API 诊断”打开独立诊断窗口。测试会通过本机 Codex CLI 和现有认证发起真实、只读、禁用子代理的最小请求，不读取 API Key。每次测试都会产生 Token，并进入正常的本机会话统计；默认只执行 1 次。

“API 总耗时”取自 Codex 的 `turn.completed` 事件，“CLI 收尾”单独记录请求完成后本地进程退出所需时间。Codex CLI 的 JSONL 输出不是逐 Token 流；窗口会尽可能从会话 `token_count` 事件刷新累计输出与实时吞吐，最终 Token 数以 `turn.completed.usage` 为准。

CLI 调用方式依据 [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)。

## 官方订阅与自定义 API

Codex 官方订阅使用 ChatGPT 登录，不是可粘贴到 HTTP 接口中的 API Key。测试官方订阅时，点击主窗口的“API 诊断”，或在“自定义 API”窗口点击“测试 Codex 官方订阅”；程序会复用本机 Codex CLI 的现有登录状态。官方登录方式见 [Codex authentication](https://developers.openai.com/codex/auth/)。

“自定义 API”用于独立购买的 OpenAI Platform API Key、第三方兼容 API 或自建服务。点击“新增 API”，填写名称、基础 URL、接口类型和鉴权方式；点击“获取模型”即可从兼容的 `/v1/models` 接口读取模型，再保存并测速。例如基础 URL 可以填写 `https://api.openai.com/v1`，程序会根据所选类型使用 `/chat/completions`、`/responses` 或 `/completions`。其 API 用量和 Codex/ChatGPT 订阅分开计费。

默认执行 3 次以计算稳定性。测试按 API 顺序逐个执行，避免并发抢占本机带宽影响比较；每次都会产生对应服务的实际 Token 消耗。远程接口必须使用 HTTPS，只有 localhost 可使用 HTTP。

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
