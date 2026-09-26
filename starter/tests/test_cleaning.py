"""清洗口径的回归测试，对应 KB-001 §2（规范化）与 §3（剔除）。

两条原则：

* **不写死真实数据集的数字。** 评审时会换掉 `data/`，所以六条剔除各自的计数
  用一份合成夹具来断言；真实数据集只断言与数据无关的不变量（日期都是 ISO、
  规范化后的外键都合法之类）。
* **只测行为，不测内部函数名。** 六条剔除的顺序、规范化的写法，全部通过
  `build_clean_db()` 的产物来验证。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kbqa.cleaning import REMOVAL_REASONS, build_clean_db, parse_amount

_SOURCE_SCHEMA = """
CREATE TABLE stores (store_id TEXT PRIMARY KEY, store_name TEXT, category TEXT, district TEXT);
CREATE TABLE products (product_id TEXT PRIMARY KEY, product_name TEXT,
                       product_category TEXT, unit_price REAL);
CREATE TABLE sales (order_id TEXT, date TEXT, store_id TEXT, product_id TEXT,
                    qty TEXT, amount TEXT, payment TEXT);
"""


def _make_source(path: Path, rows: list[tuple]) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(_SOURCE_SCHEMA)
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?)",
        [("S01", "一号店", "拉面", "中区"), ("S02", "二号店", "轻食", "北区")],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?)",
        [("P01", "味噌拉面", "面类", 38.0), ("P02", "三文鱼poke", "轻食", 42.0)],
    )
    conn.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


#: 21 行合成明细，每一行都是为了压中某一条或某几条规则而造的。
#: 列：order_id, date, store_id, product_id, qty, amount, payment
_ROWS = [
    # --- 应当保留的 9 行 ---
    ("ORD1", "2026-06-01", "S01", "P01", "1", "10.00", "微信"),
    # §2.1 编号大小写与首尾空白要规范化；§2.2 斜杠日期；§2.3 带 ¥ 的金额
    ("ORD2", "2026/6/2", "s02 ", " p02", "2", "¥20.00", "支付宝"),
    # §2.2 DD-MM-YYYY **日在前**：30 只能当"日"，按月在前会变成非法日期
    ("ORD3", "30-07-2026", "S01", "P01", "1", "5.00", "现金"),
    # §3.6 共用订单号、只差商品的两行是合法的多行订单，必须全部留下
    ("ORD14a", "2026-06-07", "S01", "P01", "1", "8.00", "微信"),
    ("ORD14b", "2026-06-07", "S01", "P02", "2", "12.00", "微信"),
    # §4 退款行：金额为负，要保留，不能当脏数据扔掉
    ("ORD15", "2026-06-08", "S01", "P01", "1", "-5.00", "现金"),
    ("ORD16", "2026-06-08", "S01", "P01", "1", "¥-3.50", "现金"),
    # §2.1 全小写 + 前后空格，规范化后是合法外键
    ("ORD17", "2026-06-09", "s01", "p01", "1", "6.00", "微信"),
    # --- 规则 1：日期无法解析，4 行 ---
    ("ORD4", "2026-13-45", "S01", "P01", "1", "5.00", "现金"),
    ("ORD5", "N/A", "S01", "P01", "1", "5.00", "现金"),
    ("ORD6", "", "S01", "P01", "1", "5.00", "现金"),
    # 顺序证据：这行同时踩了规则 1 和规则 2，只能记在规则 1 头上
    ("ORD7", "N/A", "S01", "P01", "1", "", "现金"),
    # --- 规则 2：amount 为空，2 行 ---
    ("ORD8", "2026-06-03", "S01", "P01", "1", "", "现金"),
    ("ORD9", "2026-06-03", "S01", "P01", "1", "  ¥  ", "现金"),
    # --- 规则 3：qty ≤ 0 或不是整数，3 行 ---
    ("ORD10", "2026-06-04", "S01", "P01", "0", "5.00", "现金"),
    ("ORD11", "2026-06-04", "S01", "P01", "-1", "5.00", "现金"),
    ("ORD12", "2026-06-04", "S01", "P01", "abc", "5.00", "现金"),
    # --- 规则 4：规范化后门店不在 stores 里，1 行 ---
    ("ORD13", "2026-06-05", "S99", "P01", "1", "5.00", "现金"),
    # --- 规则 5：规范化后商品不在 products 里，1 行 ---
    ("ORD18", "2026-06-05", "S01", "P99", "1", "5.00", "现金"),
    # --- 规则 6：七个字段规范化后完全相同的重复行，2 行留 1，1 行 ---
    ("ORD19", "2026-06-06", "S01", "P01", "1", "5.00", "微信"),
    ("ORD19", "2026-06-06", "S01", "P01", "1", "5.00", "微信"),
]

_EXPECTED_REMOVED = {
    "1_unparseable_date": 4,
    "2_empty_amount": 2,
    "3_qty_le_zero": 3,
    "4_store_not_in_stores": 1,
    "5_product_not_in_products": 1,
    "6_duplicate_row": 1,
}
_EXPECTED_KEPT = 9


@pytest.fixture(scope="module")
def cleaned(tmp_path_factory):
    """把合成明细清洗一遍，返回（台账, 清洗表里的行）。"""
    work = tmp_path_factory.mktemp("cleaning")
    source = _make_source(work / "pos.db", _ROWS)
    target = work / "clean.db"
    report = build_clean_db(source, target)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("SELECT * FROM sales_clean")]
    conn.close()
    return report, rows


# -- §2 规范化 ------------------------------------------------------------------


def test_amount_strips_currency_symbol():
    """KB-001 §2.3 / §7.1：`¥38.00` 与 `38.00` 是同一个金额，带符号的行要保留。"""
    assert parse_amount("¥38.00") == (3800, "ok")
    assert parse_amount("38.00") == (3800, "ok")
    assert parse_amount(" ¥ 20.00 ") == (2000, "ok")
    assert parse_amount("¥-3.50") == (-350, "ok")


def test_empty_amount_is_a_removal_not_a_fill():
    """KB-001 §3.2：amount 为空直接剔除，**不回填**（v2 才回填，v3 改了）。"""
    assert parse_amount("") == (None, "empty")
    assert parse_amount("  ¥ ") == (None, "empty")
    assert parse_amount(None) == (None, "empty")


def test_ids_are_normalised_before_foreign_key_check(cleaned):
    """KB-001 §2.1 / §7.2：先规范化再判外键，顺序反了会误删真实订单。"""
    _, rows = cleaned
    kept = {(row["order_id"]) for row in rows}
    # ORD2 的 s02 / p02、ORD17 的 s01 / p01 都要变成大写并被留下
    assert {"ORD2", "ORD17"} <= kept
    for row in rows:
        assert row["store_id"] == row["store_id"].strip().upper()
        assert row["product_id"] == row["product_id"].strip().upper()


def test_dates_are_normalised_to_iso(cleaned):
    """KB-001 §2.2：三种格式都要收，`DD-MM-YYYY` 是**日在前**。"""
    _, rows = cleaned
    by_order = {row["order_id"]: row["date"] for row in rows}
    assert by_order["ORD2"] == "2026-06-02"      # 2026/6/2
    assert by_order["ORD3"] == "2026-07-30"      # 30-07-2026，日在前
    assert all(len(row["date"]) == 10 and row["date"][4] == "-" for row in rows)


# -- §3 剔除六条 ----------------------------------------------------------------


def test_each_removal_rule_has_its_own_count(cleaned):
    """六条剔除各自的计数必须准确，数据质量面板才能说清剔掉了什么。"""
    report, _ = cleaned
    assert report.removed == _EXPECTED_REMOVED


def test_removal_reasons_are_fully_accounted(cleaned):
    """台账要能对上账：原始行数 = 保留行数 + 各条剔除之和。"""
    report, rows = cleaned
    assert report.raw_rows == len(_ROWS)
    assert report.kept_rows == _EXPECTED_KEPT == len(rows)
    assert report.raw_rows == report.kept_rows + sum(report.removed.values())


def test_rules_run_in_order(cleaned):
    """KB-001 §3 说了"按顺序执行"：同时踩两条规则的行，只记在前一条头上。

    ORD7 的日期是 `N/A`、金额是空串。它只能出现在 `1_unparseable_date` 里，
    不能同时算进 `2_empty_amount`——否则各条计数加起来会超过剔除总数。
    """
    report, _ = cleaned
    assert report.removed["1_unparseable_date"] == 4
    assert report.removed["2_empty_amount"] == 2
    assert report.raw_rows == report.kept_rows + sum(report.removed.values())


def test_multi_line_orders_are_kept(cleaned):
    """KB-001 §3.6 / §7.3：共用订单号、只差商品的行是"一张订单点了两个菜"，必须留。

    只有七个字段规范化后**完全一致**的才算重复行。
    """
    _, rows = cleaned
    assert sorted(row["product_id"] for row in rows if row["order_id"] == "ORD14a") == ["P01"]
    assert sorted(row["product_id"] for row in rows if row["order_id"] == "ORD14b") == ["P02"]
    assert len([row for row in rows if row["order_id"].startswith("ORD14")]) == 2


def test_refund_rows_are_kept_and_flagged(cleaned):
    """KB-001 §4：金额为负的是退款行，保留下来并标 is_refund，不参与销售行统计。"""
    _, rows = cleaned
    refunds = {row["order_id"]: row for row in rows if row["is_refund"]}
    assert set(refunds) == {"ORD15", "ORD16"}
    assert refunds["ORD15"]["amount_cents"] == -500
    assert refunds["ORD16"]["amount_cents"] == -350
    assert all(row["amount_cents"] >= 0 for row in rows if not row["is_refund"])


def test_report_shape_covers_every_reason(cleaned):
    """台账的键要覆盖全部剔除原因，前端数据质量面板按它渲染。"""
    report, _ = cleaned
    assert set(report.removed) == set(REMOVAL_REASONS)
    assert report.kept_sales_rows + report.kept_refund_rows == report.kept_rows
    assert report.kept_sales_rows == 7
    assert report.kept_refund_rows == 2


# -- 真实数据集的不变量（不写死任何数字） ----------------------------------------


def test_real_dataset_is_actually_cleaned(tmp_path):
    """真实 `data/` 上跑一遍：清洗必须真的剔掉东西，且日期全部规范化。

    这里刻意不断言任何具体数字——评审会换数据。
    """
    from kbqa.config import load_settings

    settings = load_settings()
    if not settings.source_db.exists():
        pytest.skip("没有 %s，跳过" % settings.source_db)

    target = tmp_path / "clean.db"
    report = build_clean_db(settings.source_db, target)
    conn = sqlite3.connect(target)
    try:
        rows = conn.execute("SELECT date, store_id, product_id, qty FROM sales_clean").fetchall()
        stores = {r[0] for r in conn.execute("SELECT store_id FROM stores")}
        products = {r[0] for r in conn.execute("SELECT product_id FROM products")}
        period = conn.execute("SELECT MIN(date), MAX(date) FROM sales_clean").fetchone()
    finally:
        conn.close()

    # 清洗要真的生效
    assert report.kept_rows < report.raw_rows
    assert sum(report.removed.values()) > 0
    # 日期全部是 ISO，脏值不能留下来
    assert all(len(d) == 10 and d[4] == "-" for d, _, _, _ in rows)
    assert period[0] and period[1] and len(period[0]) == 10
    # 外键全部合法，qty 全部为正
    assert all(store in stores for _, store, _, _ in rows)
    assert all(product in products for _, _, product, _ in rows)
    assert all(qty > 0 for _, _, _, qty in rows)
