# DEBUG_LOG

接手 `starter/` 后逐个缺陷的排查记录。
每个缺陷六项：现象 / 假设 / 验证 / 根因 / 修复 / 回归测试。

`starter/HANDOVER.md` 里写着"测试全部通过、检索命中率 95%、md/txt/html 三种格式都支持"。
这三句话**没有一句是真的**，下面每一节都会给出反证。
排查顺序按作业的提示走：缺陷是分层的，修掉一层才看得见下一层。

---

## 0. 基线

在动任何代码之前先固化一份可对照的起点。

| 项 | 值 |
|---|---|
| 运行命令 | `python3 eval/run_eval.py --base-url http://127.0.0.1:8000 --questions eval/public_questions.jsonl` |
| 代码 commit | `56f7a1f`（作业包原始状态，未改动） |
| 模型与配置 | DeepSeek `deepseek-flash`，`LLM_BASE_URL=https://api.deepseek.com`，`max_tokens=4096`，思考模式默认开启 |
| 是否配置 Key | 是（live 模式） |
| 总分 | **18.00 / 100.00** |
| 存档 | `eval/reports/2026-09-26_baseline_live.json` / `.md` |

`/api/health` 快照：

```json
{"kb_docs": 36, "kb_chunks": 53, "valid_sales_rows": 18628, "llm_mode": "live"}
```

契约要求的是 `kb_docs: 35`、`valid_sales_rows: 18290`（评测题 N01 的实际期望值）。

失败检查项的分布（`report.json` 统计）：

| 检查项 | 次数 | 指向 |
|---|---|---|
| `metrics.expect.*` | 18 | 清洗与指标口径 |
| `retrieval.gold_all` / `gold_any` | 9 | 检索基础设施 |
| `doc.cite_all` / `answer_type_in` | 13 | 意图路由 |
| `data.numbers_all` / `evidence_required` | 12 | 指标口径 |
| `health.expect.*` | 2 | 健康检查字段 |
| 其余（hybrid / version / multi_turn / refusal / safety） | 46 | 以上缺陷的连带 |

### 缺陷索引

按依赖顺序排列，编号即排查顺序。

| # | 层 | 位置 | 一句话 |
|---|---|---|---|
| 1 | 口径 | `cleaning.py:77-102` | KB-001 的规范化与六条剔除一条都没实现 |
| 2 | 口径 | `tools.py:52-61` | 日期区间用了半开区间，丢掉区间最后一天 |
| 3 | 口径 | `tools.py:96-108` | 退款行被排除、退款金额硬编码 0、订单数用明细行数、客单价分母错 |
| 4 | 口径 | `tools.py:122-151` | `daily_metrics` 与 `summary` 口径不一致 |
| 5 | 检索 | `tokenizer.py:20-22` | 中文按空白切词，整句变成一个 token，BM25 失效 |
| 6 | 检索 | `loader.py:12` | 只收 `.md`，`.txt` / `.html` 不进索引 |
| 7 | 检索 | `loader.py:82-84` | 只按 UTF-8 解码，GBK 导出的旧文件变乱码 |
| 8 | 检索 | `chunker.py:37-63` | 定长死切、丢尾块、无结构化切块 |
| 9 | 重建 | `index.py:23-27` | 缓存键不含知识库内容，换库不失效 |
| 10 | 重建 | `config.py:49-51` | 索引缓存提交进了 git，评审方永远读到旧索引 |
| 11 | 路由 | `planner.py:253-258` | "多少/多久/几" 无条件覆盖政策路由 |
| 12 | 会话 | `sessions.py:21-28` | `session_id` 被忽略，全局共享一份历史 |
| 13 | 检索 | `retriever.py:240,306` | 先取 top-k 再过滤，违反契约 §4 |
| 14 | 作答 | `answerer.py:28` | 上下文被截断到 200 字 |
| 15 | 可观测 | `service.py:170-174` | 裸 except 吞异常，trace 与日志里没有真实原因 |
| 16 | 契约 | `service.py:70` | `kb_docs` 数的是文件数，不是文档数 |
| 17 | 安全 | `tools.py:74-79` | `run_sql` 允许执行写操作 SQL |
| 18 | 健康 | `service.py:87-89` | `data_period` 取到脏日期，为 `""` 与 `"N/A"` |

---

## 1. 清洗流水线形同虚设

### 现象

第一关的指标接口全线偏。
`GET /api/metrics/summary?start=2026-06-01&end=2026-06-30` 返回：

```json
{"net_revenue": 152883.0, "refund_amount": 0.0, "orders": 4272, "aov": 35.79, "qty": 6360}
```

而题库 M01 期望 `net_revenue: 156757.00`、`refund_amount: 953.00`、`orders: 4311`、`aov: 36.36`、`qty: 6496`。
六个指标接口题 M01–M06，只有区间内真的没有数据的 M05 通过。

同时 `/api/health` 的 `valid_sales_rows` 是 **18628**，而 `data/sales.csv` 去掉表头正好也是 18628 行。
`/api/data_quality` 的清洗台账六项全是 0：

