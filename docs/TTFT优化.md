## TTFT优化

| 字段 | 含义 |
|------|------|
| `spawn_elapsed_s` | 仅 `create_subprocess_exec`  await 时长（本地起句柄）。 |
| `since_popen_first_stdout_read_s` | 子进程已返回后，到**第一次**从 stdout 读到数据：偏「子进程/运行时冷启动 + 初始化到开始写管道」。 |
| `since_popen_s`（首条 ndjson 行） | 到首条完整 NDJSON：通常与上一项接近，除非首 read 未含换行。 |
| `since_first_ndjson_line_s`（thinking / assistant 首条） | **首条 NDJSON 行之后**到首段「思考/正文」：更贴近「管线已通之后」的**远端/模型首 token**（若先有 `system/init` 再 thinking，这段会吃掉「init 完成 → 模型开始吐字」）。 |

## 分析
下面按**文件末尾这次请求**（`completion_id=chatcmpl-1033f3524b4c4815992590c380a4f53e`，约 16:11:01→16:11:58）解读；依据是你刚加上的 `cursor_cli` INFO 行。

## 关键 INFO 行（原文要点）

- **16:11:01,889**：子进程已起，`spawn_elapsed_s=0.179`（建进程 ~0.18s）。  
- **16:11:22,099**：首条 NDJSON，`event_type='system' subtype='init'`，`since_popen_first_stdout_read_s=20.210`，`read_to_first_line_ms=0.0`。  
- **16:11:30,780**：首条 **assistant 流式正文**（7 字符 `Reading`），`since_popen_s=28.891`，`since_first_ndjson_line_s=8.681`。

## 耗时怎么读

| 阶段 | 时长 | 含义 |
|------|------|------|
| **建子进程** | **~0.18s** | `spawn_elapsed_s`；PowerShell/agent 拉起句柄，不是大头。 |
| **popen 后 → 第一次 stdout 有数据** | **~20.21s** | `since_popen_first_stdout_read_s` 与 `since_popen_s` 相同，且 `read_to_first_line_ms=0`：第一次 `read()` 就带回**一整条**首行 NDJSON。这段时间子进程在跑、但管道里一直没字节；典型对应 **Node/Electron（或 CLI）冷启动 + 读 profile/登录/MCP/仓内文件 + 等到能打出第一条 `system/init`**，wrapper 只能记成「等首包」。 |
| **首行 NDJSON → 首段 assistant 正文** | **~8.68s** | `since_first_ndjson_line_s=8.681`：init 行已出之后，到**第一条可转发给前端的 assistant 文本**；更贴近 **「管线已通 → 首轮可见输出」**（仍可能含编排/工具准备，不全是纯模型 TTFT）。 |
| **首段 assistant 相对 HTTP stream 起点** | **~29.1s** | 约 `0.18 + 20.21 + 8.68` 量级（相对 16:11:01.889 的 popen 时刻到 16:11:30.780）。 |

## 结论（一句话）

这次请求里，**约 20s 耗在「子进程已起来 →  stdout 出现第一条 `system/init`」**（本地冷启动 + CLI 初始化 + 首包前的后端/编排）；**再约 8.7s 耗在「首条 init 之后 → 第一段 assistant 正文」**。  
本次日志里**没有**出现 `first thinking text delta` 的 INFO，说明**先到达的是 assistant 流**（或 thinking 无文本 delta），首段可见正文就是 `Reading`。

整条 completion 从 **16:11:01,704** `stream started` 到 **16:11:58,156** `stream done`，墙钟大约 **56s**，后面大块是工具读大文件、`grep`、`Get-Location` 和多轮输出，属于**交互执行**，与上面「首包 / 首 token」是不同阶段。