# 经营看板 + 问答服务

运营内部用的问答服务：一条线查销售数据库，一条线查公司知识库。
Python 3.12，只用 `fastapi` / `uvicorn` / `httpx`，测试用 `pytest`。

## 跑起来

```bash
make setup      # uv venv --python 3.12 + 装依赖
make rebuild    # 重建清洗表与检索索引
make run        # 起服务，默认 http://127.0.0.1:8000
make test       # 跑测试（173 条）
```

评测对着**已经起好的服务**跑，三个目标按花不花钱分：

```bash
make eval-free  # 检索 / 指标 / 健康检查三类，22 分，不调模型，免费，随时可跑
make eval-mock  # 完整 55 题，自己起一个没配 Key 的服务来跑，免费
make eval      # 完整 55 题，对着 live 服务跑，要 Key、花钱，人工触发
make eval-mine  # 自己出的那 8 道题
```

四个都跑完自动跟回归基线（`eval/reports/2026-09-26_confirm_100`）比一次，
**掉分则退出码非零**——「没伤到以前的分数」是条能跑的命令，不是一句感觉。

换一套数据或知识库：

```bash
make rebuild DATA_DIR=/path/to/data KB_DIR=/path/to/knowledge_base
```

`DATA_DIR`、`KB_DIR`、`VAR_DIR` 也可以直接作为环境变量传给 `make run`。

## 目录

| 文件 | 干什么的 |
|---|---|
| `kbqa/config.py` | 环境变量与路径；“今天”固定 2026-09-01 |
| `kbqa/cleaning.py` | 把原始 `sales` 导进 `var/clean.db` |
| `kbqa/tools.py` | 指标查询：汇总、按天、支付方式、商品排行、门店、品类、对比、单价 |
| `kbqa/loader.py` | 读知识库文件，认出 doc_id、标题、生效日期 |
| `kbqa/chunker.py` | 切块 |
| `kbqa/tokenizer.py` | 分词 |
| `kbqa/index.py` | BM25 索引 + 磁盘缓存 |
| `kbqa/retriever.py` | 检索与元数据过滤 |
| `kbqa/docfacts.py` / `units.py` | 从文档里挑句子、出引用 |
| `kbqa/planner.py` / `entities.py` / `timeparse.py` | 意图、实体、时间 |
| `kbqa/answerer.py` / `hybrid.py` / `render.py` | 组装回答 |
| `kbqa/llm.py` / `live.py` / `toolspec.py` | 模型客户端与工具回路 |
| `kbqa/service.py` / `server.py` | 编排与 HTTP 层 |
| `kbqa/trace.py` | 一次问答的落痕（纯内存，容量 200，重启即失） |
| `web/` | 零构建前端：`index.html` / `app.js` / `style.css` |
| `tests/` | 173 条测试 |

## 接口

契约里点名的七个，加三个只读的（契约 §1 允许在此之外增加接口，这三个都是加法）：

| 方法 | 路径 | 干什么 |
|---|---|---|
| GET | `/api/health` | 健康检查。`llm_mode` 告诉你是 live 还是 mock |
| GET | `/api/metrics/summary` | 区间汇总：净营业额 / 退款 / 订单数 / 销量 / 客单价 |
| GET | `/api/metrics/daily` | 按天，缺的天补零 |
| POST | `/api/retrieve` | 检索片段，恰好 `top_k` 条（不够才少给） |
| POST | `/api/chat` | 问答，同一 `session_id` 视为同一段对话 |
| GET | `/api/trace/{trace_id}` | 一次问答的完整过程 |
| GET | `/api/data_quality` | 清洗报告，外加剔除原因的中文标签 |
| GET | `/api/stores` | 门店列表（看板的门店下拉用，**不写死在前端**） |
| GET | `/api/metrics/top_products` | 商品排行（看板 Top 10 用） |
| GET | `/api/traces` | 最近几次问答的**摘要**（只给 id 和元信息，不给完整步骤） |

## 前端

`GET /` 是一个零构建的原生前端，三个视图：#/dashboard（看板）、#/chat（对话框）、
#/debug/&lt;trace_id&gt;（调试面板）。没有 CDN、没有构建步骤、没有新依赖——
`web/` 三个文件直接由 `StaticFiles` 托管，`web/` 不存在时服务照常启动，只是不挂前端。

## 两种模式

配了 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 就走模型，没配就走本地模板回答。
没有 Key 时服务照常启动，`/api/chat` 不会 500，**看板与调试面板也照常可用**
（模板路径同样往 trace 里写检索明细，调试面板不依赖有没有 Key）。

模型给的数字对不上工具结果的，整段换成按工具结果渲染的模板回答——这是
`LiveEngine._finalise()` 的兜底，判据见 `tests/test_number_guard.py`。

交接说明见 `HANDOVER.md`，模型接入见 `../LLM_SETUP.md`。
