# Cursor / Claude CLI OpenAI Wrapper

将本机的 **Claude Code CLI**（`claude`）与 **Cursor Agent CLI**（`agent`）包装成兼容 **OpenAI Chat Completions** 与 **Responses** 的 HTTP 服务，便于直接复用 `OpenAI SDK`、`curl` 或第三方客户端（将 `base_url` 指到本服务的 `/v1`）。

按 `AGENT_SCHEDULE` 顺序调度底层 CLI；若某一 agent 在**首个正文 delta 产出前**启动失败，会自动降级到列表中的下一个 agent（详见 [Agent 调度与降级](#agent-调度与降级)）。

## 当前能力

### 正式服务（`uvicorn app.main:app`）

- `GET /healthz`：健康检查；反映 `agent_schedule` 中各 agent 可执行文件是否可用，以及 Cursor 专项状态（如 API Key 是否已配置）
- `GET /v1/models`：列出当前 CLI 可用模型（内部会调用 `agent models`）
- `POST /v1/chat/completions`：对话补全
- `POST /v1/responses`：Responses API；请求里的 `input` 会解析为内部消息后，与 Chat 共用同一套 CLI 调用、greeting、流式 keepalive 等逻辑
- Chat 与 Responses 均支持普通 JSON 响应，以及 `stream=true` 时的 SSE 流式输出
- 多 Agent 调度：默认 `claude` → `cursor` 顺序尝试；启动失败时自动降级，流式会向客户端推送降级说明

### Mock 回放服务（`uvicorn app.mock_startup_main:app`）

用于开发或联调：按 JSON 剧本重放流式/非流式响应，**不**调用真实 Claude / Cursor CLI。对外 **HTTP 路径与正式服务相同**：

- `GET /healthz`（响应中会标明 `service: mock-replay-api`）
- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`

## 前置条件

1. Python 3.10+
2. 按 `AGENT_SCHEDULE` 至少配置并可用其中一种 CLI：
   - **Claude**（默认排第一）：已安装 [Claude Code CLI](#安装-claude-code-cli若尚未有-claude-命令)，且本机已完成 `claude` 登录或 API 配置（鉴权由 Claude CLI 自行处理，wrapper 不注入 `ANTHROPIC_*` 环境变量）
   - **Cursor**：已安装 [Cursor Agent CLI](#安装-cursor-agent-cli若尚未有-agent-命令)，且本机已完成 `agent login`（或使用 `CURSOR_API_KEY`，[Cursor API Key（可选）](#cursor-api-key可选)）
3. 若调度列表中靠前的 agent 不可用，服务仍可运行并在请求时降级到后续 agent；`GET /healthz` 在**任一** scheduled agent 可用时为 `ok`，否则为 `degraded`

## 安装依赖

```bash
pip install -r requirements.txt
```

## 配置文件初始化

仓库中的 `app/config.bak.py` 为配置模板（与 `app/config.py` 结构一致）。首次使用前请**先复制为** `app/config.py`，再在 `app/config.py` 中按需填写 `CONFIG_*` 等项；`app/config.py` 通常被 `.gitignore` 忽略，避免把本地密钥提交进版本库。

**Windows CMD（项目根目录下执行）：**

```cmd
copy app\config.bak.py app\config.py
```

**Linux / macOS（项目根目录下执行）：**

```bash
cp app/config.bak.py app/config.py
```

复制完成后，用编辑器打开 `app/config.py` 修改配置即可。

## 环境变量

- `CLAUDE_BIN`：Claude Code CLI 可执行文件，默认 `claude`
- `CURSOR_BIN`：Cursor Agent CLI 可执行文件，默认 `agent`
- `AGENT_SCHEDULE`：Agent 调度顺序，逗号分隔，如 `claude,cursor` 或 `cursor,claude`；默认 `claude,cursor`。仅在对应 agent **启动失败**（首个 delta 前异常）时降级到下一个；流式过程中已产出正文后出错不会切换 agent
- `CURSOR_WORKSPACE`：CLI 执行时使用的工作区目录，默认当前目录
- `WRAPPER_API_KEY`：包装层 Bearer Token，可选；设置后会校验 `Authorization` 请求头
- `CURSOR_API_KEY`：Cursor 官方 API Key，可选；设置后会注入到 `cursor-agent` 子进程环境变量，用于在没有执行过 `agent login` 的环境（如服务器、CI）下完成 Cursor 后端鉴权
- `DEFAULT_MODEL`：默认对外模型名，默认 `cursor-agent`
- `MODEL_ALIASES`：模型别名映射，格式如 `gpt-4o=sonnet-4.6,gpt-4.1=gpt-5.4`

内置默认别名（节选）：

| 对外模型 id | 解析后传给 CLI |
|---|---|
| `cursor-agent` | `auto`（仅 Cursor；Claude 会省略 `--model` 使用本机默认） |
| `claude-opus-4.6` / `claude-sonnet-4.6` 等 | `opus-4.6` / `sonnet-4.6` 等 |
| `gpt-5.4`、`gemini-3.1-pro` 等 | 对应 Cursor 模型名 |

完整列表见 `app/config.bak.py` 中的 `DEFAULT_MODEL_ALIASES`。

- `CURSOR_TRUST`：是否自动加 `--trust`，默认开启
- `CURSOR_APPROVE_MCPS`：是否自动加 `--approve-mcps`，默认开启
- `CURSOR_FORCE`：是否自动加 `--force`
- `CURSOR_SANDBOX`：可选，传给 `--sandbox`

你也可以直接在 `app/config.py` 顶部的 `CONFIG_*` 变量里填写配置。优先级规则：

- `CONFIG_*` 有值：优先使用文件内配置
- `CONFIG_*` 为空字符串：回退使用对应环境变量

## Cursor API Key（可选）

在 `cursor.com/dashboard` → Integrations → API Keys 创建 Key（形如 `key_xxxxxxxxxxxxxxxx`）。无需 `agent login` 时，可把 Key 交给 wrapper，由其在调用 `cursor-agent` 时注入子进程环境。

PowerShell 临时启动示例：

```powershell
$env:CURSOR_API_KEY = "key_xxxxxxxxxxxxxxxx"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

也可使用 `setx CURSOR_API_KEY "..."` 写入用户环境变量，或写在 `app/config.py` 的 `CONFIG_CURSOR_API_KEY`（有值时优先于环境变量）。

服务启动后访问 `GET /healthz`，返回中 `cursor_cli.cursor_api_key_configured` 为 `true` 表示已检测到 Key（值不会被打印）。

## Agent 调度与降级

- **配置**：`AGENT_SCHEDULE` 或 `app/config.py` 中的 `CONFIG_AGENT_SCHEDULE`（如 `claude,cursor`）。
- **行为**：按列表从左到右选用 CLI；`stream_chat` / `run_chat` 在**尚未向客户端产出任何正文**时抛错，视为启动失败并尝试下一个 agent。
- **流式降级提示**：切换前会推送一条说明（Chat 为 `content` chunk，Responses 为独立 `output_item`），形如 `[claude] 启动失败: ...`。
- **ACK 文案**：首个成功 agent 的首个 delta 前会输出介入确认；`claude` 与 `cursor` 使用不同默认文案（`WRAPPER_RESPONSE_ACK_TEXT_CLAUDE` / `WRAPPER_RESPONSE_ACK_TEXT_CURSOR`）。
- **全部失败**：非流式 HTTP 502；流式返回 error 事件。

更细的时序与 `output_index` 规划见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`GET /healthz` 会检查 `agent_schedule` 中各 agent 的可执行文件是否可用（`agent_schedule.agents`），并在 `cursor_cli` 中附带 Cursor 专项状态（含 `cursor_api_key_configured`）。只要任一 scheduled agent 可用则 `status` 为 `ok`，否则为 `degraded`。`GET /v1/models` 仍通过 Cursor CLI 调用 `agent models` 列出模型（与当前调度顺序无关）。

## 调用示例

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-wrapper-key" \
  -d '{
    "model": "cursor-agent",
    "messages": [
      {"role": "system", "content": "You are concise."},
      {"role": "user", "content": "介绍一下 FastAPI"}
    ]
  }'
