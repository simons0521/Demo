# 评测报告

- 服务地址：`http://127.0.0.1:8000`
- 题库：`/Users/simons/Desktop/moneki-ai-takehome/eval/public_questions.jsonl`
- 生成时间：2026-09-26 20:47:33
- 知识库：载入 35 份文档（用于 quote 逐字校验）

## 总分

**18.00 / 18.00（100.0%）**，6 题全绿 / 共 6 题。

每题耗时：中位数 5.84 秒，最大 7.65 秒，合计 36.3 秒。

## 分类别

| 类别 | 得分 | 满分 | 比例 | 全绿题数 |
|---|---|---|---|---|
| 数据 + 文档（`hybrid`） | 18.00 | 18.00 | 100.0% | 6 / 6 |

## `/api/health` 快照

```json
{
  "status": "ok",
  "llm_mode": "live",
  "kb_docs": 35,
  "kb_chunks": 204,
  "valid_sales_rows": 18290,
  "today": "2026-09-01",
  "data_period": {
    "start": "2026-05-01",
    "end": "2026-08-31"
  },
  "cleaning_report": {
    "raw_rows": 18628,
    "removed": {
      "1_unparseable_date": 8,
      "2_empty_amount": 150,
      "3_qty_le_zero": 30,
      "4_store_not_in_stores": 10,
      "5_product_not_in_products": 40,
      "6_duplicate_row": 100,
      "note_unparseable_amount": 0
    },
    "kept_rows": 18290,
    "kept_sales_rows": 18196,
    "kept_refund_rows": 94
  },
  "index_key": "d21d7a42020b",
  "kb_warnings": [
    "跳过没有 KB 编号的文件：README.md"
  ]
}
```

## 没通过的题（0 道）

没有。

## 全部题目

| 题号 | 类别 | 得分 | 满分 | 耗时（秒） |
|---|---|---|---|---|
| H01 | hybrid | 3.00 | 3.00 | 5.12 |
| H02 | hybrid | 3.00 | 3.00 | 5.36 |
| H03 | hybrid | 3.00 | 3.00 | 6.50 |
| H04 | hybrid | 3.00 | 3.00 | 5.47 |
| H05 | hybrid | 3.00 | 3.00 | 6.21 |
| H06 | hybrid | 3.00 | 3.00 | 7.65 |
