"""把原始 sales 导进 var/clean.db，指标都查这张表。

清洗动作全部出自 KB-001：§2 规范化四条，§3 剔除六条（按顺序执行）。
本模块是唯一实现这两节的地方，工具层与接口层都不许再各写一套口径。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

#: 金额里的 `¥` 去掉再按数字解析。
_CURRENCY = str.maketrans("", "", "¥￥ \t　")

REMOVAL_REASONS = (
    "1_unparseable_date",
    "2_empty_amount",
    "3_qty_le_zero",
    "4_store_not_in_stores",
    "5_product_not_in_products",
    "6_duplicate_row",
)

#: KB-001 §2.2：date 接受三种格式，按顺序试。
#: `DD-MM-YYYY` 是旧 POS 的导出格式，**日在前、月在后**——`25-07-2026` 是 2026 年 7 月 25 日。
#: 放在最后试，所以 ISO 与斜杠格式不会被它抢走：`2026-06-01` 用 `%d-%m-%Y` 解析会
#: 把 2026 当成"日"而失败，方向不会反。
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y")


def normalise_code(value: Optional[str]) -> str:
    """KB-001 §2.1：门店与商品编号去掉首尾空白并转为大写。

    `s01`、`S01 `、` s03` 规范化之后都是合法编号，**不能**当脏数据扔掉。
    这一步必须发生在判外键之前，顺序反了会误删真实订单（§7.2）。
    """
    return (value or "").strip().upper()


def parse_date(value: Optional[str]) -> Optional[str]:
    """按 KB-001 §2.2 解析日期，返回 `YYYY-MM-DD`；三种格式都认不出来时返回 None。"""
    text = (value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(value: Optional[str]) -> tuple[Optional[int], str]:
    """返回 (分, 状态)。状态取值：`ok`、`empty`、`bad`。

    KB-001 §2.3 与 §3.2：`¥38.00` 与 `38.00` 是同一个金额，带符号的行是可恢复的，
    必须保留；空金额直接剔除，**不回填**（v2 才按 `qty × unit_price` 回填，v3 改了）。
    """
    text = (value or "").translate(_CURRENCY)
    if not text:
        return None, "empty"
    try:
        cents = int((Decimal(text) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None, "bad"
    return cents, "ok"


def parse_qty(value: Optional[str]) -> Optional[int]:
    """KB-001 §2.4：按整数解析。解析不了的返回 None，会被 §3.3 剔除。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class CleaningReport:
    raw_rows: int = 0
    kept_rows: int = 0
    kept_sales_rows: int = 0
    kept_refund_rows: int = 0
    removed: dict[str, int] = field(default_factory=lambda: {k: 0 for k in REMOVAL_REASONS})
    note_unparseable_amount: int = 0

    def as_dict(self) -> dict:
        return {
            "raw_rows": self.raw_rows,
            "removed": dict(self.removed, note_unparseable_amount=self.note_unparseable_amount),
            "kept_rows": self.kept_rows,
            "kept_sales_rows": self.kept_sales_rows,
            "kept_refund_rows": self.kept_refund_rows,
        }