```

## OpenAI Python SDK 示例

```python
from openai import OpenAI

client = OpenAI(
    api_key="your-wrapper-key",
    base_url="http://127.0.0.1:8000/v1",
)

resp = client.chat.completions.create(
    model="cursor-agent",
    messages=[{"role": "user", "content": "hello"}],
)

print(resp.choices[0].message.content)
```

## 流式说明

当请求中设置 `stream=true` 时，按 `AGENT_SCHEDULE` 选用底层 CLI，例如：

**Claude（`claude`）：**

```bash
claude -p "<prompt>" --output-format stream-json --verbose --include-partial-messages [--model <resolved>]
```

**Cursor（`agent`）：**

```bash
agent -p "<prompt>" --output-format stream-json --stream-partial-output [--model <resolved>] ...
```

随后将对应 CLI 的 NDJSON 事件转换成 OpenAI 风格的 SSE `chat.completion.chunk`（或 Responses 事件）数据帧。

## 测试

```bash
pytest
```

## 已知限制

- 不支持 `tools/function calling`
- 不支持真正的多轮会话续接（无 session id 透传到底层 CLI）
- `messages` 会被压平成单个提示词再发给当前选用的 CLI
- `GET /v1/models` 仅反映 Cursor CLI 的 `agent models`，不包含 Claude 侧模型枚举
- 传给 Claude 时，`auto` / `cursor-agent` 等 Cursor 专用别名会省略 `--model`，使用本机 Claude 默认模型

---

## Tips（周边工具与可选说明）

以下内容多涉及第三方工具、开发辅助或 Cursor 产品本身的通用配置，与「在本机跑起本 wrapper」无强绑定，可按需查阅。

### 安装 Claude Code CLI（若尚未有 `claude` 命令）

请按 [Anthropic Claude Code](https://docs.anthropic.com/en/docs/claude-code) 官方说明安装，并确保在终端中可执行 `claude`、已完成登录或 API 配置。wrapper 通过 `CLAUDE_BIN`（默认 `claude`）调用，不代为设置 `ANTHROPIC_AUTH_TOKEN` 等变量。

若仅使用 Cursor、不需要 Claude，可将调度改为仅 Cursor，例如：

```bash
# Linux / macOS
export AGENT_SCHEDULE=cursor

