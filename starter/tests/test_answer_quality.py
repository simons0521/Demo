"""作答链路的回归测试：数字校验的误伤、工具标记泄漏、兜底答案的形状。

这三件事都出在"模型说完了之后引擎怎么收尾"这一段，症状互不相干，
根子上是同一类毛病：**引擎拿着一份不全的信息做判断，然后判错了方向。**

* `_numbers_in` 把范围连字符读成负号 → 模型答对的数字被判成"编造"；
* 白名单只收工具结果，不收**模型眼前看到的检索结果** → 引用文档也算编造；
* 收尾时只看 `tool_calls` 空不空 → DeepSeek 写成正文的工具标记成了最终答案；
* 兜底答案把整篇原文拼在 200 字正文前面 → 判回来的回答反而更难读。

真实数据集一直在换，所以除了"整篇文档不该被倒出来"那一条，
其余全部用合成数据压行为，不写死任何真实数字。
"""

from __future__ import annotations

from datetime import date

import pytest

from kbqa.answerer import Answerer
from kbqa.entities import Catalog
from kbqa.index import build_index
from kbqa.llm import LLMReply
from kbqa.live import LiveEngine, _numbers_in
from kbqa.planner import Plan
from kbqa.retriever import Retriever
from kbqa.schemas import Answer
from kbqa.trace import Trace

TODAY = date(2026, 9, 1)
DATA_PERIOD = {"start": "2026-05-01", "end": "2026-08-31"}

#: 尖括号拆开写：这里要的是一段**字面量**，不是 HTML 标签。
_LT, _GT = chr(0x3C), chr(0x3E)

#: DeepSeek 把它自己的工具调用标记当正文发回来时的样子。
#: `finish_reason` 是正常的 `stop`、`tool_calls` 是空的——只看这两个字段
#: 会以为模型好好回答了。V01、H06 实测就是这么把一段 XML 交给用户的。
DSML_REPLY = "\n".join(
    [
        "让我再查一下。",
        _LT + "｜｜DSML｜｜ calls" + _GT,
        _LT + '｜｜DSML｜｜ invoke name="search_kb"' + _GT,
        _LT + '｜｜DSML｜｜ parameter name="query" string="true"' + _GT + "退款政策",
        _LT + "/｜｜DSML｜｜ parameter" + _GT,
        _LT + "/｜｜DSML｜｜ invoke" + _GT,
        _LT + "/｜｜DSML｜｜ calls" + _GT,
    ]
)


# -- 假件 -------------------------------------------------------------------------

class _ScriptedClient:
    """按台本一轮一轮地回：前几轮调工具，最后一轮给最终回答。"""

    def __init__(self, final: str, calls: list[dict] | None = None) -> None:
        self.final = final
        self.calls = calls or []
        self.rounds = 0

    def chat_with_retry(self, messages, tools=None, budget=None, on_call=None) -> LLMReply:
        if self.rounds < len(self.calls):
            call = self.calls[self.rounds]
            self.rounds += 1
            return LLMReply(
                message={"role": "assistant", "content": "", "tool_calls": [call]},
                finish_reason="tool_calls",
                content="",
                tool_calls=[call],
                elapsed=0.0,
            )
        self.rounds += 1
        return LLMReply(
            message={"role": "assistant", "content": self.final},
            finish_reason="stop",
            content=self.final,
            tool_calls=[],
            elapsed=0.0,
        )


class _RecordingAnswerer:
    """模板兜底：记下来自己有没有被叫到，再给一个明显好认的答案。"""

    TEMPLATE = "（模板兜底答案）"

    def __init__(self) -> None:
        self.calls = 0

    def answer(self, plan, trace=None) -> Answer:
        self.calls += 1
        return Answer(answer=self.TEMPLATE, answer_type="data")


