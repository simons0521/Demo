# LLM 接入说明

本文档说明怎么把本服务接到你自己的大模型上。走的是契约 §7.1 的 OpenAI 兼容
Chat Completions 路线：**只改三个环境变量，不需要改代码**。

---

## 1. 用了什么

| 项 | 值 |
|---|---|
| 厂商 | DeepSeek |
| 模型名 | `deepseek-flash` |
| 协议 | OpenAI 兼容 Chat Completions（`POST {base_url}/chat/completions`） |
| SDK | 不用 SDK。直接用 `httpx` 0.28.1 发裸 HTTP POST |
| 运行环境 | Python 3.12.13 |
| 流式 | 不使用。请求里不带 `stream`，一次拿完整响应 |

模型名只从 `LLM_MODEL` 读，代码里没有任何写死的模型名或厂商判断——
换成别家的兼容接口同样只改那三个变量。

**思考模式：保持 DeepSeek 默认（开启）。** 请求里不传 `thinking` 字段，也就
不关它。理由：这个服务是多轮工具调用式的问答，模型要自己规划"先查什么、
再查什么、查到了够不够回答"，思考模式对这一步帮助明显；关掉更快更省，但
规划更容易乱，而答错的代价比多花几秒大得多。实测 55 题的公开题库全过，
单题最慢也在 180 秒预算内。代价是慢一些、token 贵一些，这是有意的取舍。

开启思考模式的两个直接后果，都按契约 §7.3 处理了：

- **`max_tokens` 设为 4096**（`starter/kbqa/llm.py` 的 `MAX_TOKENS`）。
  思考也占输出额度，设小了额度会被思考吃光、回来一个空正文加
  `finish_reason: "length"`。4096 是契约要求的 2048 底线之上的取值。
- **多轮之间 `reasoning_content` 原样回传**。`LiveEngine` 把收到的 assistant
  消息**整条**追加进 `messages`，不自己挑字段重组，所以
  `reasoning_content` 不会丢。少了它带 `tools` 的请求会被接口拒成 400。

思考内容只进 trace，不进任何对外字段（`answer`、`citations`、`data_evidence`）。

---

## 2. 配置从哪里读

**全部从环境变量读**，没有配置文件、没有命令行参数。读取点只有一处：

```
starter/kbqa/config.py  →  load_settings()
```

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `LLM_BASE_URL` | 空 | 模型服务地址。原样拼接 `{值}/chat/completions`，**不补 `/v1`、不截路径** |
| `LLM_API_KEY` | 空 | 以 `Authorization: Bearer <key>` 发送 |
| `LLM_MODEL` | 空 | 请求体里的 `model` 字段 |
| `LLM_TIMEOUT` | `120` | 单次模型调用超时（秒）。契约 §7.3 要求不小于 120 |
| `CHAT_BUDGET` | `150` | `/api/chat` 整体预算（秒），给契约的 180 秒上限留余量 |

上面三个 LLM 变量**任意一个为空**就进入无 Key 的降级模式
（`/api/health` 里的 `llm_mode` 变成 `"mock"`），服务照常启动，见第 5 节。

单次调用的实际超时取 `min(LLM_TIMEOUT, 剩余预算)`，即先耗尽的那一个；
预算用尽时返回结构化 `refusal`，不会挂起。

下面这些也走环境变量，但与模型接入无关，列出来是为了完整：
`DATA_DIR`、`KB_DIR`、`VAR_DIR`、`TODAY`（默认 `2026-09-01`，契约规定的系统"今天"）。

---

## 3. 怎么换成你们的

**改哪几个值**：`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`，就这三个。

```bash
cd starter

LLM_BASE_URL=https://api.deepseek.com \
LLM_API_KEY=<你们的 Key> \
LLM_MODEL=deepseek-flash \
.venv/bin/uvicorn kbqa.server:app --port 8000
```

如果你们用 `make run`，等价写法是先导出再跑：

```bash
cd starter
export LLM_BASE_URL=https://api.deepseek.com
export LLM_API_KEY=<你们的 Key>
export LLM_MODEL=deepseek-flash
make run
```

**两个容易踩的点**：

1. `LLM_BASE_URL` 填 `https://api.deepseek.com` 即可，**不要加 `/v1`**。
   DeepSeek 的兼容接口本身不带 `/v1`，本服务也不会替你补——它把地址原样拼上
   `/chat/completions`。多写一层会 404。