def open_readonly(path: Path) -> sqlite3.Connection:
    """以**只读**方式打开数据库。

    契约 §5 要求"数据库不能有任何改动"。原来的实现叫 `open_readonly`，
    做的却是普通的 `sqlite3.connect`——名字只表达了意图，没有任何东西
    拦着写：`run_sql` 是模型可以直接调的工具，它只要发出一条
    `DELETE` / `DROP`，清洗表就没了，而且后面每个指标都会跟着错。
    用 URI 的 `mode=ro`，写操作由 SQLite 自己拒绝，不靠调用方自觉。

    路径先 `resolve()` 再 `as_uri()`：URI 里带空格、`?`、`#` 的路径
    （macOS 上很常见）必须转义，手拼 `file:` 前缀会连库都打不开。
    """
    conn = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def clean_rows(
    rows: Iterable[sqlite3.Row],
    store_ids: Iterable[str],
    product_ids: Iterable[str],
) -> tuple[list[tuple], CleaningReport]:
    """按 KB-001 §2 规范化、§3 剔除，返回（清洗后的行, 台账）。

    §3 开头写着"按下面的顺序执行"，所以这里是一串 `continue`——一行同时踩两条规则时
    只记在前一条头上，各条计数加起来才等于剔除总数，台账不会重复计数。
    """
    report = CleaningReport()
    valid_stores = {normalise_code(code) for code in store_ids}
    valid_products = {normalise_code(code) for code in product_ids}
    seen: set[tuple] = set()
    kept: list[tuple] = []

    for row in rows:
        report.raw_rows += 1

        # §3.1 日期解析不了
        day = parse_date(row["date"])
        if day is None:
            report.removed["1_unparseable_date"] += 1
            continue

        # §3.2 金额为空。v3 起直接剔除，**不回填**——v2 是按 qty × unit_price 回填的。
        # 非空但解析不出来的（"abc" 之类）同样剔除，不能悄悄当 0 元记一笔收入；
        # 它单独记在 note 里，方便数据质量面板区分"空值"和"垃圾值"。
        cents, status = parse_amount(row["amount"])
        if status != "ok":
            report.removed["2_empty_amount"] += 1
            if status == "bad":
                report.note_unparseable_amount += 1
            continue

        # §3.3 数量 ≤ 0，或压根不是整数
        qty = parse_qty(row["qty"])
        if qty is None or qty <= 0:
            report.removed["3_qty_le_zero"] += 1
            continue

        # §2.1 编号规范化必须发生在判外键之前，否则 `s01` 会被当成不存在的门店误删
        order_id = (row["order_id"] or "").strip()
        store_id = normalise_code(row["store_id"])
        product_id = normalise_code(row["product_id"])
        payment = (row["payment"] or "").strip()

        # §3.4 / §3.5 维表里没有这个编号
        if store_id not in valid_stores:
            report.removed["4_store_not_in_stores"] += 1
            continue
        if product_id not in valid_products:
            report.removed["5_product_not_in_products"] += 1
            continue

        # §3.6 七个字段规范化后**完全一致**才算重复行。共用订单号、只差商品的多行订单
        # （同一单点了两个菜）不是重复，必须留下；金额正负不同也不是重复。
        signature = (order_id, day, store_id, product_id, qty, cents, payment)
        if signature in seen:
            report.removed["6_duplicate_row"] += 1
            continue
        seen.add(signature)

        kept.append(
            (order_id, day, store_id, product_id, qty, cents, payment, 1 if cents < 0 else 0)
        )

    report.kept_rows = len(kept)
    report.kept_refund_rows = sum(1 for row in kept if row[-1])
    report.kept_sales_rows = report.kept_rows - report.kept_refund_rows
    return kept, report


_SCHEMA = """
CREATE TABLE stores (store_id TEXT PRIMARY KEY, store_name TEXT, category TEXT, district TEXT);
CREATE TABLE products (product_id TEXT PRIMARY KEY, product_name TEXT,
                       product_category TEXT, unit_price REAL);
CREATE TABLE sales_clean (
    order_id TEXT, date TEXT, store_id TEXT, product_id TEXT,
    qty INTEGER, amount_cents INTEGER, payment TEXT, is_refund INTEGER
);
CREATE INDEX idx_clean_date ON sales_clean(date);
CREATE INDEX idx_clean_store ON sales_clean(store_id);
CREATE INDEX idx_clean_product ON sales_clean(product_id);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def build_clean_db(source: Path, target: Path) -> CleaningReport:
    """从只读的源库重建清洗表。返回清洗台账，供 `/api/health` 与数据质量面板使用。"""
    if not source.exists():
        raise FileNotFoundError("找不到源数据库：%s" % source)
    src = open_readonly(source)
    try:
        raw_stores = [
            tuple(r)
            for r in src.execute("SELECT store_id, store_name, category, district FROM stores")
        ]
        raw_products = [
            tuple(r)
            for r in src.execute(
                "SELECT product_id, product_name, product_category, unit_price FROM products"
            )
        ]
        rows, report = clean_rows(
            src.execute("SELECT order_id, date, store_id, product_id, qty, amount, payment FROM sales"),
            [row[0] for row in raw_stores],
            [row[0] for row in raw_products],
        )
    finally:
        src.close()

    # 维表跟着 §2.1 一起规范化，清洗表里的外键才真的能对上；
    # 规范化后若撞号（`s01` 与 `S01` 并存）保留先出现的那个。
    stores = list({normalise_code(row[0]): row for row in raw_stores}.values())
    stores = [(normalise_code(row[0]),) + row[1:] for row in stores]
    products = list({normalise_code(row[0]): row for row in raw_products}.values())
    products = [(normalise_code(row[0]),) + row[1:] for row in products]

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    out = sqlite3.connect(target)
    try:
        out.executescript(_SCHEMA)
        out.executemany("INSERT INTO stores VALUES (?,?,?,?)", stores)
        out.executemany("INSERT INTO products VALUES (?,?,?,?)", products)
        out.executemany("INSERT INTO sales_clean VALUES (?,?,?,?,?,?,?,?)", rows)
        out.execute(
            "INSERT INTO meta VALUES ('cleaning_report', ?)",
            (json.dumps(report.as_dict(), ensure_ascii=False),),
        )
        out.execute("INSERT INTO meta VALUES ('source_db', ?)", (source.name,))
        out.commit()
    finally:
        out.close()
    return report
