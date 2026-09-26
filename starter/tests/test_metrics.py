"""指标口径的回归测试，对应 KB-001 §4 与契约 §4。

和 `test_cleaning.py` 一个路子：用一份合成的清洗表压各条口径，**不写死真实
数据集的数字**（评审会换 `data/`），真实数据上只断言与数据无关的不变量。

夹具直接建清洗表，不走 `build_clean_db()`——指标层是独立的一层，
它的测试不该被清洗层的行为左右。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from kbqa.tools import DataTools

_SCHEMA = """
CREATE TABLE stores (store_id TEXT PRIMARY KEY, store_name TEXT, category TEXT, district TEXT);
CREATE TABLE products (product_id TEXT PRIMARY KEY, product_name TEXT,
                       product_category TEXT, unit_price REAL);
CREATE TABLE sales_clean (
    order_id TEXT, date TEXT, store_id TEXT, product_id TEXT,
    qty INTEGER, amount_cents INTEGER, payment TEXT, is_refund INTEGER
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

#: 列：order_id, date, store_id, product_id, qty, amount_cents, payment, is_refund
_SALES = [
    # 一张订单点了两个菜：orders 只能算 1 单，qty 要把两行都算上
    ("A1", "2026-06-01", "S01", "P01", 2, 2000, "微信", 0),
    ("A1", "2026-06-01", "S01", "P02", 1, 1000, "微信", 0),
    ("A2", "2026-06-10", "S01", "P01", 3, 3000, "现金", 0),
    # 第二张多行订单。有它在，`COUNT(*)` 与 `COUNT(DISTINCT order_id)` 才拉得开差距：
    # 区间内的销售行有 6 行，订单只有 4 张。少了这张单，半开区间会碰巧把最后一天
    # 排除掉，剩下的行数正好等于订单数，缺陷就被掩盖了。
    ("D1", "2026-06-20", "S01", "P01", 1, 400, "微信", 0),
    ("D1", "2026-06-20", "S01", "P02", 1, 600, "微信", 0),
    # 退款行：金额为负，冲减净营业额，同时单独计入退款金额
    ("A3", "2026-06-11", "S01", "P01", 1, -500, "现金", 1),
    # 只有退款的日期：净营业额是负的，订单数 0，客单价没有（不是 0）
    ("A4", "2026-06-12", "S01", "P02", 1, -800, "现金", 1),
    # 落在区间最后一天：end 必须是闭区间，否则整天数据丢失
    ("B1", "2026-06-30", "S02", "P01", 1, 700, "微信", 0),
    # 区间之外，不能被算进来
    ("C1", "2026-07-01", "S01", "P01", 1, 999, "微信", 0),
]

#: 2026-06-01 ~ 2026-06-30 全门店的口径期望
_EXPECTED_SUMMARY = {
    # 销售 77.00 冲掉退款 13.00
    "net_revenue": 64.00,
    "refund_amount": 13.00,
    # A1（两行）、A2、D1（两行）、B1 —— 6 行明细，去重后 4 单
    "orders": 4,
    # 销售 2+1+3+1+1+1 = 9，退款 1+1 = 2
    "qty": 7,
    # 64.00 ÷ 4
    "aov": 16.00,
}

_JUNE = ("2026-06-01", "2026-06-30")


@pytest.fixture(scope="module")
def tools(tmp_path_factory) -> DataTools:
    db = tmp_path_factory.mktemp("metrics") / "clean.db"
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO stores VALUES (?,?,?,?)",
        [("S01", "一号店", "拉面", "中区"), ("S02", "二号店", "轻食", "北区")],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?)",
        [("P01", "味噌拉面", "面类", 38.0), ("P02", "三文鱼poke", "轻食", 42.0)],
    )
    conn.executemany("INSERT INTO sales_clean VALUES (?,?,?,?,?,?,?,?)", _SALES)
    conn.commit()
    conn.close()
    return DataTools(db)


# -- 区间 -----------------------------------------------------------------------


