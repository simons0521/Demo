"""调试面板的数据来源：检索明细必须真的进 trace（契约 §6）。

契约 §6 要求 trace 里看得到"检索到的每个片段的 `doc_id`、`chunk_id`、分数，
以及**哪些被过滤掉、为什么**"。模板作答路径本来就有（`answerer._search` 落痕），
**live 路径没有**——而配了 Key、评测实际走的就是 live。于是第四关的调试面板上
"这次检索到了什么"是空的。

这个文件先把缺口钉成会红的测试，再改代码。

两条**故意是绿的**（改动前后都该绿）——它们是护栏，不是待修的缺陷：

* `test_the_tool_result_shape_is_frozen`：`search_kb` 的工具结果会进到发给模型的
  `role:"tool"` 消息里，而数字白名单是从它整个 `json.dumps` 里抽的。多一个字段
  就可能让"模型编数字"的判定变样，兜底模板的触发时机跟着变，分数朝不可预测的
  方向漂。
* `test_retrieve_result_shape_is_frozen`：契约 §4 的返回形状。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from kbqa.aliases import AliasTable
from kbqa.chunker import Chunk
from kbqa.index import BM25Index
from kbqa.llm import LLMReply
from kbqa.retriever import MAX_TRACE_DROPPED, MAX_TRACE_HITS, Retriever
from kbqa.trace import Trace

#: 废止版：本来分最高，但自 2026-07-01 起被 KB-NEW 取代。
_OLD_META = {"status": "已废止", "superseded_by": "KB-NEW", "effective_from": "2026-01-01"}
_NEW_META = {"status": "现行", "effective_from": "2026-07-01"}
_META = {"KB-OLD": _OLD_META, "KB-NEW": _NEW_META}

#: 废止版的正文**必须比现行版更贴题**，这套夹具才成立：反事实要演示的正是
#: "本来该它第一，只是被版本规则挡掉了"。改这几行字之前先跑
#: `test_counterfactual_rank_...`——`的` 一多（"苹果**的**退款"）bigram 就从
#: `果退` 变成 `果的`/`的退`，废止版反而拼不过现行版，名次掉到 2。
_PAIRS = [
    ("KB-OLD", 1, "苹果退款时限 7 天，苹果退款规则见附件。"),
    ("KB-NEW", 1, "苹果的退款时限说明，苹果退款规则补充条款。"),
    ("KB-C", 1, "苹果的陈列要求，苹果摆放位置在货架第二层。"),
]


def _index(pairs, meta=None) -> BM25Index:
    chunks = [
        Chunk(doc_id=doc_id, chunk_id="%s#%d" % (doc_id, number), text=text, source_text=text)
        for doc_id, number, text in pairs
    ]
    return BM25Index(chunks, meta or {}, AliasTable.from_json({}), "test-key")


def _retriever(pairs=_PAIRS, meta=_META) -> Retriever:
    # `meta` 默认必须是 `_META`：整个文件考的就是"废止版被过滤之后，
    # 本来会得多少分"。默认成空元数据的话，KB-OLD 根本不会被过滤，
    # 断言只会以外面的"前提不成立"红掉——看着像没实现，其实是夹具写漏了。
    return Retriever(_index(pairs, meta), date(2026, 9, 1))


class _AllowEverything(Retriever):
    """把元数据过滤整个关掉。

    这样同一篇被过滤的文档就会真的出现在结果里、真的带上分数——用它来验证
    `would_be_score` 不是"看起来合理"，而是**等于**它本来会拿到的那个分。
    """

    def _eligible(self, doc_id, as_of, store_id, historical=False):  # noqa: D102
        return None


# -- 反事实分：被过滤掉的那篇本来会得多少分 ---------------------------------------


def test_counterfactual_equals_the_score_the_doc_would_have_got():
    """把过滤关掉重跑一遍，拿到的真实分数必须与 `would_be_score` 逐位相等。

    这是整套改动里最值得写的一条测试：反事实分照抄了一遍排序公式
    （`total = raw + DOC_PRIOR * best`，再乘两个乘子），抄错一个乘子依然
    "看起来合理"。只有拿关掉过滤的检索结果去对，才能证明它是对的。
    """
    result = _retriever().search("苹果退款", top_k=5, explain=True)
    filtered = {item["doc_id"]: item for item in result.filtered}
    assert "KB-OLD" in filtered, "废止版没被过滤，这条测试的前提不成立"

    actual = _AllowEverything(_index(_PAIRS, _META), date(2026, 9, 1)).search(
        "苹果退款", top_k=5
    )
    scored = [hit.score for hit in actual.hits if hit.doc_id == "KB-OLD"]
    assert scored, "关掉过滤之后 KB-OLD 也没进结果，这条测试的前提不成立"

    assert filtered["KB-OLD"]["would_be_score"] == pytest.approx(scored[0], abs=1e-9)


def test_counterfactual_rank_says_how_high_the_filtered_doc_would_have_ranked():
    """废止版本来会排第 1——这正是这个仓库里最要命的那类 bug 的样子。

    名次拿"关掉过滤重跑"的**真实顺序**去核，不写死一个数字：写死的话，
    名次算成"片段名次"（同一篇文档的第二个高分片段也算在它前面）也照样能凑出
    同一个数，看不出来。这段代码里"文档名次"和"片段名次"差在哪，
    只有多块的文档才分得出来——所以核对的基准必须是真实结果里的顺序。
    """
    result = _retriever().search("苹果退款", top_k=5, explain=True)
    filtered = {item["doc_id"]: item for item in result.filtered}
    assert "KB-OLD" in filtered, "废止版没被过滤，这条测试的前提不成立"

    actual = _AllowEverything(_index(_PAIRS, _META), date(2026, 9, 1)).search(
        "苹果退款", top_k=5
    )
    order = [hit.doc_id for hit in actual.hits if not hit.padded]
    assert "KB-OLD" in order, "关掉过滤之后 KB-OLD 也没进结果，这条测试的前提不成立"

    assert filtered["KB-OLD"]["would_be_rank"] == order.index("KB-OLD") + 1
    assert filtered["KB-OLD"]["would_be_rank"] == 1, "废止版本来会排第一，这正是原来那道缺陷的样子"


# -- explain 必须是只读的 ---------------------------------------------------------


def test_explain_does_not_change_the_results():
    """加了明细，排序与过滤结果必须一个字都不变。

    反事实要另外跑一遍打分，很容易顺手把 `scores` 写出边界。这条测试把
    "只做加法"钉死。
    """
    retriever = _retriever()
    plain = retriever.search("苹果退款", top_k=5)
    explained = retriever.search("苹果退款", top_k=5, explain=True)

    def shape(result):
        return (
            [(hit.doc_id, hit.chunk_id, hit.score, hit.padded) for hit in result.hits],
            [(item["doc_id"], item["reason"]) for item in result.filtered],
        )

    assert shape(plain) == shape(explained)


# -- 被规则挤掉的片段 -------------------------------------------------------------


def test_dropped_records_the_one_chunk_per_doc_rule():
    """`MAX_CHUNKS_PER_DOC = 1` 挤掉的片段要留下痕迹。

    主循环里那个 `continue` 是静默的：一篇文档的第二个高分片段被跳过之后，
    调试面板上看不出"它本来也在候选里"。契约 §6 的"哪些被过滤掉"不只是
    元数据过滤，也包括这种情况。
    """
    retriever = _retriever(
        [
            ("KB-A", 1, "苹果苹果苹果价格"),
            ("KB-A", 2, "苹果苹果苹果规格"),
            ("KB-B", 1, "苹果的陈列要求，苹果摆放位置，苹果面向顾客。"),
            ("KB-C", 1, "苹果的采购周期，苹果进货频次，苹果验收标准。"),
        ]
    )
    result = retriever.search("苹果", top_k=3, explain=True)
    reasons = {item["chunk_id"]: item["reason"] for item in result.dropped}
    assert "KB-A#2" in reasons, "同一篇的第二个片段被跳过了，但 dropped 里没有它"
    assert "一格" in reasons["KB-A#2"]


def test_dropped_records_what_was_cut_by_top_k():
    """取满 `top_k` 之后剩下的候选也要记一笔，否则看不出"差一点就入选了"。"""
    retriever = _retriever(
        [
            ("KB-A", 1, "苹果的售价与规格说明。"),
            ("KB-B", 1, "苹果的陈列要求与摆放位置。"),
            ("KB-C", 1, "苹果的采购周期与进货频次。"),
            ("KB-D", 1, "苹果的验收标准与损耗上限。"),
        ]
    )
    result = retriever.search("苹果", top_k=2, explain=True)
    reasons = [item["reason"] for item in result.dropped]
    assert any("top_k" in reason for reason in reasons)


# -- trace 体积 -------------------------------------------------------------------


def test_as_trace_payload_stays_small():
    """面板要能加载得动：明细再多也不能把 trace 撑成几兆。

    评测脚本对响应有 2 MB 上限（`eval/README.md`），这里按更严的 64 KB 卡。

    语料**故意造得比真实知识库大**（真实是 35 篇 / 204 块，这里是 40 篇 / 320 块），
    而且要一次取满：三个截断上限（片段 20 条、丢弃 30 条、正文 240 字）一个都
    没踩到的话，这条测试就等于没测——真实语料上一跑就是几兆。
    """
    pairs = [
        ("KB-%03d" % doc, number, "苹果退款规则第 %d 条，苹果时限说明。" % number)
        for doc in range(40)
        for number in range(1, 9)
    ]
    retriever = _retriever(pairs, {})
    payload = retriever.search("苹果退款", top_k=len(pairs), explain=True).as_trace()

    size = len(json.dumps(payload, ensure_ascii=False))
    assert size < 64 * 1024, "trace 明细 %d 字节，面板加载不动" % size
    assert payload["hits"][0]["text"], "面板要显示片段正文，hits 里得带上它"
    assert payload["hits_total"] == 40, "每篇一格，40 篇应该正好 40 条命中"
    assert len(payload["hits"]) <= MAX_TRACE_HITS, "命中片段没有按上限截断"
    assert len(payload["dropped"]) <= MAX_TRACE_DROPPED, "被丢弃的片段没有按上限截断"
    assert payload["dropped_total"] == 40 * 7, "总数要报全量，不能只报截断后的条数"


# -- live 路径真的把明细写进 trace 了 ----------------------------------------------


class _ScriptedClient:
    """第一轮按模型的习惯去检索，第二轮给出不带数字的正文。

    正文**故意不含数字**：数字校验是另一条链路的事，这里要压的是
    "live 路径有没有把检索明细记进 trace"，别让数字校验把测试带偏。
    """

    def __init__(self) -> None:
        self.rounds = 0

    def chat_with_retry(self, messages, tools=None, budget=None, on_call=None) -> LLMReply:
        self.rounds += 1
        if self.rounds == 1:
            call = {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "search_kb",
                    "arguments": json.dumps({"query": "苹果退款", "top_k": 3}, ensure_ascii=False),
                },
            }
            return LLMReply(
                message={"role": "assistant", "content": "", "tool_calls": [call]},
                finish_reason="tool_calls",
                content="",
                tool_calls=[call],
                elapsed=0.0,
            )
        return LLMReply(
            message={"role": "assistant", "content": "已按检索到的原文回答。"},
            finish_reason="stop",
            content="已按检索到的原文回答。",
            tool_calls=[],
            elapsed=0.0,
        )


def test_the_live_path_records_the_retrieval_detail(tmp_path, monkeypatch):
    """配了 Key 时走的是 live：这条路径的 trace 里也必须有检索明细。

    **不能**用 `client` 夹具：它把三个 LLM 变量删掉，而且 `server._service`
    是模块级单例、建好之后就不再读环境变量了。所以这里自己建一个 `Service`，
    并直接替掉 `LLMClient`（真发 HTTP 会打到一个不存在的地址上）。
    """
    monkeypatch.setenv("VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/not-a-real-endpoint")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    from kbqa import service as service_module

    monkeypatch.setattr(service_module, "LLMClient", lambda *a, **kw: _ScriptedClient())

    service = service_module.Service()
    payload = service.chat(None, "外卖订单多久内可以申请退款？")
    trace = service.get_trace(payload["trace_id"])

    searches = [step for step in trace["steps"] if step["step"] == "search"]
    assert searches, "live 路径的 trace 里没有 search 步骤：调试面板看不到检索"

    # 用模型实际给的那个查询词认领：模板兜底那条路径记的是规划器改写过的问句，
    # 两者不同，所以"query 等于 苹果退款"只可能来自 run_tool。
    from_tool = [step for step in searches if step["detail"]["query"] == "苹果退款"]
    assert from_tool, "search 步骤不是工具调用产生的那个：%r" % [s["detail"]["query"] for s in searches]

    hits = from_tool[0]["detail"]["hits"]
    assert hits, "检索明细里没有任何片段"
    for key in ("doc_id", "chunk_id", "score"):
        assert key in hits[0], "检索明细缺字段 %s" % key


def test_run_tool_records_the_search_step_only_when_given_a_trace():
    """没传 trace 就不记：`/api/retrieve` 之类的调用不该在别人身上留痕。"""
    from kbqa.service import Service

    service = Service()
    without = Trace(trace_id="t-1", question="")
    service.run_tool("search_kb", {"query": "退款", "top_k": 3})
    assert [step for step in without.steps if step["step"] == "search"] == []

    with_trace = Trace(trace_id="t-2", question="")
    service.run_tool("search_kb", {"query": "退款", "top_k": 3}, trace=with_trace)
    assert [step for step in with_trace.steps if step["step"] == "search"]


# -- 护栏（改动前后都该是绿的） ---------------------------------------------------


def test_the_tool_result_shape_is_frozen():
    """`search_kb` 的工具结果会原样进到发给模型的消息里。

    数字白名单是把整个工具结果 `json.dumps` 之后抽出来的，多一个字段就可能
    改变"模型有没有编数字"的判定，兜底模板的触发时机跟着变——分数会朝
    不可预测的方向漂。所以这个形状**必须**冻住。
    """
    from kbqa.service import Service

    service = Service()
    result = service.run_tool("search_kb", {"query": "退款", "top_k": 3})
    assert set(result) == {"results"}
    for item in result["results"]:
        assert set(item) == {"doc_id", "chunk_id", "score", "text"}


def test_retrieve_result_shape_is_frozen():
    """契约 §4 的返回形状：`results` 里每条恰好四个字段。"""
    from kbqa.service import Service

    service = Service()
    assert set(service.retrieve("退款", 3)) == {"results"}
