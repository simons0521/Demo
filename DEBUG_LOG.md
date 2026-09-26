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

| # | 层 | 位置 | 一句话 | 状态 |
|---|---|---|---|---|
| 1 | 口径 | `cleaning.py:77-102` | KB-001 的规范化与六条剔除一条都没实现 | ✅ 已修 `d09fc9b` |
| 2 | 口径 | `tools.py:52-61` | 日期区间用了半开区间，丢掉区间最后一天 | ✅ 已修 `e3b00ca` |
| 3 | 口径 | `tools.py:96-108` | 退款行被排除、退款金额硬编码 0、订单数用明细行数、客单价分母错 | ✅ 已修 `e3b00ca` |
| 4 | 口径 | `tools.py:122-151` | `daily_metrics` 与 `summary` 口径不一致 | ✅ 已修 `e3b00ca` |
| 5 | 检索 | `tokenizer.py:20-22` | 中文按空白切词，整句变成一个 token，BM25 失效 | ✅ 已修 `4b89860` |
| 6 | 检索 | `loader.py:12` | 只收 `.md`，`.txt` / `.html` 不进索引 | ✅ 已修 `4b89860` |
| 7 | 检索 | `loader.py:82-84` | 只按 UTF-8 解码，GBK 导出的旧文件变乱码 | ✅ 已修 `4b89860` |
| 8 | 检索 | `chunker.py:37-63` | 定长死切、丢尾块、无结构化切块 | ✅ 已修 `8becfd7` |
| 9 | 重建 | `index.py:23-27` | 缓存键不含知识库内容，换库不失效 | ✅ 已修 `8becfd7` |
| 10 | 重建 | `config.py:49-51` | 索引缓存提交进了 git，评审方永远读到旧索引 | ✅ 已修 `8becfd7` |
| 11 | 路由 | `planner.py:253-258` | "多少/多久/几" 无条件覆盖政策路由 |
| 12 | 会话 | `sessions.py:21-28` | `session_id` 被忽略，全局共享一份历史 |
| 13 | 检索 | `retriever.py:240,276,306` | 结果张冠李戴、先取 top-k 再过滤、补齐不守每篇一格 | ✅ 已修 `69af4ce` |
| 14 | 作答 | `answerer.py:28` | 上下文被截断到 200 字 |
| 15 | 可观测 | `service.py:170-174` | 裸 except 吞异常，trace 与日志里没有真实原因 |
| 16 | 契约 | `service.py:70` | `kb_docs` 数的是文件数，不是文档数 |
| 17 | 安全 | `tools.py:74-79` | `run_sql` 允许执行写操作 SQL |
| 18 | 健康 | `service.py:87-89` | `data_period` 取到脏日期，为 `""` 与 `"N/A"` |
| 19 | 测试 | `tests/conftest.py:21-51` | 夹具在类上直接打桩且不还原，污染同进程的其它测试 | ✅ 已修 `89523a4` |
| 20 | 作答 | `llm.py` / `hybrid.py` | 模型工具调用不收敛时整题答不出来（H05、S01） |

**接手中没有提到、排查中新发现的**：#19（测试基建污染）、
#20（工具调用不收敛）。#10 除了"提交进 git"，还叠了一层——那份缓存
**本身就是陈旧的**（25 篇 / 53 块，对不上实际的 35 篇），
即提交的是个连原作者代码都跑不出来的文件。

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

修完 `clean_rows()` 之后，从清洗表直接按 KB-001 §4 的口径算一遍指标题：

```
removed {'1_unparseable_date': 8, '2_empty_amount': 150, '3_qty_le_zero': 30,
         '4_store_not_in_stores': 10, '5_product_not_in_products': 40,
         '6_duplicate_row': 100, 'note_unparseable_amount': 0}
kept_rows 18290   sales 18196   refund 94
M01 want 156757/953/4311/6496/36.36 -> 全部命中
M02 want 41740/107/875/1395/47.7    -> 全部命中
M03 want 11024/16/461/689/23.91     -> 全部命中
M04 want 3625/0/53/125/68.4         -> 全部命中
M05 want 0/0/0/0/None               -> 全部命中
valid_sales_rows want 18290         -> 18290（与题库 N01 期望一致）
```