# PowerShell
$env:AGENT_SCHEDULE = "cursor"
```

或在 `app/config.py` 中设置 `CONFIG_AGENT_SCHEDULE = "cursor"`。

### 安装 Cursor Agent CLI（若尚未有 `agent` 命令）

在 Windows PowerShell 中可执行：

```powershell
irm 'https://cursor.com/install?win32=true' | iex
agent --version
agent login
```

### Mock 回放服务（开发/联调）

接口列表见上文「当前能力 → Mock 回放服务」。默认从仓库内 `mock/mock_startup_outputs.json` 读取剧本；可通过环境变量 `MOCK_STARTUP_CONFIG` 指定其它 JSON 路径（详见 `app/mock_startup_main.py`）。

```bash
uvicorn app.mock_startup_main:app --host 0.0.0.0 --port 8000
```

### 接入 OpenClaw 的示例配置

在 OpenClaw 中将 `baseUrl` 指向本服务，例如：

```json
{
  "models": {
    "providers": {
      "cursorcli": {
        "baseUrl": "http://127.0.0.1:8000/v1",
        "apiKey": "__OPENCLAW_REDACTED__",
        "api": "openai-completions",
        "models": [
          {
            "id": "cursor-agent",
            "name": "Cursor Agent (Auto)",
            "api": "openai-completions",
            "reasoning": false,
            "input": [
              "text"
            ],
            "cost": {
              "input": 0,
              "output": 0,
              "cacheRead": 0,
              "cacheWrite": 0
            },
            "contextWindow": 200000,
            "maxTokens": 8192
          },
          {
            "id": "claude-opus-4.6",
            "name": "Claude 4.6 Opus",
            "api": "openai-completions",
            "reasoning": false,
            "input": [
              "text"
            ],
            "cost": {
              "input": 0,
              "output": 0,
              "cacheRead": 0,
              "cacheWrite": 0
            },
            "contextWindow": 200000,
            "maxTokens": 8192
          },
          {
            "id": "claude-sonnet-4.6",
            "name": "Claude 4.6 Sonnet",
            "api": "openai-completions",
            "reasoning": false,
            "input": [
              "text"
            ],
            "cost": {
              "input": 0,
              "output": 0,
              "cacheRead": 0,
              "cacheWrite": 0
            },
            "contextWindow": 200000,
            "maxTokens": 8192
          },
          {
            "id": "gpt-5.4",
            "name": "GPT-5.4",
            "api": "openai-completions",
            "reasoning": false,
            "input": [
              "text"
            ],
            "cost": {
              "input": 0,
              "output": 0,
              "cacheRead": 0,
              "cacheWrite": 0
            },
            "contextWindow": 200000,
            "maxTokens": 8192
          },
          {
            "id": "gemini-3.1-pro",
            "name": "Gemini 3.1 Pro",
            "api": "openai-completions",
            "reasoning": false,
            "input": [
              "text"
            ],
            "cost": {
              "input": 0,
              "output": 0,
              "cacheRead": 0,
              "cacheWrite": 0
            },
            "contextWindow": 200000,
            "maxTokens": 8192
          }
        ]
      }
    }
  }
}
```

OpenClaw 侧若需分段输出，可配置（具体以 OpenClaw 文档为准）：

```
agents.defaults.blockStreamingDefault = "on"
agents.defaults.blockStreamingBreak = "text_end"
```

更细的块流参数示例：

```
"agents": {
  "defaults": {
      "blockStreamingDefault": "on",
      "blockStreamingBreak": "text_end",
      "blockStreamingChunk": {
        "minChars": 100,
        "maxChars": 3800,
        "breakPreference": "paragraph"
      },
      "blockStreamingCoalesce": {
        "minChars": 1,
        "maxChars": 3800,
        "idleMs": 6000
      }
  }
}
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `blockStreamingChunk.minChars` | `800` | 低于此字数不投递，继续等 |
| `blockStreamingChunk.maxChars` | `1200` | 超过此字数强制截断（无论有没有 `\n\n`） |
| `blockStreamingChunk.breakPreference` | — | 优先切割位置：`paragraph`→`newline`→`sentence`→`whitespace`→强切 |
| `blockStreamingCoalesce.idleMs` | — | 空闲多少毫秒后把小块合并发出 |
| `channels.tt.textChunkLimit` | — | TT 通道独立硬上限（覆盖 maxChars） |

`breakPreference` 的优先级降级链（chunker 从高到低尝试，找不到则降到下一级）：

| 优先级 | 名称 | 实际匹配的分隔符 |
|---|---|---|
| 1 | `paragraph` | `\n\n`（空行，即段落边界） |
| 2 | `newline` | `\n`（单个换行） |
| 3 | `sentence` | `。` `！` `？` `.` `!` `?` 后跟空格或换行 |
| 4 | `whitespace` | 空格 ` `（单词边界） |
| 5 | 强切（fallback） | 直接在 `maxChars` 位置硬截断，不管在哪 |

### Cursor CLI 全局配置（非本仓库）

配置文件路径（Windows）：`%USERPROFILE%\.cursor\cli-config.json`

```json
{
  "approvalMode": "unrestricted"
}
```

- `approvalMode`：例如 `unrestricted` 表示允许执行相关命令策略（以 Cursor 官方说明为准）。

### 修改tt的回复表情
```
"channels": {
    "tt": {
      "enabled": true,
      "reactionExpr": "[好的]"
    }
  }
```