def test_end_date_is_inclusive(tools):
    """契约 §4：区间两端都含。半开区间会整天丢掉区间最后一天的数据。

    B1 就落在 6 月 30 日。它必须出现在结果里，单独查这一天也必须查得到。
    """
    assert tools.query_metrics(*_JUNE)["net_revenue"] == 64.00
    assert tools.query_metrics("2026-06-30", "2026-06-30")["net_revenue"] == 7.00
    assert tools.query_metrics("2026-06-30", "2026-06-30")["orders"] == 1


def test_start_date_is_inclusive(tools):
    """A1 落在 6 月 1 日，单查这一天要查得到。"""
    assert tools.query_metrics("2026-06-01", "2026-06-01")["net_revenue"] == 30.00
    assert tools.query_metrics("2026-06-01", "2026-06-01")["orders"] == 1


def test_out_of_range_is_excluded(tools):
    """C1 在 7 月 1 日，不能漏进六月的口径里。"""
    assert tools.query_metrics(*_JUNE)["net_revenue"] == 64.00
    assert tools.query_metrics("2026-07-01", "2026-07-01")["net_revenue"] == 9.99


# -- §4 五个指标 ----------------------------------------------------------------


def test_net_revenue_includes_refunds(tools):
    """KB-001 §4：净营业额 = 销售行 + 退款行，退款是负的。

    只算销售行会多出 13.00；退款金额也不是 0，而是两笔退款的绝对值之和。
    """
    got = tools.query_metrics(*_JUNE)
    assert got["net_revenue"] == _EXPECTED_SUMMARY["net_revenue"]
    assert got["refund_amount"] == _EXPECTED_SUMMARY["refund_amount"]


def test_orders_counts_distinct_orders_not_rows(tools):
    """KB-001 §4：有效订单数 = 销售行的 `COUNT(DISTINCT order_id)`。

    6 月里销售行有 6 行，但只有 A1、A2、D1、B1 四张单——A1 和 D1 各点了两个菜。
    退款行不参与订单数，所以 A3、A4 不算。
    """
    assert tools.query_metrics(*_JUNE)["orders"] == _EXPECTED_SUMMARY["orders"]


def test_qty_nets_out_refunds(tools):
    """KB-001 §4：销量 = 销售行数量 − 退款行数量。"""
    assert tools.query_metrics(*_JUNE)["qty"] == _EXPECTED_SUMMARY["qty"]


def test_aov_uses_distinct_orders_as_denominator(tools):
    """KB-001 §4：客单价 = 净营业额 ÷ 有效订单数（不是 ÷ 明细行数）。

    按明细行数算会得到 64.00 / 6 = 10.67，按订单数算才是 16.00。
    """
    assert tools.query_metrics(*_JUNE)["aov"] == _EXPECTED_SUMMARY["aov"]


def test_summary_reports_all_metric_fields(tools):
    """契约 §4 的五个字段一个都不能少。"""
    got = tools.query_metrics(*_JUNE)
    assert set(_EXPECTED_SUMMARY) <= set(got)


# -- 空区间 ---------------------------------------------------------------------


def test_empty_range_is_zeros_not_nulls(tools):
    """区间内没有数据：金额与数量是 0，客单价是 `null`（不能拿 0 去除）。"""
    got = tools.query_metrics("2026-09-01", "2026-09-30")
    assert got["net_revenue"] == 0.0
    assert got["refund_amount"] == 0.0
    assert got["orders"] == 0
    assert got["qty"] == 0
    assert got["aov"] is None


# -- 过滤 -----------------------------------------------------------------------


def test_store_and_product_filters(tools):
    """按门店、按商品过滤，编号大小写与空白都要先规范化（KB-001 §2.1）。"""
    assert tools.query_metrics(*_JUNE, store_id="S02")["net_revenue"] == 7.00
    assert tools.query_metrics(*_JUNE, store_id=" s02 ")["net_revenue"] == 7.00
    assert tools.query_metrics(*_JUNE, store_id="S01")["net_revenue"] == 57.00
    # P02 在 6 月有三行：卖出 10.00 + 6.00，退回 8.00
    assert tools.query_metrics(*_JUNE, product_id="P02")["net_revenue"] == 8.00
    assert tools.query_metrics(*_JUNE, store_id="S01", product_id="P02")["net_revenue"] == 8.00
    # 拆开加起来要等于总数
    assert (
        tools.query_metrics(*_JUNE, store_id="S01")["net_revenue"]
        + tools.query_metrics(*_JUNE, store_id="S02")["net_revenue"]
        == _EXPECTED_SUMMARY["net_revenue"]
    )


