"""规划器的回归测试：一句话进来，该走查数、查文档、还是两条都走。

只测**路由**这一个判断，不碰取数和作答。规划器是整条链路的岔路口，
岔错了后面再准也没用：政策题被拉去查数，会拿一个不相干的数字回答
"外卖订单多久内可以申请退款"；异常题只查文档，会把"为什么"那半答得很好、
数字那半整个丢掉。

和另外几个测试文件一个路子：不写死真实数据集的数字，用合成的目录、
商品和区间来压，只断言与内容无关的不变量。
"""

from __future__ import annotations

from datetime import date

import pytest

from kbqa.aliases import AliasTable
from kbqa.entities import Catalog
from kbqa.planner import Planner

#: 固定一个"今天"，让"现在"落在中国数据区间**之外**——
#: 这正是原始缺陷暴露出来的场景："Super Souper 现在周五晚上营业到几点"
#: 被当成问今天的数，判成"没有数据"。
TODAY = date(2026, 9, 1)
DATA_PERIOD = {"start": "2026-05-01", "end": "2026-08-31"}


@pytest.fixture
def planner() -> Planner:
    catalog = Catalog(
        stores=[
            {"store_id": "S02", "store_name": "二号店"},
            {"store_id": "S03", "store_name": "三号店"},
        ],
        products=[
            {"product_id": "P001", "product_name": "牛肉poke", "unit_price": 25.0},
            {"product_id": "P002", "product_name": "味噌拉面", "unit_price": 30.0},
        ],
        aliases=AliasTable.from_json({}),
    )
    return Planner(catalog, TODAY, DATA_PERIOD)


def _plan(planner: Planner, question: str):
    return planner.plan(question)


# -- 文档那一路 ----------------------------------------------------------------

#: 答案写在知识库里、句子里却带着"多少/多久/几"的问题。
#: "问数量"和"问数据库"不是一回事：这些问题的答案是一条**规定**，
#: 数据库里根本没有对应的表。
_DOC_SIDE = [
    ("外卖订单多久内可以申请退款？", "“多久”是政策词，答案是一条规定"),
    ("会员现在单笔充值满 500 送多少？", "句子里没有任何数据库算得出来的口子"),
    ("供应商最后赔了我们多少钱？", "金额写在断供纪要里，库里没有这张表"),
    ("员工迟到多久算一次？", "问的是制度怎么写"),
]


@pytest.mark.parametrize("question,why", _DOC_SIDE)
def test_a_question_answered_by_the_documents_stays_on_the_doc_path(
    planner: Planner, question: str, why: str
):
    """带数量词的文档题不能被"多少/多久/几"拉去查数。

    原来的实现里有一段路由覆盖：只要句子里出现"多少/多久/几"，就无条件
    把意图改成查数、把 kind 从 doc 改成 summary。它推翻了上面 `_choose_kind`
    用更完整的信息（政策词、有没有可查的口子、指标是否显式）刚做完的判断，
    结果这一整类问题都拿不到引用、答非所问。
    """
    plan = _plan(planner, question)
    assert plan.intent == "doc", "%s（%s）" % (question, why)
    assert plan.needs_docs, question
    assert not plan.needs_data, question


def test_a_policy_question_about_the_current_version_is_not_out_of_period(planner: Planner):
    """问"现在的规定"不是问"今天的数"。

    这类问题句子里有"现在"，会被解析成一个落在数据区间之外的当天区间；
    `_check_period` 于是判成"没有数据"。代码里本来有一句补救——区间还原成
    整个数据区间——但它的条件是 `intent != "data"`，而上面那段覆盖刚好把
    意图改成了 "data"，补救就永远不生效，问题被答成"数据库里没有这条"。
    """
    plan = _plan(planner, "现在的退款政策要求提前多久申请？")
    assert plan.kind != "out_of_period", plan.refusal
    assert plan.intent != "refusal", plan.refusal


# -- 两条都要的那一路 ----------------------------------------------------------

def test_asking_why_keeps_the_numbers_as_well_as_the_cause(planner: Planner):
    """"为什么…"要的是原因，但数字本身也得给。

    原来的实现把"为什么"当成"改走文档"：`intent, kind = "doc", "doc"`。
    异常题（有指标、有主体、在问原因）本该是两条路都要，被这么一压就只剩
    文档那半——"8 月 17 日到 19 日为什么一分钱都没有"只答出原因，
    不给这三天的实际数字。作答器里本来就有一段按 `asks_why` 追加原因块的
    逻辑（`_answer_data` 里），路由只需要**别把取数那半掐掉**。
    """
    plan = _plan(planner, "S02 在 8 月 17 日到 19 日为什么一分钱营业额都没有？")
    assert plan.intent == "hybrid", plan.intent
    assert plan.needs_data and plan.needs_docs


def test_a_price_question_about_a_product_keeps_both_sides(planner: Planner):
    """问商品售价：既要库里的实收价，也要文档里现行那一版的标价。"""
    plan = _plan(planner, "牛肉poke 现在卖多少钱一份？")
    assert plan.kind == "price", plan.kind
    assert plan.intent == "hybrid", plan.intent


def test_a_target_question_keeps_both_sides(planner: Planner):
    """"达到目标了吗"要的是目标值和实际值，目标值在文档里。"""
    plan = _plan(planner, "618 当天 S02 的牛肉poke 卖了多少份？达到目标了吗？")
    assert plan.kind == "target", plan.kind
    assert plan.intent == "hybrid", plan.intent


# -- 数字那一路（别改过头）------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    [
        "7 月整体的净营业额是多少？",
        "8 月一共退了多少钱？",
        "6 月的净营业额是多少？",
    ],
)
def test_a_metrics_question_still_goes_to_the_numbers(planner: Planner, question: str):
    """指标 + 时间都点到了的问句，还是要老老实实去查数。"""
    plan = _plan(planner, question)
    assert plan.intent == "data", question
    assert plan.needs_data, question