五个指标题共 25 项断言全部**精确命中**（连零容差的 `orders`、`qty` 都对得上），
说明这份口径理解没有偏差。

这里有个容易踩的点：`kept_rows` 是 18290，而 `kept_sales_rows`（不含退款行）是 18196。
`valid_sales_rows` 要的是**前者**——它是"清洗后还剩多少行"的健康指标，退款行也算数。
一开始我按后者去对，差了 94，是 94 行退款行的量。

另外两条数据可以自证，不用猜：

- **版本陷阱**：KB-001 §6 写明 v3 相对 v2 改了三处，其中"`amount` 为空的行改为直接剔除，
  v2 是按 `qty × unit_price` 回填后继续参与统计"。所以要**剔除**，不能回填。
  数据里正好有 150 行空 `amount`。
- **`DD-MM-YYYY` 的方向**：KB-001 §2.2 说"日在前、月在后"，并提示"这一类里会出现
  '日'大于 12 的样本"。原始数据里有 `30-07-2026`，按日在前解析成 2026-07-30 合法；
  按月在前会得到非法日期。方向确认。

### 根因

`kbqa/cleaning.py:77-102` `clean_rows()` —— KB-001 §3 的六条剔除与 §2 的四条规范化全部缺失。

具体到行：

- `cleaning.py:84-85`：`parse_amount` 返回非 `ok` 时置 `cents = 0` 而不是剔除该行。
- `cleaning.py:86`：`parse_qty(...) or 0`，没有 `qty <= 0` 的剔除，也没有对解析失败的处理。
- `cleaning.py:87-98`：无条件 `kept.append(...)`；`store_id` / `product_id` 未 `strip().upper()`，未做维表外键校验，未做去重。
- `cleaning.py:15-22`：`REMOVAL_REASONS` 定义了却无任何写入点，`report.removed` 恒为全 0。

### 修复

commit `d09fc9b`，改 `starter/kbqa/cleaning.py`：

- 新增 `parse_date()`：收 `YYYY-MM-DD` / `YYYY/M/D` / `DD-MM-YYYY` 三种格式，
  返回 ISO；都认不出来返回 `None`。`%d-%m-%Y` 放在最后试，
  所以 ISO 与斜杠格式不会被它抢走（`2026-06-01` 用 `%d-%m-%Y` 解析会
  把 2026 当成"日"而失败），方向不会反。
- 新增 `normalise_code()`：`strip().upper()`。**必须发生在判外键之前**，
  否则 `s01` 会被当成不存在的门店误删，这正是 KB-001 §7.2 提醒的那个坑。
- 重写 `clean_rows()`：按 §3 的顺序执行六条剔除。用一串 `continue` 而不是
  一次性判六条，保证一行同时踩两条规则时只记在前一条头上，
  各条计数之和才等于剔除总数。
  - 规则 2 里，`amount` 为空算 `2_empty_amount`；非空但解析不出来的
    （`"abc"` 之类）同样剔除，但另记一笔 `note_unparseable_amount`——
    不能悄悄当 0 元记成一笔收入。真实数据里这一类是 0 行。
  - 规则 6 的签名是七个字段**规范化之后**的元组。共用订单号、只差商品的多行订单
    签名不同，自然留下（§7.3）。
  - `is_refund` 由 `cents < 0` 判定，退款行保留。
- `build_clean_db()`：维表跟着 §2.1 一起规范化，清洗表里的外键才真的能对上；
  规范化后若撞号保留先出现的那个。

顺带修好了缺陷 #18：`data_period` 之前取到 `""` 和 `"N/A"`，
现在脏日期在清洗阶段就被剔掉，`MIN/MAX` 拿到的是纯 ISO 日期。

### 回归测试

`starter/tests/test_cleaning.py`，11 个用例，commit `1c23d51`（红）→ `d09fc9b`（绿）：

