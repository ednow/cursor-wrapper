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

## 环境变量

- `CURSOR_BIN`：Cursor CLI 可执行文件，默认 `agent`
- `CURSOR_WORKSPACE`：CLI 执行时使用的工作区目录，默认当前目录
- `WRAPPER_API_KEY`：包装层 Bearer Token，可选；设置后会校验 `Authorization` 请求头
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

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
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

## 已知限制

- 当前仅兼容 `chat.completions`
- 当前不支持 `tools/function calling`
- 当前不支持真正的 Cursor 会话续接
- `messages` 会被压平成单个提示词再发给 Cursor CLI