# -- 按日 -----------------------------------------------------------------------


def test_daily_covers_every_day_including_empty_ones(tools):
    """契约 §4：区间内每一天都要有一条记录，没营业额的日期也要出现。"""
    days = tools.daily_metrics(*_JUNE)["days"]
    assert len(days) == 30
    assert [d["date"] for d in days][0] == "2026-06-01"
    assert [d["date"] for d in days][-1] == "2026-06-30"


def test_daily_uses_the_same_definitions_as_summary(tools):
    """按日与汇总必须是同一套口径，否则前端看板的柱子和卡片对不上。"""
    days = tools.daily_metrics(*_JUNE)["days"]
    by_date = {d["date"]: d for d in days}

    assert by_date["2026-06-01"]["net_revenue"] == 30.00
    assert by_date["2026-06-01"]["orders"] == 1
    assert by_date["2026-06-01"]["aov"] == 30.00
    # A1 两行算一单
    assert by_date["2026-06-20"]["net_revenue"] == 10.00
    assert by_date["2026-06-20"]["orders"] == 1
    # 只有退款的那两天：净额是负的，订单数 0，客单价 null
    assert by_date["2026-06-11"]["net_revenue"] == -5.00
    assert by_date["2026-06-11"]["orders"] == 0
    assert by_date["2026-06-11"]["aov"] is None
    assert by_date["2026-06-12"]["net_revenue"] == -8.00
    assert by_date["2026-06-12"]["aov"] is None
    # 没有数据的日期填 0，不是缺席
    assert by_date["2026-06-02"]["net_revenue"] == 0.0
    assert by_date["2026-06-02"]["aov"] is None

    total = tools.query_metrics(*_JUNE)
    assert round(sum(d["net_revenue"] for d in days), 2) == total["net_revenue"]
    assert sum(d["orders"] for d in days) == total["orders"]


# -- 真实数据集的不变量（不写死任何数字） ----------------------------------------


def test_real_dataset_metrics_are_consistent(tmp_path):
    """真实 `data/` 上跑一遍：几个口径之间的关系必须自洽。

    这里刻意不断言任何具体数字——评审会换数据。
    """
    from kbqa.cleaning import build_clean_db
    from kbqa.config import load_settings

    settings = load_settings()
    if not settings.source_db.exists():
        pytest.skip("没有 %s，跳过" % settings.source_db)

    db = tmp_path / "clean.db"
    build_clean_db(settings.source_db, db)
    real = DataTools(db)
    period = real.data_period()
    start, end = period["start"], period["end"]

    whole = real.query_metrics(start, end)
    # 退款金额是绝对值
    assert whole["refund_amount"] >= 0
    assert whole["orders"] > 0
    # 客单价 = 净营业额 ÷ 订单数，自洽到分（合成夹具里断言精确值，这里只断言关系）
    assert abs(whole["aov"] - whole["net_revenue"] / whole["orders"]) < 0.01
    # 每个区间的订单数不可能超过明细行数，销量也不可能超过销售行数量之和
    assert 0 < whole["orders"] <= 20000
    assert 0 < whole["qty"] <= 100000

    # 按日汇总要与区间汇总对得上——两边的口径必须完全一致
    days = real.daily_metrics(start, end)["days"]
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    assert len(days) == span
    assert abs(sum(d["net_revenue"] for d in days) - whole["net_revenue"]) < 0.01
    assert sum(d["orders"] for d in days) == whole["orders"]

    # 按门店拆开再加总，也要与区间汇总对得上（订单数在配额上是零容差的）
    per_store = real.by_store(start, end)["stores"]
    assert len(per_store) > 0
    assert abs(sum(s["net_revenue"] for s in per_store) - whole["net_revenue"]) < 0.01
    assert sum(s["orders"] for s in per_store) == whole["orders"]

    # 按支付方式拆开，同样要对得上账
    mix = real.payment_mix(start, end)
    assert abs(sum(p["net_revenue"] for p in mix["payments"].values())
               - whole["net_revenue"]) < 0.01