| 用例 | 压的是 KB-001 哪一条 |
|---|---|
| `test_amount_strips_currency_symbol` | §2.3 `¥38.00` 与 `38.00` 同额 |
| `test_empty_amount_is_a_removal_not_a_fill` | §3.2 空金额剔除、不回填 |
| `test_ids_are_normalised_before_foreign_key_check` | §2.1 + §7.2 规范化先于判外键 |
| `test_dates_are_normalised_to_iso` | §2.2 三种格式，`DD-MM-YYYY` 日在前 |
| `test_each_removal_rule_has_its_own_count` | §3 六条各自计数 |
| `test_removal_reasons_are_fully_accounted` | §3 台账要对得上账 |
| `test_rules_run_in_order` | §3 "按顺序执行"，一行只记一条 |
| `test_multi_line_orders_are_kept` | §3.6 + §7.3 多行订单不是重复行 |
| `test_refund_rows_are_kept_and_flagged` | §4 退款行保留并标 `is_refund` |
| `test_report_shape_covers_every_reason` | 台账键覆盖全部原因 |
| `test_real_dataset_is_actually_cleaned` | 真实数据的不变量 |

前十个用一份 21 行的合成明细（`_ROWS`）压各条规则，**不写死真实数据集的数字**——
评审会换 `data/`。最后一个在真实数据上只断言与数据无关的不变量：
日期全是 ISO、外键全合法、`qty` 全为正、清洗确实剔掉了东西。

红→绿的证据：

```
# commit 1c23d51（修复前）
7 failed, 4 passed in 0.10s
# commit d09fc9b（修复后）
11 passed in 0.12s
```

写测试时我把夹具的算术数错了（以为 20 行、保留 8 行），第一次跑出来
`kept_rows=9` 而断言写的是 8。六条剔除的计数当时已经全部命中，
错的只是我自己数错了行数，改的是测试不是实现。

---

## 2. 指标口径：三处同源缺陷

清洗修好之后指标仍然全线偏，说明问题不止一层。缺陷 #2 / #3 / #4 是同一类，
都在 `tools.py`，一次改完。

### 现象

`GET /api/metrics/summary?start=2026-06-01&end=2026-06-30`：

```json
{"net_revenue": 153131.0, "refund_amount": 0.0, "orders": 4243, "aov": 36.09, "qty": 6334}
```

题库 M01 期望 `156757.00` / `953.00` / `4311` / `36.36` / `6496`。
五个数没有一个对，但都"差不多"——不是崩溃，是系统性偏差，最难发现的那种。

### 假设

- **猜 A：清洗还是不对。** 排除 —— 清洗台账与 N01 的 `valid_sales_rows` 已经对上了，
  换 `tools.py` 一处不改、直接用 SQL 按 §4 算一遍，五个数全中。
  所以数据是对的，**算的人不对**。
- **猜 B：只是"漏了退款"一处。** 排除 —— 补上退款后净营业额是 156757 + 953 = 157710，
  比期望**多了** 953；说明还有一处反向的偏差在抵消它。
- **猜 C：日期区间是半开区间，丢掉了最后一天。** ← 方向对了，顺着查下去三条都出来了。

### 验证

把旧实现的输出逐个拆开，看每一个数是怎么算出来的：

```bash
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('var/clean.db')
print('6 月销售行净额  ', c.execute(\"SELECT SUM(amount_cents)/100.0 FROM sales_clean\"
      \" WHERE date>='2026-06-01' AND date<='2026-06-30' AND is_refund=0\").fetchone()[0])
print('6/30 销售行净额 ', c.execute(\"SELECT SUM(amount_cents)/100.0 FROM sales_clean\"
      \" WHERE date='2026-06-30' AND is_refund=0\").fetchone()[0])
print('6 月明细行数    ', c.execute(\"SELECT COUNT(*) FROM sales_clean\"
      \" WHERE date>='2026-06-01' AND date<='2026-06-30' AND is_refund=0\").fetchone()[0])
print('6/30 明细行数   ', c.execute(\"SELECT COUNT(*) FROM sales_clean\"
      \" WHERE date='2026-06-30' AND is_refund=0\").fetchone()[0])
"
# 157710.0 / 4579.0 / 4380 / 137
```

三个错值全部能精确复现：

| 指标 | 旧实现算出 | 拆解 |
|---|---|---|
| `net_revenue` | 153131.0 | 157710（6 月销售行）− 4579（丢掉 6/30）= 153131 ✅ |
| `orders` | 4243 | 4380（销售明细行数）− 137（丢掉 6/30）= 4243 ✅ |
| `qty` | 6334 | 截至 6/29 的销售 `qty` 之和，既不冲退款也不含 6/30 ✅ |

对上账就说明没有第四处偏差了。三处分别是：

