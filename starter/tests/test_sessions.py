"""对话历史的回归测试：会话之间必须互不可见，追问必须接得上。

契约 §3（`/api/chat`）写明"同一个 `session_id` 的多次请求视为同一段对话，
要支持追问"。这句话有两个方向，缺一个都不成立：

* **隔离**：A 会话的历史不能被 B 会话看见。
* **贯通**：同一个会话里，上一轮得真的传到规划器手上，追问才补得全。

两个方向各测一遍。这里不写死任何数字，只断言会话行为。
"""

from __future__ import annotations

import pytest

from kbqa.sessions import SessionStore


def _turn(question: str) -> dict:
    return {"question": question, "standalone": question, "slots": {}}


# -- 隔离 -----------------------------------------------------------------------

def test_history_is_kept_per_session():
    """两个会话各说各的，谁的追问只能看到谁的上文。"""
    store = SessionStore()
    store.append("a", _turn("6 月的净营业额是多少？"))
    store.append("b", _turn("8 月一共退了多少钱？"))

    assert [t["question"] for t in store.history("a")] == ["6 月的净营业额是多少？"]
    assert [t["question"] for t in store.history("b")] == ["8 月一共退了多少钱？"]


def test_a_session_without_history_is_empty():
    store = SessionStore()
    store.append("a", _turn("6 月的净营业额是多少？"))
    assert store.history("从没出现过的会话") == []


def test_an_anonymous_request_never_shares_a_bucket():
    """没带 `session_id` 的请求不参与会话。

    宁可把它当成孤立的单轮，也不能塞进一个公共桶里——那个桶会让
    不同人的追问互相补全（"那 7 月呢"被补成上一个陌生人的话题），
    既答错，也把别人的问题带出来。
    """
    store = SessionStore()
    store.append(None, _turn("6 月的净营业额是多少？"))

    assert store.history(None) == []
    assert store.history("") == []
    assert store.history("a") == []


def test_history_survives_a_copy_being_mutated():
    """`history()` 交出去的是副本，调用方改它不该动到存着的那份。"""
    store = SessionStore()
    store.append("a", _turn("6 月的净营业额是多少？"))
    store.history("a").append(_turn("外部塞进来的"))

    assert len(store.history("a")) == 1


# -- 容量 -----------------------------------------------------------------------

def test_a_session_keeps_at_most_max_turns():
    store = SessionStore(max_turns=3)
    for number in range(5):
        store.append("a", _turn("第 %d 轮" % number))

    kept = [t["question"] for t in store.history("a")]
    assert kept == ["第 2 轮", "第 3 轮", "第 4 轮"]


def test_the_oldest_session_is_evicted_when_the_cap_is_reached():
    """`max_sessions` 得真的用上，不然没人回收，进程里会一直涨。"""
    store = SessionStore(max_sessions=2)
    store.append("a", _turn("第一段"))
    store.append("b", _turn("第二段"))
    store.append("c", _turn("第三段"))

    assert store.history("a") == []
    assert [t["question"] for t in store.history("b")] == ["第二段"]
    assert [t["question"] for t in store.history("c")] == ["第三段"]


def test_talking_again_keeps_a_session_alive():
    """刚聊过的那一段不该被后来的会话挤掉。"""
    store = SessionStore(max_sessions=2)
    store.append("a", _turn("第一段"))
    store.append("b", _turn("第二段"))
    store.append("a", _turn("又聊一句"))
    store.append("c", _turn("第三段"))

    assert [t["question"] for t in store.history("a")] == ["第一段", "又聊一句"]
    assert store.history("b") == []


def test_clear_can_target_one_session():
    store = SessionStore()
    store.append("a", _turn("第一段"))
    store.append("b", _turn("第二段"))

    store.clear("a")
    assert store.history("a") == []
    assert len(store.history("b")) == 1

    store.clear()
    assert store.history("b") == []


# -- 贯通：接口这一层 ------------------------------------------------------------

_FIRST = "6 月的净营业额是多少？"
_FOLLOW_UP = "那 7 月呢？"


def test_a_follow_up_uses_the_previous_turn(client):
    """同一段会话里，追问要能接上上一轮的话题。"""
    first = client.post("/api/chat", json={"session_id": "s-1", "question": _FIRST})
    assert first.status_code == 200

    second = client.post("/api/chat", json={"session_id": "s-1", "question": _FOLLOW_UP})
    assert second.status_code == 200
    body = second.json()
    assert body["answer_type"] != "clarify", body["answer"]

    trace = client.get("/api/trace/%s" % body["trace_id"]).json()
    steps = {step["step"]: step["detail"] for step in trace["steps"]}
    standalone = steps["plan"]["standalone_question"]
    assert "净营业额" in standalone, standalone


def test_two_sessions_never_see_each_other(client):
    """换一个 `session_id` 问同一句追问：没有上文，就该老老实实反问。"""
    client.post("/api/chat", json={"session_id": "s-1", "question": _FIRST})
    other = client.post("/api/chat", json={"session_id": "s-2", "question": _FOLLOW_UP})

    assert other.json()["answer_type"] == "clarify"
    assert "上文" in other.json()["answer"] or "完整" in other.json()["answer"]


def test_the_same_question_without_a_session_id_is_stateless(client):
    """不带 `session_id` 时，两轮之间不共享任何东西。"""
    client.post("/api/chat", json={"question": _FIRST})
    second = client.post("/api/chat", json={"question": _FOLLOW_UP})
    assert second.json()["answer_type"] == "clarify"
