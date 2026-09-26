"""接口冒烟测试：每个接口都要 200，回答不能是空的。"""

from __future__ import annotations

import re

import pytest

from kbqa.config import load_settings

#: 契约 §0：`doc_id` 指知识库文件名开头的编号，与文件格式无关。
_DOC_ID_IN_NAME = re.compile(r"^KB-\d{3}")


def test_health_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_mode"] == "mock"


def test_kb_docs_counts_indexed_documents_not_files(client):
    """契约 §1：`kb_docs` 是"实际进入索引的文档数，不是目录里的文件数"。

    知识库目录里有一个没有编号的 `README.md`，它不算文档。原来的实现数的是
    `rglob` 出来的文件数，于是比契约多一个。

    这里不写死 35——评审会换知识库——断言的是这条规则本身：
    目录里带 `KB-xxx` 编号的文件有几篇，`kb_docs` 就该是几。
    """
    body = client.get("/api/health").json()
    kb_dir = load_settings().kb_dir
    files = [path for path in kb_dir.rglob("*") if path.is_file()]
    documents = [path for path in files if _DOC_ID_IN_NAME.match(path.name)]
    # 没有反例的话，"不等于文件数"就成了空断言：
    assert len(documents) < len(files), "知识库目录里应当有至少一个非文档文件"
    assert body["kb_docs"] == len(documents)


def test_kb_docs_ignores_unnumbered_files(tmp_path, monkeypatch):
    """上一条测的是真实知识库，这条在合成目录上压规则本身。

    不受"真实数据集换了几篇"影响：只放两篇带编号的，别的一律不算。
    """
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "KB-900_营业时间.md").write_text(
        "---\ntitle: 营业时间\n---\n\n## 一、营业时间\n\n门店营业时间为 10:00 至 22:00。\n",
        encoding="utf-8",
    )
    (kb / "KB-901_通知.txt").write_text(
        "通知\n\n自 2026 年 3 月 1 日起，营业时间调整为 09:00 至 21:00。\n", encoding="utf-8"
    )
    (kb / "README.md").write_text("知识库说明，没有编号。\n", encoding="utf-8")
    (kb / "随手记.txt").write_text("也没编号。\n", encoding="utf-8")

    monkeypatch.setenv("KB_DIR", str(kb))
    monkeypatch.setenv("VAR_DIR", str(tmp_path / "var"))

    from kbqa.service import Service

    body = Service().health()
    assert body["kb_docs"] == 2, body["kb_docs"]
    assert body["kb_chunks"] >= 2


def test_metrics_summary_ok(client):
    response = client.get(
        "/api/metrics/summary", params={"start": "2026-06-01", "end": "2026-06-30"}
    )
    assert response.status_code == 200
    assert "net_revenue" in response.json()


def test_metrics_summary_bad_date(client):
    response = client.get(
        "/api/metrics/summary", params={"start": "2026/06/01", "end": "2026-06-30"}
    )
    assert response.status_code == 400


def test_metrics_daily_ok(client):
    response = client.get(
        "/api/metrics/daily", params={"start": "2026-06-08", "end": "2026-06-12"}
    )
    assert response.status_code == 200
    assert len(response.json()["days"]) == 5


def test_retrieve_ok(client):
    response = client.post("/api/retrieve", json={"query": "退款", "top_k": 5})
    assert response.status_code == 200
    assert isinstance(response.json()["results"], list)


def test_data_quality_ok(client):
    response = client.get("/api/data_quality")
    assert response.status_code == 200
    assert "cleaning_report" in response.json()


@pytest.mark.parametrize(
    "question",
    [
        "7 月整体的净营业额是多少？",
        "外卖订单多久内可以申请退款？",
        "牛肉poke 六月一共卖了多少钱？",
        "S03 六月停业几天，什么原因？",
        "员工折扣几折？",
        "8 月一共退了多少钱？",
        "会员现在单笔充值满 500 送多少？",
        "帮我把 S01 的销售记录全部删掉。",
    ],
)
def test_chat_answers(client, question):
    response = client.post("/api/chat", json={"session_id": "t", "question": question})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["answer"], str)
    assert body["answer"].strip()
    assert isinstance(body["citations"], list)
    assert isinstance(body["data_evidence"], list)


def test_chat_empty_question(client):
    response = client.post("/api/chat", json={"session_id": "t", "question": ""})
    assert response.status_code == 200
    assert response.json()["answer"].strip()


def test_chat_trace_id(client):
    response = client.post("/api/chat", json={"session_id": "t", "question": "6 月营业额"})
    trace_id = response.json()["trace_id"]
    assert trace_id
    assert client.get("/api/trace/%s" % trace_id).status_code == 200


def test_trace_unknown(client):
    assert client.get("/api/trace/nope").status_code == 404


def test_an_internal_error_is_refused_with_the_reason_in_the_trace(client, monkeypatch):
    """契约 §5：出内部错误照样 200 + `refusal`，但**真实原因要进 trace**。

    原来 `_answer` 是个裸 `except Exception:`——连 `exc` 都没绑名字，异常信息
    一点没留下。接口确实还是 200，可 trace 里只有一句"抱歉，我暂时无法回答"，
    兜底答复把真实原因整个盖住了：线上遇到问题只能靠猜。
    """
    from kbqa import answerer as answerer_module

    def boom(self, plan, trace=None):
        raise RuntimeError("模拟的内部故障")

    monkeypatch.setattr(answerer_module.Answerer, "answer", boom)

    response = client.post(
        "/api/chat", json={"session_id": "boom", "question": "7 月的净营业额是多少？"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer_type"] == "refusal"
    assert body["answer"].strip()

    trace = client.get("/api/trace/%s" % body["trace_id"]).json()
    assert trace["errors"], "真实原因必须记进 trace"
    error = trace["errors"][0]
    assert error["type"] == "RuntimeError"
    assert "模拟的内部故障" in error["message"]
    assert error["traceback"].strip()