```json
{"raw_rows": 18628, "removed": {"1_unparseable_date": 0, "2_empty_amount": 0, "3_qty_le_zero": 0,
 "4_store_not_in_stores": 0, "5_product_not_in_products": 0, "6_duplicate_row": 0},
 "kept_rows": 18628}
```

一条都没剔掉，说明清洗步骤根本没执行。

另一个旁证：`/api/health` 的 `data_period` 是 `{"start": "", "end": "N/A"}`。
`data_period` 取的是 `MIN(date)` / `MAX(date)`，能取到空串和 `N/A`，说明库里有这两个值当日期。

### 假设

原始数据里的脏值没有被识别。`cleaning.py` 里定义了 `REMOVAL_REASONS` 六项，但从没被写入过。
先猜三种可能，逐个排除：

- **猜 A：清洗跑了但数据是干净的。** 排除 —— 原始 `sales` 表里确实有 225 行非 `YYYY-MM-DD` 的日期、45 行 `store_id` 不在 `stores` 表里。
- **猜 B：清洗跑了但阈值写错，把可恢复的写法当成脏数据扔了。** 排除 —— 台账是全 0，不是"扔错了"，是**一行都没扔**。
- **猜 C：`clean_rows()` 里的剔除逻辑被整段删掉了**，只剩一个"照抄"循环。← 验证后确认。

### 验证

```bash
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('data/pos.db')
print('rows', c.execute('SELECT COUNT(*) FROM sales').fetchone()[0])
print('非 ISO 日期', c.execute(\"SELECT COUNT(*) FROM sales WHERE date NOT LIKE '____-__-__'\").fetchone()[0])
print('脏 store_id', c.execute('SELECT COUNT(*) FROM sales WHERE store_id NOT IN (SELECT store_id FROM stores)').fetchone()[0])
"
# 18628 / 225 / 45
```

数据确实脏，而清洗台账全 0，两者矛盾。

再看代码，`clean_rows()`（`kbqa/cleaning.py:77-102`）的主循环是：

```python
cents, status = parse_amount(row["amount"])
if status != "ok":
    cents = 0                       # ← 解析失败置 0，而 KB-001 §3.2 要求直接剔除
qty = parse_qty(row["qty"]) or 0     # ← 解析失败置 0，没有 qty<=0 的剔除
kept.append((...))                   # ← 无条件 append，日期、外键、重复行一律不查
```

`REMOVAL_REASONS` 这个元组在 `cleaning.py:15-22` 定义，全文再无引用。
`CleaningReport.removed` 从初始化就是全 0，直到序列化都没人写过。

`store_id` / `product_id` 也没有做 `strip().upper()` 规范化（KB-001 §2.1），
所以 `s01` 这种写法即使想校验外键也校验不了。

为了确认正确的口径长什么样，我按 KB-001 §2/§3 手写了一遍完整实现，跑出来：

```
removed {'date': 8, 'amt': 150, 'qty': 0, 'store': 10, 'prod': 40, 'dup': 100}  kept 18290
M01 want 156757/953/4311/6496/36.36 -> 全部命中
M02 want 41740/107/875/1395/47.7    -> 全部命中
M03 want 11024/16/461/689/23.91     -> 全部命中
M04 want 3625/0/53/125/68.4         -> 全部命中
M05 want 0/0/0/0/None               -> 全部命中
valid_sales_rows want 18290         -> 18290（与题库 N01 期望一致）
```

五个指标题全部**精确命中**（连容差 0 的 `orders`、`qty` 都对得上），
说明这份口径理解没有偏差，可以照它来修。

顺带确认了一个版本陷阱：KB-001 §6 写明 v3 相对 v2 改了三处，
其中"`amount` 为空的行改为直接剔除，v2 是按 `qty × unit_price` 回填后继续参与统计"。
所以要**剔除**，不能回填。数据里正好有 150 行空 `amount`。

另外 `DD-MM-YYYY` 的方向也有数据可自证：KB-001 §2.2 说"日在前、月在后"，
并提示"这一类里会出现'日'大于 12 的样本"。原始数据里有 `30-07-2026`，
按日在前解析成 2026-07-30 合法；按月在前会得到非法日期。方向确认。

### 根因

`kbqa/cleaning.py:77-102` `clean_rows()` —— KB-001 §3 的六条剔除与 §2 的四条规范化全部缺失。

具体到行：

- `cleaning.py:84-85`：`parse_amount` 返回非 `ok` 时置 `cents = 0` 而不是剔除该行。
- `cleaning.py:86`：`parse_qty(...) or 0`，没有 `qty <= 0` 的剔除，也没有对解析失败的处理。
- `cleaning.py:87-98`：无条件 `kept.append(...)`；`store_id` / `product_id` 未 `strip().upper()`，未做维表外键校验，未做去重。
- `cleaning.py:15-22`：`REMOVAL_REASONS` 定义了却无任何写入点，`report.removed` 恒为全 0。

### 修复

- commit：（见下）

### 回归测试

- 测试名：（见下）

---

<!-- 后续缺陷按同样格式追加 -->
