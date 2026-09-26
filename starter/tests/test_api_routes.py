"""新增的四条只读接口（契约 §1 允许"在此之外增加任何接口和字段"）。

这四条都是给第一关的看板和第四关的调试面板用的：

* `/api/stores`、`/api/metrics/top_products` —— 看板上不该有写死的门店，
  门店和商品都得从后端来，评审换掉 `data/` 之后面板才不会开始说谎。
* `/api/traces` —— 调试面板得先能列出"最近有哪些次问答"，否则只能靠手粘 id。
* `/api/data_quality` 的 `removal_reasons` —— 剔除原因的中文标签。

**加的全是字段和路由，老接口的形状一个没动**：`/api/data_quality` 的
`cleaning_report`、`/api/health` 的 `cleaning_report` 都是冻结的（单测与评测
都盯着那几个键），所以标签另起一个字段。
"""

from __future__ import annotations

from kbqa import server


def test_every_new_route_answers_200(client):
    """四条路由都活着。"""
    for path in ("/api/stores", "/api/metrics/top_products", "/api/traces", "/api/data_quality"):
        params = {"start": "2026-05-01", "end": "2026-08-31"} if "top_products" in path else None
        response = client.get(path, params=params)
        assert response.status_code == 200, "%s 返回了 %s" % (path, response.status_code)
        assert isinstance(response.json(), dict), "%s 的顶层必须是对象" % path


# -- /api/stores ------------------------------------------------------------------


def test_stores_comes_from_the_database(client):
    """门店列表来自库里，不是前端写死的。

    `data/` 被换掉之后这张表就变了——接口照着查，面板跟着变。
    """
    stores = client.get("/api/stores").json()["stores"]
    assert stores, "门店列表是空的，看板的门店下拉就没得选了"
    for store in stores:
        assert {"store_id", "store_name"} <= set(store)


# -- /api/metrics/top_products ----------------------------------------------------


def test_top_products_returns_a_ranked_list(client):
    payload = client.get(
        "/api/metrics/top_products",
        params={"start": "2026-05-01", "end": "2026-08-31", "limit": 5},
    ).json()
    products = payload["products"]
    assert len(products) <= 5, "limit 没生效"
    for item in products:
        assert {"product_id", "product_name", "net_revenue", "orders", "qty"} <= set(item)


def test_top_products_rejects_a_bad_date(client):
    """日期非法是 400（参数错），不是 500。"""
    response = client.get(
        "/api/metrics/top_products", params={"start": "2026/05/01", "end": "2026-08-31"}
    )
    assert response.status_code == 400
    assert "error" in response.json()


def test_top_products_rejects_a_bad_limit(client):
    """`limit` 越界是 422：0 和 999 都不该悄悄被接受。"""
    for limit in (0, 999):
        response = client.get(
            "/api/metrics/top_products",
            params={"start": "2026-05-01", "end": "2026-08-31", "limit": limit},
        )
        assert response.status_code == 422, "limit=%s 没有被拒绝" % limit


# -- /api/traces ------------------------------------------------------------------


def test_traces_lists_summaries_and_never_the_full_steps(client):
    """列表接口只给摘要——整份 trace 有大几十 KB，20 份叠起来这个响应就没法用了。"""
    answer = client.post("/api/chat", json={"question": "外卖订单多久内可以申请退款？"}).json()
    summaries = client.get("/api/traces").json()["traces"]
    assert summaries, "刚聊过一次，列表却是空的"

    assert summaries[0]["trace_id"] == answer["trace_id"], "最新的应该排最前面"
    for item in summaries:
        assert "steps" not in item, "列表接口把完整 steps 也带出来了"
        assert "llm_calls" not in item, "列表接口把完整 llm_calls 也带出来了"
        assert set(item) == {
            "trace_id",
            "session_id",
            "question",
            "started_at",
            "total_ms",
            "step_count",
            "llm_call_count",
            "error_count",
        }
    assert summaries[0]["step_count"] >= 2, "至少该有 plan 和 response 两步"

    # 对照：单条查询那条路由是**有** steps 的，摘要的"没有"不是因为压根没记。
    full = client.get("/api/trace/%s" % answer["trace_id"]).json()
    assert full["steps"]


def test_traces_rejects_a_bad_limit(client):
    for limit in (0, 999):
        assert client.get("/api/traces", params={"limit": limit}).status_code == 422


# -- /api/data_quality 的 removal_reasons -----------------------------------------


def test_removal_reasons_cover_every_key_with_a_label(client):
    """每个剔除原因都要有中文标签，且顺序、数值与报告逐项一致。"""
    quality = client.get("/api/data_quality").json()
    report = quality["cleaning_report"]
    reasons = quality["removal_reasons"]

    assert [item["key"] for item in reasons] == list(report["removed"])
    for item in reasons:
        assert item["label_cn"] and item["label_cn"] != item["key"], "键 %s 没有中文标签" % item["key"]
        assert item["removed"] == report["removed"][item["key"]]


def test_the_note_is_not_counted_as_a_removal_reason(client):
    """`note_unparseable_amount` 是**注解**，不是第七条剔除原因。

    它是 `2_empty_amount` 里"金额是垃圾值、不是空值"的那部分，已经计在
    那一格里了。面板把它当一条原因加起来的话，剔除总数就会多出一截，
    对不上 `raw_rows - kept_rows`。这条测试把这件事钉死。
    """
    quality = client.get("/api/data_quality").json()
    report = quality["cleaning_report"]
    reasons = quality["removal_reasons"]

    kinds = {item["key"]: item["kind"] for item in reasons}
    assert kinds["note_unparseable_amount"] == "note"
    assert sum(1 for kind in kinds.values() if kind == "note") == 1

    counted = sum(item["removed"] for item in reasons if item["kind"] == "reason")
    assert counted == report["raw_rows"] - report["kept_rows"], (
        "剔除原因的合计 %d 对不上原始行数 - 保留行数 %d"
        % (counted, report["raw_rows"] - report["kept_rows"])
    )


def test_cleaning_report_shape_is_frozen(client):
    """加了中文标签，`cleaning_report` 本身必须一个字节没变。

    它同时流进 `/api/health`，单测与评测的 health `expect` 都盯着那几个键。
    """
    from_quality = client.get("/api/data_quality").json()["cleaning_report"]
    from_health = client.get("/api/health").json()["cleaning_report"]
    assert from_quality == from_health == server.service().tools.cleaning_report()
