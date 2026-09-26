#!/usr/bin/env python3
"""离线体检 `eval/my_questions.jsonl`——不连服务、不调模型、不花钱。

**为什么要有这个脚本**：题库里最容易犯的错是**静默失效**的那种。

* `doc_id` 写歪一个字母，`cite_all` 就永远红——而它看起来跟"模型答错了"一模
  一样，于是花在调检索上的时间全是白费的；
* `checks` 的键名拼错（`number_all` 少个 s），评测脚本根本不看这个键，这条检查
  **当作不存在**：题目白送分，你却以为它在看着；
* 题干里把文档编号写出来，等于把答案告诉了模型。

这三类错都不会报错，只会让分数悄悄不对。所以这里逐条挑出来，**有问题就退出码
非零**，好接进 CI。

**不检查期望值算得对不对**——那得连服务才知道，而且真算了就成了"拿服务给服务
自己判卷"。期望值应当从数据侧独立算出来（做法记在 `EVAL_REPORT.md`）。
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: `run_eval` 不是包，是 eval/ 下的一个脚本，按路径塞进 sys.path 引进来。
sys.path.insert(0, os.path.join(ROOT, "eval"))

import run_eval as R  # noqa: E402

#: 默认查自己的题库；也可以跟一个路径参数，指到别的题库上去（自测这个脚本本身
#: 就靠它——一个从不报错的校验器等于没有）。
QUESTIONS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "eval", "my_questions.jsonl")
KB_DIR = os.path.join(ROOT, "knowledge_base")

#: 问答轮次里 `run_eval` 真的会看的检查键。**照着源码抄的**，不是凭印象写的——
#: 凭印象写就会冒出"检查脚本说没事、评测脚本其实不认"的假安全。
CHAT_CHECKS = {
    "answer_type_in",
    "numbers_all", "numbers_any", "numbers_none", "numbers_none_beyond_question",
    "fact_all", "fact_any",
    "text_all", "text_any", "text_none",
    "cite_all", "cite_any", "cite_none", "cite_max",
    "evidence_required", "evidence_numbers",
    "quotes_verbatim",
}
API_EXPECT = {"net_revenue", "refund_amount", "orders", "qty", "aov"}
HEALTH_EXPECT = {"kb_docs", "valid_sales_rows"}

#: 必须写成**列表**的检查键。写成字符串不会报错，但会静默降级：`text_any: "牛肉"`
#: 会被 `run_eval` 当成逐字遍历（找"牛"或"肉"），检查实质上消失了，而报告上还是绿的。
LIST_CHECKS = {
    "answer_type_in", "text_all", "text_any", "text_none",
    "cite_all", "cite_any", "cite_none", "evidence_numbers",
    "numbers_all", "numbers_any", "numbers_none",
}

DOC_ID = re.compile(r"KB-\d+")


def doc_ids_in(item: dict) -> list[str]:
    """题目里所有指名道姓的 doc_id，按出现位置收集（含 `fact_*.docs`）。"""
    found: list[str] = []
    for key in ("gold_all", "gold_any"):
        found += [str(doc) for doc in item.get(key) or []]
    for turn in item.get("turns") or []:
        checks = turn.get("checks") or {}
        for key in ("gold_all", "gold_any", "cite_all", "cite_any", "cite_none"):
            found += [str(doc) for doc in checks.get(key) or []]
        for key in ("fact_all", "fact_any"):
            block = checks.get(key)
            if isinstance(block, dict):
                found += [str(doc) for doc in block.get("docs") or []]
    return found


def check_number_specs(problems: list[str], where: str, key: str, value) -> None:
    """数字检查项的形状。

    `numbers_none_beyond_question` **不收列表**：它收 `{min: N}` 这样的阈值字典
    （小于 min 的数字算"五家门店""四个月"这类结构性说法，不算编造），
    其余几个数字键收列表，元素是裸数字或 `{value, tol}`（tol 缺省按 0，同 run_eval）。
    """
    if key == "numbers_none_beyond_question":
        if not isinstance(value, dict) or not isinstance(value.get("min", 10), (int, float)):
            problems.append("%s %s：应该是 {min: 数字} 这样的字典" % (where, key))
        return
    if not isinstance(value, list):
        problems.append("%s %s：应该是列表" % (where, key))
        return
    for spec in value:
        if isinstance(spec, dict):
            if "value" not in spec:
                problems.append("%s %s：字典里没有 value" % (where, key))
            elif not isinstance(spec["value"], (int, float)) or isinstance(spec["value"], bool):
                problems.append("%s %s：value 不是数字（%r）" % (where, key, spec["value"]))
            elif "tol" in spec and (
                    not isinstance(spec["tol"], (int, float)) or isinstance(spec["tol"], bool)):
                problems.append("%s %s：tol 不是数字（%r）" % (where, key, spec["tol"]))
        elif not isinstance(spec, (int, float)) or isinstance(spec, bool):
            problems.append("%s %s：既不是数字也不是 {value, tol}（%r）" % (where, key, spec))


def check_shape(problems: list[str], item: dict, category: str, qid: str) -> None:
    """按**类别**核对题型。`run_eval` 是按 category 分派 runner 的，不是按形状猜的
    ——所以这里也必须按类别来，否则会把合法的题判成非法。"""
    if category == "metrics":
        request = item.get("request")
        if not isinstance(request, dict) or "path" not in request:
            problems.append("%s：metrics 类的题要带 request.path" % qid)
        expected = item.get("expect")
        if not isinstance(expected, dict) or not expected:
            problems.append("%s：metrics 类的题要带 expect，否则不产生任何检查" % qid)
        else:
            for name in expected:
                if name not in API_EXPECT:
                    problems.append("%s：expect.%s 不是指标接口的字段" % (qid, name))
    elif category == "health":
        expected = item.get("expect")
        if not isinstance(expected, dict) or not expected:
            problems.append("%s：health 类的题要带 expect" % qid)
        else:
            for name in expected:
                if name not in HEALTH_EXPECT:
                    problems.append("%s：expect.%s 不是健康检查的字段" % (qid, name))
    elif category == "retrieval":
        if not (item.get("query") or "").strip():
            problems.append("%s：retrieval 类的题没有 query" % qid)
        if not isinstance(item.get("top_k"), int):
            problems.append("%s：retrieval 类的题没有 top_k" % qid)
        if not isinstance(item.get("results_count"), int):
            problems.append("%s：retrieval 类的题没有 results_count" % qid)
        if not (item.get("gold_all") or item.get("gold_any")):
            # 只查条数不查命中谁，等于只测了"返回 5 条"，检索质量一个字节没测到。
            problems.append("%s：既没有 gold_all 也没有 gold_any，这题只测了条数" % qid)
    else:
        turns = item.get("turns")
        if not isinstance(turns, list) or not turns:
            problems.append("%s：%s 类的题要走问答，必须带 turns" % (qid, category))
            return
        for index, turn in enumerate(turns, 1):
            where = "%s 第 %d 轮" % (qid, index)
            question = (turn.get("question") or "").strip()
            if not question:
                problems.append("%s：没有 question" % where)
            elif DOC_ID.search(question):
                problems.append("%s：题干里出现了文档编号，等于把答案说出去了" % where)
            checks = turn.get("checks")
            if not isinstance(checks, dict) or not checks:
                problems.append("%s：没有 checks，这轮不产生任何检查、分数白送" % where)
                continue
            for key, spec in checks.items():
                if key not in CHAT_CHECKS:
                    problems.append("%s：检查键 %r 评测脚本不认，这条检查不会生效" % (where, key))
                    continue
                if key in LIST_CHECKS and not isinstance(spec, list):
                    problems.append("%s：%r 必须是列表，写成字符串会被逐字拆开、检查失效"
                                    % (where, key))
                    continue
                if key.startswith("numbers_"):
                    check_number_specs(problems, where, key, spec)
                if key == "evidence_required" and not isinstance(spec, bool):
                    problems.append("%s：evidence_required 应该是 true/false" % where)
                if key == "quotes_verbatim" and not isinstance(spec, bool):
                    problems.append("%s：quotes_verbatim 应该是 true/false" % where)
                if key == "answer_type_in":
                    for name in spec or []:
                        if name not in R.ANSWER_TYPES:
                            problems.append("%s：answer_type %r 不是合法取值" % (where, name))
                if key == "cite_max" and not isinstance(spec, int):
                    problems.append("%s：cite_max 应该是整数" % where)


def main() -> int:
    problems: list[str] = []
    try:
        questions = R.load_questions(QUESTIONS)
    except SystemExit as exc:                       # 题库文件本身的 JSON 就坏了
        print("题库读不了：%s" % exc)
        return 1
    if not questions:
        print("题库是空的：%s" % QUESTIONS)
        return 1

    kb = R.KnowledgeBase(KB_DIR)
    if not kb.loaded:
        # 知识库不在就**当检查不了**，不当作通过——否则换个目录跑就悄悄放行了。
        print("知识库没加载起来，doc_id 那几项查不了：%s" % (kb.errors or KB_DIR))
        return 1

    seen: set[str] = set()
    points = 0
    for item in questions:
        qid = str(item.get("id") or "")
        if not qid:
            problems.append("有一条没有 id：%s" % str(item)[:80])
            continue
        if qid in seen:
            problems.append("%s：id 重复了" % qid)
        seen.add(qid)

        value = item.get("points")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            problems.append("%s：points 应该是正整数，实际 %r" % (qid, value))
        else:
            points += value

        category = item.get("category")
        if category not in R.CATEGORY_ORDER:
            problems.append("%s：类别 %r 不在 %s 里"
                            % (qid, category, "/".join(R.CATEGORY_ORDER)))
            continue                                 # 类别不认识就别再往下判形状了
        check_shape(problems, item, category, qid)

        for doc_id in doc_ids_in(item):
            if not DOC_ID.fullmatch(doc_id):
                problems.append("%s：%r 不像文档编号" % (qid, doc_id))
            elif doc_id not in kb.docs:
                problems.append("%s：知识库里没有 %s，这题会永远红" % (qid, doc_id))

    if problems:
        print("自己的评测题有 %d 个问题：" % len(problems))
        for problem in problems:
            print("  - %s" % problem)
        return 1

    counts: dict[str, int] = {}
    for item in questions:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    print("自己的评测题 %d 道，合计 %d 分，知识库 %d 份文档"
          % (len(questions), points, len(kb.docs)))
    print("  类别分布：" + "、".join("%s %d" % (key, counts[key])
                                    for key in R.CATEGORY_ORDER if key in counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
