# Cursor CLI OpenAI Wrapper

将本机的 `Cursor CLI` 包装成一个兼容 `OpenAI Chat Completions API` 的 HTTP 服务，便于直接复用现有 `OpenAI SDK`、`curl` 或第三方客户端。

## 当前能力

- `POST /v1/chat/completions`
- `GET /v1/models`
- `GET /healthz`
- 支持普通响应和 `stream=true` 的 SSE 流式输出

## 前置条件

1. 本机已安装 `Cursor Agent CLI`
2. 本机已完成 `agent login`
3. Python 3.10+

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

- `CURSOR_BIN`：Cursor CLI 可执行文件，默认 `agent`
- `CURSOR_WORKSPACE`：CLI 执行时使用的工作区目录，默认当前目录
- `WRAPPER_API_KEY`：包装层 Bearer Token，可选；设置后会校验 `Authorization` 请求头
- `CURSOR_API_KEY`：Cursor 官方 API Key，可选；设置后会注入到 `cursor-agent` 子进程环境变量，
  用于在没有执行过 `agent login` 的环境（如服务器、CI）下完成 Cursor 后端鉴权
- `DEFAULT_MODEL`：默认对外模型名，默认 `cursor-agent`
- `MODEL_ALIASES`：模型别名映射，格式如 `gpt-4o=claude-4-sonnet,gpt-4.1=gpt-5`

内置默认别名：

- `cursor-agent -> auto`
- `CURSOR_TRUST`：是否自动加 `--trust`，默认开启
- `CURSOR_APPROVE_MCPS`：是否自动加 `--approve-mcps`
- `CURSOR_FORCE`：是否自动加 `--force`
- `CURSOR_SANDBOX`：可选，传给 `--sandbox`

你也可以直接在 `app/config.py` 顶部的 `CONFIG_*` 变量里填写配置。
优先级规则是：

- `CONFIG_*` 有值：优先使用文件内配置
- `CONFIG_*` 为空字符串：回退使用对应环境变量

## Cursor API Key 的获取与使用

### 获取

`cursor.com/dashboard` → Integrations → API Keys → Create API Key，复制形如
`key_xxxxxxxxxxxxxxxx` 的值。仅生成时可见，请立即保存。

### 启动时注入（推荐）

无需执行 `agent login`，直接把 Key 通过环境变量传给 wrapper，wrapper 会在调用
`cursor-agent` 时把它注入到子进程环境。

PowerShell 临时启动：

```powershell
$env:CURSOR_API_KEY = "key_xxxxxxxxxxxxxxxx"
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

永久写入用户环境变量：

```powershell
setx CURSOR_API_KEY "key_xxxxxxxxxxxxxxxx"
```

或者直接写到 `app/config.py` 顶部（优先级高于环境变量）：

```python
CONFIG_CURSOR_API_KEY = "key_xxxxxxxxxxxxxxxx"
```

### 自检

服务启动后访问 `GET /healthz`，返回里 `cursor_cli.cursor_api_key_configured`
为 `true` 表示 wrapper 检测到了 Key（值不会被打印出来）。


## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动response回放服务
```bash
uvicorn app.mock_startup_main:app --host 0.0.0.0  --port 8000
```


如果你还没有安装独立的 `agent` 命令，可以先在 Windows PowerShell 中执行：

```powershell
irm 'https://cursor.com/install?win32=true' | iex
agent --version
agent login
```

服务的 `GET /healthz` 会返回当前 `CURSOR_BIN` 是否可用；如果未安装或未配置，会显示 `status: degraded`。

`GET /v1/models` 会实时调用 `agent models`，返回当前 Cursor Agent CLI 实际可用的模型列表。

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

当请求中设置 `stream=true` 时，服务会调用：

```bash
agent -p "<prompt>" --output-format stream-json --stream-partial-output
```

随后把 Cursor CLI 的 NDJSON 事件转换成 OpenAI 风格的 SSE `chat.completion.chunk` 数据帧。

## 测试

```bash
pytest
```

## 配置到openclaw中

```json
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
```


### 增加分段输出
agents.defaults.blockStreamingDefault = "on"
agents.defaults.blockStreamingBreak = "text_end"

### 块段输出配置
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
        "minChars": 100,
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

这是 `breakPreference` 的优先级降级链，chunker 会从最高优先级开始找，找到就切，找不到才降到下一个：

| 优先级 | 名称 | 实际匹配的分隔符 |
|---|---|---|
| 1 | `paragraph` | `\n\n`（空行，即段落边界） |
| 2 | `newline` | `\n`（单个换行） |
| 3 | `sentence` | `。` `！` `？` `.` `!` `?` 后跟空格或换行 |
| 4 | `whitespace` | 空格 ` `（单词边界） |
| 5 | 强切（fallback） | 直接在 `maxChars` 位置硬截断，不管在哪 |

## cursor cli的配置
配置文件地址：`%USERPROFILE%\.cursor\cli-config.json`

```json
{
  "approvalMode": "unrestricted",

}

```

- `approvalMode`:允许执行所有命令

## 已知限制

- 当前仅兼容 `chat.completions`
- [x] 当前不支持 `tools/function calling`
- 当前不支持真正的 Cursor 会话续接
- `messages` 会被压平成单个提示词再发给 Cursor CLI

## TTFT优化

| 字段 | 含义 |
|------|------|
| `spawn_elapsed_s` | 仅 `create_subprocess_exec`  await 时长（本地起句柄）。 |
| `since_popen_first_stdout_read_s` | 子进程已返回后，到**第一次**从 stdout 读到数据：偏「子进程/运行时冷启动 + 初始化到开始写管道」。 |
| `since_popen_s`（首条 ndjson 行） | 到首条完整 NDJSON：通常与上一项接近，除非首 read 未含换行。 |
| `since_first_ndjson_line_s`（thinking / assistant 首条） | **首条 NDJSON 行之后**到首段「思考/正文」：更贴近「管线已通之后」的**远端/模型首 token**（若先有 `system/init` 再 thinking，这段会吃掉「init 完成 → 模型开始吐字」）。 |