def _call(name: str, arguments: str) -> dict:
    return {"id": "c-%s" % name, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _engine(client, answerer=None, run_tool=None) -> LiveEngine:
    return LiveEngine(
        client,
        answerer or _RecordingAnswerer(),
        run_tool or (lambda name, params: {"net_revenue": 123.0}),
        "2026-09-01",
        DATA_PERIOD,
        budget=150.0,
    )


def _plan(question: str) -> Plan:
    return Plan(question=question, standalone=question, search_query=question)


def _kinds(trace) -> list[str]:
    return [step["step"] for step in trace.steps]


# -- 范围连字符不是负号 -----------------------------------------------------------

def test_a_range_hyphen_is_not_read_as_a_minus_sign():
    """`8 月 10 日-31 日` 里的 `-31` 不是数字。

    原正则 `-?\\d+` 只要看到连字符就当成负号，于是范围写法凭空多出一个负数。
    这个数字当然是"在工具结果里找不到"的——因为压根不存在。
    """
    values = _numbers_in("8 月 10 日-31 日")

    assert -31.0 not in values
    assert 31.0 in values
    assert values[:3] == [8.0, 10.0, 31.0]


def test_a_real_minus_still_counts():
    """真的负号要留住：退款行、环比跌幅都是负的，丢掉它们就成了另一种漏检。"""
    assert -31.0 in _numbers_in("环比 -31 元")
    assert -31.0 in _numbers_in("-31")


def test_a_date_is_not_read_as_a_negative_month():
    """`2026-08-31` 的年月日早就在拆，但零散的 `2026-08` 同样是范围写法。"""
    values = _numbers_in("统计到 2026-08 为止")

    assert -8.0 not in values
    assert 2026.0 in values and 8.0 in values


def test_a_date_range_in_the_answer_does_not_trigger_the_fallback():
    """模型答的是对的，别把它的回答换成兜底模板。

    这一条照着 C07 的实况写：模型的回答里有一个日期范围，
    范围连字符读出来的负数对不上工具结果，整段正确的回答就被判成"编了数字"，
    换成了一份把整篇文档倒出来的兜底答案。
    """
    answerer = _RecordingAnswerer()
    client = _ScriptedClient(
        "8 月 9 日-31 日一共 123 元。", calls=[_call("query_metrics", "{}")]
    )
    trace = Trace("t-range", "8 月 9 日的净营业额是多少？")

    answer = _engine(
        client, answerer, run_tool=lambda name, params: {"net_revenue": 123.0, "days": 31}
    ).answer(_plan("8 月 9 日的净营业额是多少？"), trace, [])

    # 8、9 来自问句，123、31 来自工具结果——四类数字都该放行，
    # 唯一多出来的只有范围连字符读出来的那个 -31。
    assert answer.answer == "8 月 9 日-31 日一共 123 元。"
    assert answerer.calls == 0
    assert "number_check_failed" not in _kinds(trace)


# -- 检索结果里的数字也算数 -------------------------------------------------------

def test_numbers_from_retrieved_documents_are_allowed():
    """模型引用检索到的文档时，里面的数字是**它眼前的内容**，不是编的。

    `retrieved` 一直都在收集，只是从来没人用它：模型引用了检索命中的
    另一篇文档（不是代码挑出来做引用的那篇），数字就被判成编造。
    """
    answerer = _RecordingAnswerer()
    client = _ScriptedClient(
        "按规定满 500 送 77 元。",
        calls=[_call("search_kb", '{"query": "储值赠送"}')],
    )
    trace = Trace("t-retrieved", "储值赠送规则是什么？")

    answer = _engine(
        client,
        answerer,
        run_tool=lambda name, params: {
            "results": [{"doc_id": "KB-900", "text": "单笔充值满 500 元赠送 77 元。"}]
        },
    ).answer(_plan("储值赠送规则是什么？"), trace, [])

    assert answer.answer == "按规定满 500 送 77 元。"
    assert answerer.calls == 0


def test_a_number_nobody_saw_is_still_caught():
    """白名单放宽的是"看过的"，不是全部：凭空冒出来的数字照样打回模板。

    这是防编造那道闸，放宽它的前提是它还得拦得住真编的数字。
    """
    answerer = _RecordingAnswerer()
    client = _ScriptedClient("净营业额是 999999 元。")
    trace = Trace("t-made-up", "7 月的净营业额是多少？")

    answer = _engine(
        client, answerer, run_tool=lambda name, params: {"net_revenue": 123.0}
    ).answer(_plan("7 月的净营业额是多少？"), trace, [])

    assert answer.answer == _RecordingAnswerer.TEMPLATE
    assert answerer.calls == 1
    assert "number_check_failed" in _kinds(trace)


# -- 工具标记不能当答案 -----------------------------------------------------------

def test_tool_call_markup_never_reaches_the_user():
    """没有下发 `tools` 的那一轮，模型会把工具调用写成正文里的标记。

    `finish_reason="stop"`、`tool_calls=[]`，只看这两个字段会以为它答完了，
    于是这段 XML 成了最终答案。它不是回答，是"我还想查"。
    """
    answerer = _RecordingAnswerer()
    client = _ScriptedClient(DSML_REPLY, calls=[_call("query_metrics", "{}")])
    trace = Trace("t-dsml", "7 月的净营业额是多少？")

    answer = _engine(client, answerer).answer(_plan("7 月的净营业额是多少？"), trace, [])

    assert "DSML" not in answer.answer
    assert answer.answer == _RecordingAnswerer.TEMPLATE
    assert answerer.calls == 1, "该走模板兜底，而不是把标记原样交出去"


def test_a_normal_answer_still_gets_through():
    """别把兜底做成常态：正常回答必须原样保留。"""
    answerer = _RecordingAnswerer()
    client = _ScriptedClient("净营业额是 123 元。", calls=[_call("query_metrics", "{}")])
    trace = Trace("t-ok", "7 月的净营业额是多少？")

    answer = _engine(client, answerer).answer(_plan("7 月的净营业额是多少？"), trace, [])

    assert answer.answer == "净营业额是 123 元。"
    assert answerer.calls == 0


# -- 兜底答案不该是整篇原文 -------------------------------------------------------

#: 一篇"答案只在其中一段"的合成文档。其余段落写得足够长，
#: 长到如果被拼进答案里，字数上一定看得出来。
_LONG_DOC = """# KB-900 门店值班与交接规定

## 一、适用范围
本规定适用于全部直营门店的早班、中班与晚班交接，含节假日值班安排。门店排班由店长统筹，
值班表提前一周张贴在员工通道，临时调班需经店长书面确认，不得私下换班。

## 二、交接流程
交接时双方须当面清点备用金、核对当日订单与退款记录，并在交接本上签字。备用金差额超过
50 元的，须当场查明原因并报告区域经理，不得留到次日处理。

## 三、钥匙与门禁
门店钥匙由店长与值班经理各持一套，交接时随班移交。门禁卡遗失须在 24 小时内上报总部
行政部挂失，补办期间由店长临时授权，授权记录留存备查。

## 四、异常处理
遇设备故障、客流异常或顾客投诉升级，值班经理须在 30 分钟内电话报告区域经理，
并在交接本上记录处理过程与结果。
"""


@pytest.fixture
def doc_answerer(tmp_path):
    """用临时知识库建一个真的索引——检索、挑句、引用全都走真实实现。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "KB-900_值班交接规定.md").write_text(_LONG_DOC, encoding="utf-8")
    index = build_index(kb)
    retriever = Retriever(index, TODAY)

    class _NoTools:
        def __getattr__(self, name):  # 纯文档题不该碰数据库
            raise AssertionError("文档题不该调用取数工具：%s" % name)

    return Answerer(_NoTools(), retriever, Catalog(), TODAY, DATA_PERIOD), index


def test_the_doc_answer_is_the_cited_sentence_not_the_whole_document(doc_answerer):
    """答案正文是挑出来、带引用的那几句话，不是整篇原文。

    原来返回的是 `整篇原文 + 200 字的正文`：引用本身就逐字来自文档，
    再附一份全文只是把答案撑到一千多字，评测里的 `answer_length`
    和 `number_flood` 就是被这么撑爆的（C04、S01、V03 实测）。
    """
    answerer, index = doc_answerer
    plan = Plan(
        question="门禁卡遗失要多久内上报？",
        standalone="门禁卡遗失要多久内上报？",
        search_query="门禁卡遗失上报时限",
        intent="doc",
        kind="doc",
        needs_docs=True,
    )

    answer = answerer._answer_doc(plan)

    assert answer.answer_type == "doc", answer.notes
    assert answer.citations, "得有可逐字核对的引用"
    assert len(answer.answer) < len(index.texts["KB-900"]), "答案不该是整篇原文"
    # 与问题无关的那几节不该出现在答案里。
    assert "备用金" not in answer.answer
    assert "排班" not in answer.answer


def test_the_cited_sentence_is_still_in_the_answer(doc_answerer):
    """别矫枉过正：挑出来的那句话必须还在答案里，否则就是答非所问。"""
    answerer, _ = doc_answerer
    plan = Plan(
        question="门禁卡遗失要多久内上报？",
        standalone="门禁卡遗失要多久内上报？",
        search_query="门禁卡遗失上报时限",
        intent="doc",
        kind="doc",
        needs_docs=True,
    )

    answer = answerer._answer_doc(plan)

    assert "24 小时" in answer.answer