1. **半开区间**（`_where`）：`date >= ? AND date < ?`，整天丢掉区间最后一天。
2. **退款被当成"不是营业"排掉**（`query_metrics` 的 `WHERE ... AND is_refund = 0`）。
   而 §4 的**净营业额就是销售行加退款行**，退款是负的。排掉等于把营业额算高。
   这条与第 1 条方向相反，两条叠在一起互相抵消，`net_revenue` 只差 2.3%，
   看起来像"精度问题"而不是"口径错误"——这是它藏得住的原因。
3. **订单数用明细行数**（`COUNT(*)`）：一张单点两个菜就算两单，客单价被算低。

顺带确认 `refund_amount` 是硬编码的 `0`（`tools.py:100`），
`qty` 也只有 `SUM(qty)` 没冲退款。`daily_metrics` 的 `COUNT(DISTINCT ...)` 和
`SUM(amount_cents)` 其实是对的，它只是被 `_where` 连累，以及和 `summary` 口径不一致。

### 根因

`kbqa/tools.py`：

- `tools.py:53` `clause = ["date >= ?", "date < ?"]` —— 契约 §4 要求闭区间。
- `tools.py:97-107` `query_metrics()` 的四个基数全错：
  - `WHERE ... AND is_refund = 0` 排掉退款行；
  - `refund_cents` 用字面量 `0` 占位；
  - `COUNT(*)` 数明细行而不是 `COUNT(DISTINCT order_id)`；
  - `SUM(qty)` 没冲减退款。
- `tools.py:109` 客单价的分母跟着错成了明细行数。

`payment_mix` / `top_products` / `by_store` 的 `is_refund`、`DISTINCT`、`qty`
语义本来是对的，只被 `_where` 连累。

### 修复

commit `e3b00ca`，改 `starter/kbqa/tools.py`：

- `_where()`：`date < ?` → `date <= ?`。
- `query_metrics()`：四条口径按 §4 重写。一个查询里取完四个基数
  （净额、退款、去重订单数、净销量），避免分两次查时区间跨午夜对不上——
  这个服务"今天"是固定的，但真实上线时不是。

  ```sql
  SELECT COALESCE(SUM(amount_cents), 0),                    -- 销售 + 退款 = 净额
         COALESCE(-SUM(CASE WHEN is_refund = 1
                            THEN amount_cents END), 0),        -- 取绝对值
         COUNT(DISTINCT CASE WHEN is_refund = 0
                             THEN order_id END),               -- 去重，退款不参与
         COALESCE(SUM(CASE WHEN is_refund = 0
                           THEN qty ELSE -qty END), 0)         -- 销量冲减退款
  FROM sales_clean WHERE <闭区间>
  ```

改的过程中自己踩了一次坑：SQL 里已经 `-SUM(...)` 取过绝对值了，
返回值那行还留着原来的 `yuan(-refund_cents)`，重复取负，`refund_amount` 变成了负数。
是测试里 `assert refund_amount >= 0` 抓出来的。

### 回归测试

`starter/tests/test_metrics.py`，13 个用例，commit `16c8065`（红）→ `e3b00ca`（绿）：

| 用例 | 压的是哪一条 |
|---|---|
| `test_end_date_is_inclusive` | 契约 §4 右端闭 |
| `test_start_date_is_inclusive` | 契约 §4 左端闭 |
| `test_out_of_range_is_excluded` | 区间外不能漏进来 |
| `test_net_revenue_includes_refunds` | §4 净营业额 = 销售 + 退款 |
| `test_orders_counts_distinct_orders_not_rows` | §4 订单数去重 |
| `test_qty_nets_out_refunds` | §4 销量冲退款 |
| `test_aov_uses_distinct_orders_as_denominator` | §4 客单价分母 |
| `test_summary_reports_all_metric_fields` | 契约 §4 五个字段齐全 |
| `test_empty_range_is_zeros_not_nulls` | 空区间是 0 与 `null`，不是缺字段 |
| `test_store_and_product_filters` | §2.1 过滤编号先规范化 |
| `test_daily_covers_every_day_including_empty_ones` | 契约 §4 每天都要有记录 |
| `test_daily_uses_the_same_definitions_as_summary` | 日与汇总口径一致 |
| `test_real_dataset_metrics_are_consistent` | 真实数据的自洽不变量 |