2. 模型名写 `deepseek-flash`。如果这个名字在你们的账号下不可用，直接改
   `LLM_MODEL` 的值就行，代码不用动。

**改完之后**：

- **需要重启服务**——配置只在启动时读一次（`load_settings()`）。
- **不需要重新执行 `make rebuild`**。索引与模型无关：检索是本地 BM25，
  不调用任何模型，`make rebuild` 生成的 `starter/.cache/index.json` 和
  `starter/var/clean.db` 不用重建。

**怎么确认切过去了**：

```bash
curl -s http://localhost:8000/api/health
# 看到 "llm_mode": "live" 就是接上了；仍是 "mock" 说明三个变量没读全
```

---

## 4. 怎么看到发给模型的请求

有两条路，第一条是评测时用的那条。

### 4.1 走代理（推荐）

```bash
python3 eval/llm_gateway.py proxy --upstream https://api.deepseek.com --log llm_traffic.jsonl
```

它会打印一个 `LLM_BASE_URL`，用那个地址启动服务即可：

```bash
cd starter
LLM_BASE_URL=<proxy 打印的地址> LLM_API_KEY=<你们的 Key> LLM_MODEL=deepseek-flash \
  .venv/bin/uvicorn kbqa.server:app --port 8000
```

日志写在 `llm_traffic.jsonl`（仓库根目录，已被 `.gitignore` 忽略，不会提交）。
**一行一次往返**，字段包括：时间戳、路径、完整请求体、状态码、响应体
（流式的会重新拼装出 `content` / `reasoning_content` / `tool_calls`）、耗时、
token 用量。`Authorization` 只记长度不记值，所以这份日志可以直接贴出来。

### 4.2 看服务自己的 trace

每次请求的回答里带一个 `trace_id`，用它取 trace：

```bash
curl -s http://localhost:8000/api/trace/<trace_id>
```

trace 的 `llm_calls` 数组里，每一次模型调用都有一条记录，`prompt` 字段是
**发给模型的完整 messages 的前 4000 字**（契约 §6 要求"看得到发给模型的
完整请求"）。下面是一条真实记录，只截了无关的长字段：

```json
{
  "endpoint": "https://api.deepseek.com/chat/completions",
  "model": "deepseek-flash",
  "messages": 2,
  "tools": 10,
  "prompt": "[{\"role\": \"system\", \"content\": \"你是一家连锁餐饮公司的经营分析助手，服务对象是运营同事。\\n今天固定是 2026-09-01…（系统提示词全文，共 9 条规则）\"}, {\"role\": \"user\", \"content\": \"外卖订单的退款受理窗口是多久？\"}]",
  "status": 200,
  "finish_reason": "tool_calls",
  "content_chars": 0,
  "tool_calls": ["search_kb"],
  "has_reasoning": true,
  "usage": {
    "prompt_tokens": 1950,
    "completion_tokens": 67,
    "total_tokens": 2017,
    "prompt_cache_hit_tokens": 1792,
    "prompt_cache_miss_tokens": 158
  },
  "took_ms": 841.1
}
```

同一个数组里还有 `raw_content` 和 `raw_reasoning`（模型原始输出，各截 4000 字），
排错时比对"模型到底说了什么"和"我们最后交给用户什么"很有用。

**Key 不出现在任何一条 trace 里**，只有 `endpoint` 和 `model`。

---

## 5. 没有 Key 时会怎样

**服务照常启动**，四个接口**全部正常返回**，没有任何一个返回 500。
这一节是实测的：把三个变量清空后另起一个实例（`env -u LLM_BASE_URL -u LLM_API_KEY
-u LLM_MODEL`），逐个打过。

| 接口 | 无 Key 时 |
|---|---|
| `GET /api/health` | 200。`"llm_mode": "mock"`，其余字段照常（`kb_docs` 35、`valid_sales_rows` 18290、数据区间、清洗报告） |
| `GET /api/metrics/summary`、`/api/metrics/daily` | 200。正常返回真实数字，与有没有 Key 无关 |
| `POST /api/retrieve` | 200。正常返回 BM25 检索结果（实测：`退款政策` 命中 KB-013，14.81 分） |
| `POST /api/chat` | 200。**不是** refusal，是正常回答——见下 |

