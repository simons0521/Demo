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

<!-- 后续缺陷按同样格式追加 -->