夹具是一张手搭的清洗表（不走 `build_clean_db`），指标层是独立的一层，
测试不该被清洗层的行为左右。

写夹具时踩了自己的第二个坑：一开始只放**一张**多行订单，
`test_orders_counts_distinct_orders_not_rows` 竟然通过了——
半开区间恰好把最后一天排除掉，剩下的 3 行明细正好等于 3 张单，
`COUNT(*)` 与 `COUNT(DISTINCT ...)` 撞在一起，缺陷被掩盖。
补上第二张多行订单（D1）后明细 5 行、订单 4 张，才真的红起来。
**一个不会红的测试比没有测试更危险**，它给人已经验过的错觉。

红→绿的证据：

```
# commit 16c8065（修复前）
10 failed, 3 passed in 0.15s
# commit e3b00ca（修复后）
13 passed in 0.15s
```

真实接口上的最终验收（M01–M06 六道指标题，25 项断言）：

```
M01 156757.00 / 953.00 / 4311 / 36.36 / 6496
M02  41740.00 / 107.00 /  875 / 47.70 / 1395
M03  11024.00 /  16.00 /  461 / 23.91 /  689
M04   3625.00 /   0.00 /   53 / 68.40 /  125
M05      0.00 /   0.00 /    0 /  null /    0
M06 5 天，6/8–6/11 全 0，6/12 为 998.00 / 27 / 36.96
```

全部精确命中，含零容差的 `orders` 与 `qty`。

---

## 3. 检索基础设施：分词、加载、切块、缓存（缺陷 5–10）

前两层是"算错数"，这一层是"找不到资料"。`HANDOVER.md` 说"检索命中率 95%"，
`/api/retrieve` 的实测是 **6/15**。

### 现象

`/api/health` 报 `kb_chunks: 53`，而 `knowledge_base/` 里明明有 35 篇文档。
`eval/public_questions.jsonl` 的 15 道检索题里 9 道找不到 gold 文档。

### 假设

检索是"切词 → 建索引 → 打分 → 取 top-k"一条链，任何一环坏掉都会表现为"找不到"。
按链路从下游往上排：切词、加载、切块、缓存。

### 验证

**分词**（缺陷 5）：

```
tokenize("外卖订单多久内可以申请退款？")  →  ["外卖订单多久内可以申请退款"]   # 一个 token
```

原实现是 `normalise(text).split()`。中文没有空格，**一整句话切成一个 token**，
TF 恒为 1、IDF 只比"整句出现过没有"，BM25 退化成字符串匹配。
问句和正文的整句不一样，就永远命中不了。

佐证这份代码本来是照二元组写的：`docfacts.py:73` 的注释写着"含虚字的二元组
（'在充''么开'）多半是切词残渣"，`:84` 按 `len(term) == 2` 给权重打折，
`tokenizer.content_tokens` 按 `all(char in STOP_CHARS for char in token)`
过滤——`len == 2`、`all(...)` 这两条在按空白切词下都不会生效。

**加载**（缺陷 6、7）：

```
knowledge_base/ 下 36 个文件 → 索引里 32 篇
```

契约 §0 明写"编号在文件名开头，**与格式无关**"，但 `SUPPORTED_SUFFIXES = {".md", ".markdown"}`，
`.txt`（旧 OA 导出、英文邮件）与 `.html`（内部 FAQ）整类被丢掉。
KB-011 / KB-013 / KB-025–029 全在这两类里——召回不到的题，
答案指向的正是这些文档。

KB-062 是旧 OA 用 GBK 导出的 `.txt`，`raw.decode("utf-8", errors="ignore")`
把解不出来的字节**直接删掉**：正文缺一块，不报错、不留痕。

**切块**（缺陷 8）：

```
for number, start in enumerate(range(0, len(text) - CHUNK_SIZE, CHUNK_SIZE), start=1):
```

上界少了整整一块。实测真实知识库：

| 项 | 值 |
|---|---|
| 正文总字数 | 32807 |
| 进了索引 | 26700 |
| 静默丢弃 | **6107 字（18.6%）** |
| 丢字的文档 | **35 / 35 篇**（每一篇都丢） |

