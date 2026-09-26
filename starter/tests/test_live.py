"""live 模式引擎的回归测试：工具调用不收敛时得有出路。

引擎最多让模型调几轮工具，到点还没出文本就按"没收敛"拒答。问题在于
原来**最后一轮照样把工具传下去**：模型只要还在调，这一轮的结果就被执行掉、
然后整题拒答——用户已经等了几十秒、数据也查到了，最后却什么也没拿到。

这里用一个"只要给工具就接着调、不给工具才回答"的假客户端来压，
真实模型的典型行为就是这样。
"""

from __future__ import annotations

from kbqa.llm import LLMReply
from kbqa.live import MAX_TOOL_ROUNDS, LiveEngine
from kbqa.planner import Plan
from kbqa.trace import Trace

QUESTION = "7 月的净营业额是多少？"
DATA_PERIOD = {"start": "2026-05-01", "end": "2026-08-31"}

#: 一句**不带数字**的最终回答。数字是引擎要校验的东西（工具结果里对不上的
#: 会打回模板兜底），这里要压的是"轮次"，别让数字校验把测试带偏。
REPLY = "已经查到结果了，请见上面的数据。"


class _ToolHungryClient:
    """给了工具就继续调，不给工具才开口回答。"""

    def __init__(self) -> None:
        self.tools_seen: list = []

    def chat_with_retry(self, messages, tools=None, budget=None, on_call=None) -> LLMReply:
        self.tools_seen.append(tools)
        if tools:
            call = {
                "id": "call-%d" % len(self.tools_seen),
                "type": "function",
                "function": {"name": "query_metrics", "arguments": "{}"},
            }
            return LLMReply(
                message={"role": "assistant", "content": "", "tool_calls": [call]},
                finish_reason="tool_calls",
                content="",
                tool_calls=[call],
                elapsed=0.0,
            )
        return LLMReply(
            message={"role": "assistant", "content": REPLY},
            finish_reason="stop",
            content=REPLY,
            tool_calls=[],
            elapsed=0.0,
        )


class _StubAnswerer:
    """模板兜底不该被走到；真走到了就说明数字校验把回答打回来了。"""

    def answer(self, plan, trace=None):
        raise AssertionError("不该走到模板兜底：末轮应该拿到模型自己的回答")


def _engine(client) -> LiveEngine:
    return LiveEngine(
        client,
        _StubAnswerer(),
        lambda name, params: {"net_revenue": 123.0},
        "2026-09-01",
        DATA_PERIOD,
        budget=150.0,
    )


def _plan() -> Plan:
    return Plan(question=QUESTION, standalone=QUESTION, search_query=QUESTION)


def test_the_last_round_is_asked_without_tools():
    """最后一轮不再给工具：模型没有新东西可查，再调一次只会把整题拖成拒答。

    不给 `tools`，`reply.tool_calls` 必然是空的，直接进 `_finalise`——
    这是确定的，不用赌模型听不听话。
    """
    client = _ToolHungryClient()
    answer = _engine(client).answer(_plan(), Trace("t-1", QUESTION), [])

    assert answer.answer == REPLY
    assert len(client.tools_seen) == MAX_TOOL_ROUNDS + 1
    assert client.tools_seen[-1] is None, "最后一轮不该再带工具"
    assert all(tools for tools in client.tools_seen[:-1]), "前面几轮要正常给工具"


def test_a_model_that_answers_right_away_is_not_delayed():
    """一上来就出文本的模型：一轮就结束，不该被末轮逻辑碰到。"""

    class _DirectClient:
        def __init__(self) -> None:
            self.tools_seen: list = []

        def chat_with_retry(self, messages, tools=None, budget=None, on_call=None):
            self.tools_seen.append(tools)
            return LLMReply(
                message={"role": "assistant", "content": REPLY},
                finish_reason="stop",
                content=REPLY,
                tool_calls=[],
                elapsed=0.0,
            )

    client = _DirectClient()
    answer = _engine(client).answer(_plan(), Trace("t-2", QUESTION), [])

    assert len(client.tools_seen) == 1
    assert client.tools_seen[0], "第一轮要正常带上工具"
    assert answer.answer == REPLY