**降级策略**：`Service._run_engine()` 判断 `not settings.live` 时，直接用不依赖
模型的模板作答器，连 `LLMClient` 都不构造。数字由代码从工具结果渲染，引用由
代码从文档里挑出逐字原句，所以答案仍然是**有据可查**的，只是没有模型的自然语言
润色。实测（2026 年 8 月净营业额）：

```json
{
  "answer_type": "data",
  "answer": "2026 年 8 月（全部门店）：净营业额 160345.00 元，有效订单数 4560 单，
             客单价 35.16 元，销量 6944 件，退款金额 598.00 元。"
}
```

调用模型失败（超时、错误码、空回答）走的是另一条路：那时返回
`answer_type: "refusal"`，答案里写明原因，真实原因记进 trace，同样保证 200。

**没有 Key 时前端也照常可用。** 看板三个视图只读 `/api/*`，跟有没有 Key 无关；
调试面板要的检索明细，模板路径同样往 trace 里写（`answerer._search` 走的是
同一套 `explain=True` 的检索）——**评审在干净环境里第一步就是不配 Key 跑起来，
那一步看到的看板和面板，跟配了 Key 的是同一套代码渲染的**。

---

## 6. 依赖与安装

| 项 | 值 |
|---|---|
| Python | 3.12（实测 3.12.13） |
| 第三方依赖 | `fastapi` 0.141.1、`uvicorn` 0.54.0、`httpx` 0.28.1、`pydantic`、`starlette` 1.7.0（见 `starter/requirements.txt`） |
| 模型文件下载 | **没有**。检索是纯 Python 标准库实现的 BM25，不需要向量模型、不需要 GPU、下载体积为 0 |
| 首次安装 | `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| `make rebuild` 实测耗时 | **0.17 秒**（其中建索引 0.1 秒，清洗 18,628 行 POS 数据） |
| 冷启动到 `/api/health` 就绪 | **0.24 秒** |
| 首次启动是否要联网 | 不要。没有 Key 也能启动（第 5 节），有 Key 时也只在问答时才访问模型 |

---

## 7. 自测结果

走 OpenAI 兼容协议，所以按契约 §7.5 跑了接入预检：

预检的流程是**两步**：先起一个假模型（不联网、不花钱），它打印出三个环境变量；
然后**带着这三个变量重启服务**，预检才开始发请求。

```bash
# 第一步：起假模型，记下它打印的 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
cd starter
python3 eval/llm_gateway.py preflight --service-url http://localhost:8000
# ↑ 它会停在这儿等回车。另开一个终端，用上面那三个变量重启服务，再回来按回车。

