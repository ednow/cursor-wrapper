# 流式卡死排查

## 已知现象

- 调用工具卡死（例如全盘 `glob`、E 盘 `Get-ChildItem -Recurse`、MCP `everything_search`）时，客户端长时间无新输出。
- 旧版日志可能只有一条 `responses stream keepalive` 后长期静默；需结合 **stream_observe** 观测区分卡在 SSE、queue 还是 CLI。

## 空闲超时（已实现）

**不是**整条请求的总时长上限，而是：连续 **X 秒** 未向客户端写出任何 SSE（`data:` 正文或 keepalive 注释）则 **terminate agent** 并推送终止文案。

- **keepalive 注释**（`: cursor-wrapper keepalive ...`）**也算**推送，发出后会**重置**空闲计时（与客户端「N 秒无任何下行」对齐时，需上游把 keepalive 视为活动）。
- CLI 持续输出 chunk 时，每次 `yield` 正文同样会重置计时。

| 配置 / 环境变量 | 默认 | 含义 |
|-----------------|------|------|
| `CONFIG_CURSOR_STREAM_IDLE_SECONDS` / `CURSOR_STREAM_IDLE_SECONDS` | `120` | 空闲上限（秒）；`<=0` 关闭 |
| `CONFIG_CURSOR_STREAM_MAX_SECONDS` / `CURSOR_STREAM_MAX_SECONDS` | （兼容旧名） | 与上同义，未配 IDLE 时回退读此项 |
| `CONFIG_CURSOR_STREAM_UPSTREAM_TIMEOUT_MARGIN` / `CURSOR_STREAM_UPSTREAM_TIMEOUT_MARGIN` | `10` | 上游超时头解析值再减去的秒数 |

**实际上限**（INFO 日志 `stream idle_seconds resolved`）：

```text
effective = min(本机空闲配置, 上游请求头超时 − margin)
```

上游超时从下列请求头**按顺序**取第一个可解析的值（秒，支持 `60` / `60s` / `60000ms`）：

`x-request-timeout`、`x-read-timeout`、`x-stream-timeout`、`x-timeout`、`x-client-read-timeout`、`x-forwarded-timeout`、`x-forwarded-read-timeout`、`x-proxy-read-timeout` 等（见 `app/stream_timeout.py` 中 `UPSTREAM_TIMEOUT_HEADER_NAMES`）。

示例：本机 `45`，上游头 `X-Read-Timeout: 60` → `upstream_cap=50` → **effective=45**。  
本机 `120`，上游 `60` → **effective=50**（比上游 60s 断连早 10s，便于推送终止文案）。

上游可见文案（SSE 正文 delta）：

```text
耗时过长，agent 已被终止。
```

日志关键字：`stream content-idle timeout`。

---

## 前置：确认观测代码已加载

重启 uvicorn 后，在 `logs/wrapper-YYYY-MM-DD.log` 中应能看到：

| 检查项 | 期望值 |
|--------|--------|
| 启动行 | `cursor-wrapper started build=stream-observe-20260516` |
| chunk 行号 | `main.py` 中带 `responses stream chunk` 的行号随版本变化（新构建应含 `max_seconds=`） |
| 观测关键字 | 能搜到 `observe resp_` 或 `keepalive emit` |

若仍只有 `main.py:492` 且无 `observe`，说明进程未加载新代码，下文速查表**不适用**。

相关环境变量（可选）：

| 变量 | 默认 | 含义 |
|------|------|------|
| `CURSOR_STREAM_KEEPALIVE_SECONDS` | `12` | queue 空闲多久发 SSE keepalive 注释 |
| `CURSOR_CLI_STDOUT_READ_IDLE_LOG_SECONDS` | `30` | CLI stdout 多久无数据打 INFO（`0` 关闭） |
| `CURSOR_STREAM_IDLE_SECONDS` | `120` | 空闲上限（秒） |
| `WRAPPER_LOG_LEVEL` / `CONFIG_WRAPPER_LOG_LEVEL` | `info` | 设为 `debug` 可看 `sse_delta emit`、`producer put` |

---

## 卡死时按关键字查日志（速查表）

定位某次请求时，先用响应 ID 过滤（`resp_xxx` 或 `chatcmpl-xxx`）：

```text
resp_fbd0f799cfdf4b85b85b14e16823d7f5
```

### 关键字 → 含义 → 结论