一篇 587 字的文档只索引了前 300 字。这个循环只在**短于 `CHUNK_SIZE`** 时
走"整篇一块"的兜底分支，落在 300–600 字之间的文档恰好被砍掉尾巴。

同一段代码里 `heading=document.title`、`kind` 恒为 `"text"`、`table_header`
恒为 `[]`，于是 `units.py:124` 的表格分支和 `docfacts.render_row`（把表格行
拼成"商品名称：三文鱼，售价：35.00"）**从来没被执行过**——答案里会直接甩一根竖线。

**缓存**（缺陷 9、10）：

```
def content_key(kb_dir):
    digest.update(("%s|%s|%s\n" % (INDEX_VERSION, CHUNKER_VERSION, TOKENIZER_VERSION)).encode())
```

一个**知识库字节都不看**。改正文而不改版本号，键就不变，缓存永远"命中"。

而 `config.index_path` 指向 `PROJECT_DIR / ".cache" / "index.json"`，
这个文件**被提交进了 git**。仓库里那份是 25 篇 / 53 块，对不上实际的 35 篇——
比"缓存不失效"更糟：clone 下来直接就是用一份别人机器上的陈旧产物启动。

### 根因

三处在同一层：**"中文"和"结构"这两件事，原实现一件都没当真**。
按空白切词当中文没有空格，按 `.md` 过滤当知识库只有一种格式，
按固定字数切当文档是一根长字符串，按版本号做缓存键当代码是唯一的变量。

### 修复

commit `4b89860`（分词 + 加载）、`8becfd7`（切块 + 缓存）。

**分词**改**重叠二元组**：中文段逐字滑窗两字成词，英文与数字整词保留，
标点与中英边界断开。不引第三方分词库——契约 §8 要求 `make rebuild`
在干净环境能跑，而且 `docfacts` 本来就是照二元组写的。

```
tokenize("外卖订单退款")   →  ["外卖", "卖订", "订单", "单退", "退款"]
tokenize("Makai Poke 三文鱼") → ["makai", "poke", "三文", "文鱼"]
tokenize("退款，政策")     →  ["退款", "政策"]        # 不产生 "款，" 这种残渣
```

**加载**：`SUPPORTED_SUFFIXES` 收全 `.md/.markdown/.txt/.html/.htm`；
解码按 UTF-8 → GB18030 依次尝试，两次都不成才用替换字符兜底并留告警；
HTML 先整块去掉 `<script>`/`<style>` 再去标签，标签换成**空格**而不是空串
（否则 `<p>发票</p><p>宠物</p>` 会粘成 `发票宠物` 一个词）。

**切块**改成按结构切，`CHUNKER_VERSION` → `chunker-3`：

- 标题行起新段，标题跟着下面的正文走；`heading` 记成 `"总标题 > 二级 > 三级"`
  且**不带 `#`**——`units.py:119/145` 拿它去和 `sentence.strip().lstrip("#")`
  比对，带 `#` 就永远不相等，标题永远标不成 `kind="heading"`。
- 表格整行不切断，超过 `CHUNK_SIZE` 时按行分组，**每组重复表头**：
  KB-040 的表 23 行 1149 字必须拆，拆出去的那几行没表头就只剩一串数字，
  既检索不到"售价"，`render_row` 也拼不成人话。
- 正文按行累加到 `CHUNK_SIZE`，一行本身超长才按句子边界切，**一行都不丢**。
- 块在文档里的先后顺序不能乱：`units.py` 按顺序定位引用，
  `docfacts` 遇到同分也优先取靠前的那一句。

只按标题分段、**不按空行**：第一版在空行处也分段，真实知识库上切出 387 个块，
最短 7 字。BM25 的长度归一化专门优待短块，这些碎片会把真答案顶下去。
改成只按标题分段后是 204 块，中位 155 字。

| 项 | 修复前 | 修复后 |
|---|---|---|
| 块数 | 89 | 204（表格块 24） |
| 正文覆盖 | 26700 / 32807（81.4%） | **889 / 889 行（100%）** |
| 最长块 | 300 | 299 |
| 表格块 | 0 | 24，都带表头 |

**缓存**：`content_key()` 除三个版本号外，再哈希知识库的（相对路径 + 文件内容），
所以改正文、改文件名都会失效，换目录、整体拷一份则不会。
`.cache/index.json` 用 `git rm --cached` 移出仓库并加进 `.gitignore`。