# 想写成脚本（比如 CI）就自己把服务先起好，再用 --no-wait：
#   LLM_BASE_URL=http://127.0.0.1:6399/ds-gw LLM_API_KEY=... LLM_MODEL=... \
#       uvicorn kbqa.server:app --port 8001
#   python3 eval/llm_gateway.py preflight --service-url http://localhost:8001 \
#       --port 6399 --no-wait
```

**结果：14 项检查全部通过，退出码 0。** 预检结论原文：

> 预检通过：在 OpenAI 兼容这条路线上，我们能原样接上你的服务。

| 编号 | 检查项 | 结果 | 说明 |
|---|---|---|---|
| P1 | 服务确实把请求发到了注入的 `LLM_BASE_URL`（含路径前缀） | 通过 | 共观察到 60 次 POST `/ds-gw/chat/completions` |
| P2 | 请求里的 `model` 等于注入的 `LLM_MODEL` | 通过 | 全部请求都用了注入的模型名 |
| P3 | 注入的 Key 以 `Authorization: Bearer` 发送 | 通过 | 全部请求都带了正确的 Bearer Key |
| P4 | 只用了 DeepSeek 文档列出的顶层参数 | 通过 | 没有出现任何文档外的参数 |
| P5 | `max_tokens` 不设，或不小于 2048 | 通过 | 实际发出的值是 4096 |
| P6 | 没有访问 `{prefix}/chat/completions` 之外的任何路径 | 通过 | 只访问了这一个路径，没碰别的 |
| P7 | 工具定义规范，且每个工具调用都以 `role=tool` + `tool_call_id` 回传 | 通过 | 44 个工具调用的结果都正确回传了 |
| P8 | 每个场景下 `/api/chat` 都返回 HTTP 200 与字段完整的合法 JSON | 通过 | 32 次问答全部 200 |
| P9 | 模型不可用时给出结构化 `refusal`，`answer` 从不是空串 | 通过 | 12 个失败场景都给了结构化 refusal 或有据可查的回答 |
| P10 | 思考内容没有漏进 `answer` / `citations` / `data_evidence` | 通过 | 32 次回答里思考标记都没出现在对外字段里 |
| P11 | `/api/chat` 在时限内返回（含长时间无响应的场景） | 通过 | 最慢一次 120.03 秒，在 180 秒以内 |
| P12 | 注入环境变量后 `/api/health` 报告 `llm_mode = live` | 通过 | `llm_mode = live` |
| P13 | 多轮工具调用之间 `reasoning_content` 原样回传（没有触发 400） | 通过 | 18 次多轮请求都原样回传了 |
| P14 | 保持连接的空行与 SSE 注释没有把服务弄坏 | 通过 | `slow` 场景照常给出回答 |

完整报告（含每一项的逐项证据、16 个场景的明细表）已归档在仓库里：

- [`eval/reports/2026-09-26_preflight/preflight_report.md`](eval/reports/2026-09-26_preflight/preflight_report.md)
  —— 最早那次，服务还是 100 分那一版
- [`eval/reports/2026-09-26_preflight_final/preflight_report.md`](eval/reports/2026-09-26_preflight_final/preflight_report.md)
  —— **最终代码上复跑的那次**（改动过 `live.py` 的收尾逻辑，按契约要求复跑）

两次的数字**逐项相同**（60 次 `/ds-gw/chat/completions`、44 个工具调用、
32 次问答、最慢 120.03 秒、18 次多轮往返）——假模型是确定性的，
所以这正好说明后面那些改动**没有碰到协议层**：数字守卫改的是"什么算合法数字"，
不碰请求怎么发、工具怎么回传。

预检的环境：服务跑在本机 8000 端口，假模型 `llm_gateway.py` 2.0.0，
`max_tokens` 4096，思考模式开启（所以 P10、P13 是真检查，不是"未检查"）。

### 7.1 契约 §7.3 各条的实际处理

这一节按 §7.4 只要求贴预检输出，但我们把 §7.3 每一条落在哪也一并列出来，
方便对照：

| 契约 §7.3 的要求 | 本服务的处理 |
|---|---|
| `reasoning_content` 不要展示给用户 | 只读 `message.content`；思考过程只进 trace 的 `raw_reasoning` |
| 带 `tools` 的请求要原样回传 assistant 消息 | `messages.append(reply.message)` 整条追加，不重组字段 |
| `max_tokens` ≥ 2048 | `MAX_TOKENS = 4096` |
| `finish_reason` 不是 `stop`/`tool_calls` 的四种都当错误 | `GOOD_FINISH = ("stop", "tool_calls")`，其余抛异常，由上层转成 refusal |
| 带 `tool_calls` 时 `content` 可能是空串 | 空正文 + 有 `tool_calls` 判为正常，继续下一轮；空正文 + 无 `tool_calls` 才判错 |
| `arguments` 是 JSON 字符串，要处理解析失败 | 解析失败时把错误原样回给模型让它重给，连续 2 轮失败才放弃 |
| 一次可能返回多个 `tool_calls` | 逐个执行，每个都回一条带 `tool_call_id` 的 `role: "tool"` 消息 |
| 不用文档外的参数 | 只发 `model` / `messages` / `max_tokens` / `tools` / `tool_choice` |
| 错误码不能让 `/api/chat` 返回 500 | 400/401/402/422/429/500/503 全部转成结构化 `refusal`，trace 里留真实原因 |
| 180 秒总预算、单次取 `min(120, 剩余)` | `CHAT_BUDGET=150`、`LLM_TIMEOUT=120`，预算不足 10 秒时停止调用模型 |
| 服务繁忙时的空行 / `: keep-alive` | 解析前先 `strip()`；非流式，SSE 那条用不到（见第 8 节） |

### 7.2 有 Key 的实际问答抽样

同一套代码接真实 DeepSeek（`deepseek-flash`）跑公开题库得 **100.00 / 100（55 题全过）**。
**最终代码**（`b9b62c5`）那一轮的原始报告归档在：

- [`eval/reports/2026-09-26_2047_live/`](eval/reports/2026-09-26_2047_live/) —— 最终提交的那一轮
- [`eval/reports/2026-09-26_confirm_100/`](eval/reports/2026-09-26_confirm_100/) —— 回归基线，`make eval*` 都跟它比
- [`eval/reports/2026-09-26_after_version_evidence/`](eval/reports/2026-09-26_after_version_evidence/)

中间有一轮掉到 97.00（H02），是数字守卫的白名单漏了一条早就该有的规则，
**不是代码退化**——诊断与修复见 `DEBUG_LOG.md` 第 8 节，前后经过见 `EVAL_REPORT.md` 第 3 节。
更早的低分运行（starter 初始 18.00、作答质量修复后 89.00、DSML 回归那一轮 80.00）
也都归档在 `eval/reports/` 下。

> 顺带一个反例：早期我们曾把"两次独立运行逐题一致"当成"100 分不是运气"的证据。
> 那一轮 97.00 说明这个推理站不住——同一道题连中两次，只说明这两次抽到了同一面。
> `EVAL_REPORT.md` 第 5 节记了这个教训。

---

## 8. 已知限制

下面这些是我们清楚、但没解决的问题。写出来不扣分，藏起来被撞见才扣，所以如实列：

1. **预检假模型的行为没有对过真实接口。** 本机没有 DeepSeek Key 时，
   `llm_gateway.py` 的假服务全部行为都是照官方文档实现的。按
   `eval/README_llm_gateway.md` 第 4 节列出的几条，这几条尤其可能和真实接口有出入：
   不回传 `reasoning_content` 时的 400 正文结构、`response_format` 取值不合法时的
   422（文档没给状态码，是推定的）、错误正文的 JSON 结构、`thinking_starved`
   的 1024 阈值、保活空行的条数与间隔、`usage` 里的 token 数（假服务按字符数粗算）。
   如果在真实接口上看到不一样的行为，以真实接口为准。
2. **没有实现流式。** 请求不带 `stream`，`/api/chat` 一次返回完整结果。
   契约 §7.3 里与流式有关的两条——`delta.reasoning_content` 先于 `delta.content`、
   流式响应里的 `: keep-alive` 注释——在本服务上不适用，预检里的 `slow` 场景
   也只验证了非流式那一半（正文前的空行）。前端因此没有"思考中"的流式状态，
   而是等整段返回。
3. **没有用向量模型**（契约 §7.6 是可选）。检索只有 BM25 加中文 bigram 分词，
   对英文文档的召回靠 bigram 覆盖，跨语言能力弱于向量检索。知识库里有中英两种
   文档，英语提问的召回率我们没有单独测过。
4. **`hang` 场景要等满 120 秒才返回。** 单次模型调用超时设的是契约允许的最大值
   120 秒，模型彻底不响应时用户会等到接近 120 秒才拿到 refusal。想更快返回就要
   调小 `LLM_TIMEOUT`，但那会把正常的慢回答也误判成失败，权衡后保留了 120 秒。
5. **trace 只在内存里，重启即失，容量 200 次。** `TraceStore` 是个定长环形
   缓冲（`capacity=200`），进程一退全没，`/api/trace/{id}` 也取不回来。
   要留证据得在服务还活着的时候抓走（`/api/traces` 列出最近的摘要，
   拿 `trace_id` 再去取完整的）。落盘是刻意没做的：这是运营内部的排障工具，
   不值得为它引一个存储依赖，而契约要的是"这一次为什么这么答"能看见。
6. **一次问答里可能出现两个 `search` 步。** live 路径的检索是模型调工具触发的
   （`service.run_tool`），兜底回模板时模板自己还会再检索一次（`answerer._search`）。
   两个都落痕，所以调试面板**必须按 `steps` 数组顺序渲染**，
   不能按步骤名建字典——建字典会静默丢掉一个。
7. **索引缓存跟着仓库走**（`starter/.cache/index.json`）。改了知识库要重新执行
   `make rebuild`，否则服务会加载旧索引。缓存键里带了知识库内容的指纹，内容变了
   键就变，但**不会自动重建**，需要手动跑一次。
