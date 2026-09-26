"""数字守卫的白名单收得太宽，H02 实测漏了两次。

守卫的职责写在 `SYSTEM_PROMPT` 第 1 条里：经营数字一律查库，**不要心算**。
live 路径靠 `LiveEngine._finalise()` 执行这条——模型答案里出现白名单外的数字，
整段换成按工具结果渲染的模板。

H02「618 当天 S02 的牛肉poke 卖了多少份？达到目标了吗？」连着漏了两次，
两次都不是守卫本身写错了，是"什么算合法数字"这份白名单收进来的东西不对：

* 模型写「完成率约 **104%**」——125/120，正是心算。104 本该被拦下，
  却因为检索到的 KB-033 里有电话号码 `021-5555-0104`，被当成了合法数字。
* 模型转述了店长周报里那个不靠谱的估算 **150**。KB-050 在检索结果里，
  于是 150 也算"合法"。可 KB-001 §5.2 明写周报、纪要里的数字是人工估算、
  不能当答案，这条规则代码里另外三处（`answerer` / `hybrid` / `retriever`）
  都执行了，**只有白名单这一处漏了**。

两条一起漏的后果不是稳定扣分，是**抽奖**：模型这次检索到哪几篇文档，
决定了守卫开不开火。同一份代码、同一个问题，相隔 11 分钟两次跑出相反的结果
（trace `t-20260901-0001` 开火、`t-20260901-0021` 没开火），
而 55 题的历史归档里，H02 五次全过靠的都是模板——也就是五次全开火。
"""

from __future__ import annotations

from types import SimpleNamespace

from kbqa.live import LiveEngine, _numbers_in
from kbqa.planner import Plan


class _Index:
    """只带 `_allowed_numbers` 会读到的那两个属性，不必真起一套索引。"""

    docs_meta = {
        # KB-050 的门禁写着 `type: 周报`，`Doc.estimates_only` 因此为真。
        "KB-050": {"estimates_only": True},
        "KB-023": {"estimates_only": False},
    }
    texts = {
        "KB-050": "牛肉poke 卖了大概 150 份，当天营业额我估摸着五千出头。",
        "KB-023": "当天牛肉poke 目标销量 120 份。",
    }


def _engine() -> LiveEngine:
    answerer = SimpleNamespace(retriever=SimpleNamespace(index=_Index()))
    return LiveEngine(None, answerer, lambda name, params: {}, "2026-09-01", {})


def _plan(question: str) -> Plan:
    return Plan(question=question, standalone=question, search_query=question)


# -- 一、电话号不是数量 ---------------------------------------------------------


def test_a_phone_number_is_not_a_number():
    """`021-5555-0104` 是一串电话号，不是"21、5555、104"三个数量。

    门店档案里的联系电话就这么写的。104 进了白名单，模型心算出来的 104%
    跟着被放行，守卫不开火——H02 漏掉的第一条路。整串都得掐，
    只掐前导零那两段的话 `5555` 会留下，它跟正常数量长得没有区别。
    """
    assert _numbers_in("| 联系电话 | 021-5555-0104 |") == []


def test_a_two_part_date_is_still_two_numbers():
    """两段的日期不是电话号，`2026-08` 里的年月该各自算数。

    这条是给上面那条划边界的：连字符规则一收就得收到三段以上，
    否则日期里的月份会跟着一起消失，模型答"8 月"就会被判成编数字。
    """
    assert 8.0 in _numbers_in("统计到 2026-08 为止")


def test_ordinary_numbers_still_count():
    """收窄的是"编号"，不是"数字"：真的数量一个都不能少。"""
    values = _numbers_in("净营业额 3625.00 元，有效订单 53 单，目标 120 份，完成率 104%。")
    assert [3625.0, 53.0, 120.0, 104.0] == values


# -- 二、估算文档里的数字不能当答案（KB-001 §5.2） -------------------------------


def test_numbers_from_estimate_docs_are_not_allowed():
    """检索到的周报里的数字，不该算"模型眼前有据可查"。

    模型那句"店长周报里大概 150 份"就是照着 KB-050 转述的。放行它，
    模型就会把周报的估算写进答案——而 H02 恰恰要求答案里不能出现 150。
    """
    allowed = _engine()._allowed_numbers(_plan("618 当天 S02 的牛肉poke 卖了多少份？达到目标了吗？"),
                                        [], [], {"q": [
                                            {"doc_id": "KB-050", "chunk_id": "KB-050#3",
                                             "score": 48.2, "text": _Index.texts["KB-050"]},
                                        ]})
    assert 150.0 not in allowed


def test_a_normal_doc_is_unaffected():
    """同一批检索结果里，非估算文档的数字照常放行——收窄只针对周报、纪要。"""
    allowed = _engine()._allowed_numbers(_plan("618 当天 S02 的牛肉poke 卖了多少份？达到目标了吗？"),
                                        [], [], {"q": [
                                            {"doc_id": "KB-023", "chunk_id": "KB-023#4",
                                             "score": 110.7, "text": _Index.texts["KB-023"]},
                                        ]})
    assert 120.0 in allowed


def test_a_cited_estimate_doc_does_not_whitelist_its_numbers():
    """点名引用也不行。

    白名单除了看检索结果，还看模型点名的文档（`_citations()` 从整篇文档正文
    抽数字）。模型答 H02 时确实写了 `[KB-050]`，只堵检索结果这一头，
    150 会从引用这一头原样放回来。
    """
    allowed = _engine()._allowed_numbers(_plan("618 当天 S02 的牛肉poke 卖了多少份？达到目标了吗？"),
                                        [], [{"doc_id": "KB-050", "quote": "牛肉poke 卖了大概 150 份"}])
    assert 150.0 not in allowed


# -- 三、别修过头 ---------------------------------------------------------------


def test_numbers_in_the_question_itself_are_still_allowed():
    """用户自己在问句里写的数字，不是模型编的。

    "周报里说 150 份，系统里实际是多少"这种题，150 必须留在白名单里，
    否则模型照抄一遍问句就会被判成编造数字。
    """
    plan = _plan("店长周报里说 618 那天牛肉poke 卖了 150 份，系统里实际是多少份？")
    assert 150.0 in _engine()._allowed_numbers(plan, [], [])