### 回归测试

`starter/tests/test_retrieval.py`，两组共 26 个用例：

| 组 | commit | 用例 |
|---|---|---|
| 分词 / 加载 | `9a32aca`（红）→ `4b89860`（绿） | 中文切二元组、整句不成单一 token、问句与正文共享词、英文数字整词、标点不跨段、全角归一、虚词过滤、`.txt`/`.html` 入库、GBK 不乱码、HTML 去标签去脚本、无编号文件跳过 |
| 切块 / 缓存 | `7a65fed`（红）→ `8becfd7`（绿） | 一行正文都不丢（含尾行）、300–600 字的文档不被砍尾、块不超长、表格产出 `kind="table"` 与表头、拆表后每块都带表头、`heading` 无 `#` 且是路径、`chunk_id` 唯一、缓存键随内容变、陈旧缓存不被使用、新鲜缓存被复用、坏缓存能重建 |

真实知识库上只断言**与内容无关的不变量**（"目录里有几篇带编号的文档，
索引里就该有几篇"、"每一行正文都要出现在某个块里"），评审方换
`data/` 与 `knowledge_base/` 之后这些测试照样成立。

写这组测试时自己踩了**同一个坑两次**：两条测试第一版都是假绿。
一次是"300–600 字的文档"用了 20 行**同一句话**，集合去重后前 300 字就
"覆盖"了全部行；一次是缓存测试里 KB-A 的两块写得比别家长，
BM25 的长度归一化把它们压到后面，要压的"跳过"根本没发生。
两次都改成了真的会红的写法，并把原因写进注释。

### 一处过程失误

改完切块后跑了评测，从 38.00 掉到 34.00，C02 与 S02 双双变红。
差点当成自己改坏了——实际是**端口上挂着上一轮启动的服务进程**，
新起的那个 `bind` 失败直接退出了，整轮评测打的是旧代码。

```
ERROR: [Errno 48] error while attempting to bind on address ('127.0.0.1', 8000)
```

杀掉旧进程重跑：**59.00**。教训是评测前先确认 `lsof -nP -iTCP:8000` 指向的是哪个进程。

### 阶段结果

| 轮次 | 总分 | 检索 | doc | hybrid | data |
|---|---|---|---|---|---|
| 基线 | 18.00 | 6/15 | 2/16 | 0/18 | 8/12 |
| 清洗 + 指标 | 38.00 | 6/15 | 2/16 | 0/18 | 8/12 |
| 分词 + 加载 + 切块 + 缓存 | 59.00 | 12/15 | 8/16 | 6/18 | 8/12 |

---

## 4. 检索器：结果张冠李戴（缺陷 13）

### 现象

修完上面的基础设施，检索题到 12/15。剩下三道怎么调都上不去，
而且 `/api/retrieve` 的返回里有**自相矛盾**的一行：

```
doc_id=KB-012  chunk_id=KB-013#2
doc_id=KB-041  chunk_id=KB-022#2
```

`chunk_id` 的前缀就是文档号，两个字段各说各话。

### 假设

`hit.doc_id` 在构造之后又被改写过。

### 验证

`retriever.py` 原第 274-277 行：

```python
hit = self._hit(position, score, filtered)
# 第几条命中就取排序里的第几篇文档。
hit.doc_id = ordered[len(hits)].doc_id
```

`_hit()` 本来就从 `index.chunks[position]` 取了正确的 `doc_id`，这一行是**纯覆盖**。

它错在哪：`len(hits)` 是"已经收了几条"，而 `ordered` 是按分数排好的全部片段。
只有在**一条都没被跳过**时两者才同步。`MAX_CHUNKS_PER_DOC = 1` 会把同一篇文档的
第二块跳过（第 270 行 `continue`），从此 `len(hits)` 就落后于循环下标，
`ordered[len(hits)]` 指向的是**前面某一块**，`doc_id` 就挂到别人的片段上。

所以在 R01 里：KB-013#2 明明排第 3、也确实进了结果，报出去的却是 KB-012。
**答案检索到了，只是标错了名字**——这类错误最难查，因为打分的每一步看上去都对。

### 根因

把"循环跑到第几轮"当成了"收了几条"。中间有 `continue` 时两者不相等。