| 搜什么 | 级别 | 含义 | 若卡死后… |
|--------|------|------|-----------|
| `cursor-wrapper started build=stream-observe` | INFO | 服务已加载观测构建 | 没有 → 先重启 uvicorn |
| `responses stream started` / `stream started` | DEBUG | 流式请求开始（含 `max_seconds`） | 没有 → 请求未进 wrapper |
| `stream content-idle timeout` | WARNING | 空闲超时，已 kill agent 并收尾 | 出现 → 连续 X 秒无 SSE 正文 |
| `observe resp_` + `producer put kind=delta` | DEBUG | CLI 仍在往 queue 推文本 | **持续出现** → CLI 有输出，主循环在消费 |
| `observe resp_` + `producer put kind=eof` | DEBUG | CLI 流结束 | 出现后应有 `stream done` / `responses stream done` |
| `cli stdout read still waiting` | INFO | 子进程 stdout 长时间无字节（工具/MCP 未返回） | **周期性出现** → **卡在 CLI 侧等待工具** |
| `wait_queue idle timeout` | INFO | 12s 内 queue 无新 delta | 出现 → wrapper 在等业务数据，非 SSE 写出 |
| `keepalive emit begin` | INFO | 即将 `yield` SSE keepalive 注释 | 有 begin **无** end → **卡在写给客户端（背压/断连）** |
| `keepalive emit end` | INFO | keepalive 已写出 | 每 ~12s 一对 begin/end → HTTP 连接仍活着，在等 CLI |
| `sse_delta emit begin` | DEBUG | 即将 `yield` 正文 chunk | 最后一条只有 begin **无** `sse_delta emit end` → **卡在某个 chunk 的 SSE 写出** |
| `sse_delta emit end` | DEBUG | 该 chunk 已写出 | 与 begin 成对则 SSE 层正常 |
| `responses stream chunk` / `stream chunk` | DEBUG | 传统 chunk 日志（含 body 预览） | 与 observe 对照 |
| `🔧 调用工具`（chunk body 内） | DEBUG | Agent 发起工具（glob/shell/MCP 等） | 最后一条工具 hint 后长期无上表日志 → 结合 `stdout read still waiting` |
| `generator finally` | INFO | 流式生成器结束（完成/取消/异常） | 卡死且无 finally → 生成器仍挂起 |
| `responses stream done` / `stream done` | DEBUG | 正常结束 | 有 finally 无 done → 可能在收尾 yield 处卡住 |
| `responses stream error` / `stream error` | ERROR | 业务错误结束 | — |
| `cursor CLI terminate_agent` | WARNING | 主动终止 agent 子进程 | 与墙钟超时或 cancel 相关 |

### 三种典型结论（对照最后几条日志）

| 最后出现的观测 | 最可能卡在哪里 |
|----------------|----------------|
| `sse_delta emit begin n=N`（无对应 `end`） | **SSE → 客户端**（不读流、代理缓冲、连接半开） |
| `keepalive emit begin`（无 `end`） | **SSE keepalive 写出**（同上） |
| `sse_delta emit end` 或 `keepalive emit end` 之后，仅有 `cli stdout read still waiting` | **Cursor CLI / 工具**（长命令、全盘搜索、MCP） |
| `wait_queue idle timeout` + `keepalive emit end` 循环，无新 chunk | **CLI 无 NDJSON**，连接正常 |
| `wait_queue begin` 后 `generator finally`（`prod_done=False`） | **调用方先断连**（如上游 60s 超时），CLI 可能仍在跑 |
| 什么都没有（含无 `stdout read still waiting`） | **事件循环阻塞**或**旧进程无观测** |

---

## 实测案例

### `resp_fbd0f799cfdf4b85b85b14e16823d7f5`（2026-05-16，stream_observe 已启用）

**现象**：在 E 盘搜索 `momo*` 时客户端长时间无新输出。

**时间线摘要**：

| 时间 | 事件 |
|------|------|
| 01:32:08 | `responses stream started`，`max_seconds` 未打（当时构建尚无墙钟配置） |
| 01:32:20 | 唯一一对 `keepalive emit`（CLI 启动前 12s） |
| 01:32:20～01:33:04 | chunk 1～110，`sse_delta emit begin/end` **均成对** → SSE 正常 |
| 01:33:04 | chunk 110：`shellToolCall` — **Full E: recurse for momo\*** |
| 01:33:04.043 | `wait_queue begin wait #112`，`prod_done=False` → **等 CLI / 工具** |
| 01:33:08.720 | `generator finally`，`elapsed_s=60.0`，`prod_done=False` → **上游约 60s 断连**，非 wrapper 墙钟 kill |

**结论**：

- **不是** SSE `yield` 卡死（n=110 有完整 begin/end）。
- **是** CLI 在执行 E 盘递归 shell 时长时间无 NDJSON；wrapper 停在 **`wait_queue`**。
- 本次由 **调用方 ~60s 超时断开** 收尾，非 agent 被 wrapper kill；若启用默认 **120s** `CURSOR_STREAM_MAX_SECONDS`，wrapper 会在 2 分钟时 terminate 并推送「耗时过长，agent 已被终止。」

### `resp_7e86daae2fec40ceb8f6d9d235119f3e`（2026-05-15，旧代码）

- 一条 keepalive 后全静默，`main.py:492`，无 observe。
- 疑：第二次 keepalive 的 SSE `yield` 阻塞，或 glob 用户目录后 CLI 挂起。

### `resp_37476bdaa7a842b39b668ec2cdb49c73`（2026-05-16，旧代码）

- chunk 刷到 n=95 后停，`main.py:492`，无 observe。

---

## 建议操作顺序

1. 确认启动行含 `stream-observe-20260516`，且 `responses stream started` 带 `max_seconds=`。
2. 用 `response_id` 过滤，看**最后 20 行**带 `observe` / `keepalive` / `stdout read` / `wall-clock timeout` 的记录。
3. 对照上表「三种典型结论」与实测案例。
4. 任务管理器看是否仍有 `agent` 子进程；有则倾向 CLI/工具未结束或未 kill。
5. 需要更细粒度时：`WRAPPER_LOG_LEVEL=debug` 重启，复现后搜 `sse_delta emit` 与 `producer put`。
6. 长工具任务：调大 `CURSOR_STREAM_MAX_SECONDS`，或约束 Agent 勿全盘递归。