同一段代码里还叠着两个违反契约 §4 的毛病：

- `allowed = set(range(len(self.index.chunks)))` 把**全部**片段放进去打分，
  过滤却留到取完 top-k 之后（原第 306 行）才做。被过滤的文档先占名额再被删掉。
  契约 §4 对此有明文："先取前 `top_k` 再做过滤、结果只剩两三条的实现，不符合这一条。"
  实测 `top_k=3` 只剩 **1 条**。
- 补齐那一轮不检查 `MAX_CHUNKS_PER_DOC`，同一篇的片段会连着塞进来（实测 KB-001 一次占三格）。

### 修复

commit `69af4ce`：

- 删掉那行覆盖，`Hit` 从构造起就是对的。
- `allowed` 改成"非排除文档的片段"，过滤在打分**之前**完成。
- 补齐那一轮也守 `MAX_CHUNKS_PER_DOC`。
- `_coverage` 改用**实际返回**的片段算，不再用"排序后的前 top_k"：
  被过滤掉的文档不返回，就不该拿它的覆盖率代表这次检索。

### 回归测试

`starter/tests/test_retrieval.py`，commit `89523a4`（红）→ `69af4ce`（绿）：

| 用例 | 压的是哪一条 |
|---|---|
| `test_every_hit_reports_its_own_doc_id` | `doc_id` 与 `chunk_id` 必须同源 |
| `test_a_document_never_takes_more_than_one_slot` | `MAX_CHUNKS_PER_DOC = 1` |
| `test_filtered_chunks_never_take_a_slot` | 契约 §4：过滤不能先取后删 |
| `test_hits_are_sorted_by_score_descending` | 契约 §4：按相关性降序 |
| `test_a_tiny_index_returns_what_it_has` | 片段不足 top_k 时才允许更少 |

这组测试用的是手搭的合成索引（不落盘、不碰真实知识库），
所以把 `knowledge_base/` 整个换掉也照样能跑。

**写第一条测试时又踩了一次假绿**：KB-A 的两块写得比别家长，
BM25 的长度归一化把它们压到后面，"同一篇的第二个块被跳过"这个前提根本没发生，
两个下标也就不会错开。改成让它们成为最高分才真的红。

### 顺带修掉的测试基建缺陷（#19）

这组测试一开始只在**单独跑** `test_retrieval.py` 时通过，跟全套一起跑就拿到
`KB-013` 的假结果。根因在 `tests/conftest.py`：

```python
@pytest.fixture(scope="session")
def client(tmp_path_factory):
    ...
    retriever_module.Retriever.search = fake_search      # 在类上直接赋值
    return TestClient(server.app)                        # 从不还原
```

session 作用域 + 直接赋值 + 不还原 = 从第一个用到 `client` 的测试之后，
**整个进程**的检索都被换成了那个固定返回。检索自己的测试也跟着拿到
`KB-013`，却完全看不出来是哪儿来的。

改成函数作用域 + `pytest.MonkeyPatch`，`VAR_DIR` 与三个 `LLM_*`
环境变量一并纳入还原。commit `89523a4`。

### 阶段结果

| 轮次 | 总分 | 检索 | doc | hybrid | data | safety |
|---|---|---|---|---|---|---|
| 基线 | 18.00 | 6/15 | 2/16 | 0/18 | 8/12 | 6/9 |
| 清洗 + 指标 | 38.00 | 6/15 | 2/16 | 0/18 | 8/12 | 6/9 |
| 分词 + 加载 + 切块 + 缓存 | 59.00 | 12/15 | 8/16 | 6/18 | 8/12 | 6/9 |
| 检索器 | 56.00 | **15/15** | 8/16 | 3/18 | 10/12 | 3/9 |

**检索类从 6/15 到 15/15 满分。** 总分 59 → 56 的回落不是回归：
逐题对比是 H05、S01、V03 三道，H05 与 S01 的答案原文写着
"模型服务这次没有正常返回（工具调用没有收敛）"，V03 在两次运行之间反复
（0 → 1 → 0）。这三道都归到缺陷 #20 与后面的路由/作答阶段，
`eval/reports/2026-09-26_after_retriever/report.json` 里有原始答案可查。

---

<!-- 后续缺陷按同样格式追加 -->
