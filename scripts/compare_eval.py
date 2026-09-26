#!/usr/bin/env python3
"""比两次评测的结果，**掉分就退出码非零**——用来把"没伤到以前的分数"变成一条可跑的命令。

用法：

    python3 scripts/compare_eval.py eval/reports/2026-09-26_before eval/reports/2026-09-26_after

两个参数都能给目录（里面有 `report.json`）或直接给 `report.json` 文件。
基线在前、新的在后。

输出四块：

1. 总分与通过数的涨跌；
2. `per_category` 逐类涨跌（哪一类掉了，一眼看到）；
3. **新红的题**：上次过、这次没过，连同每条失败检查的期望值与实际值——
   只有这一块能告诉你"到底哪一步坏了"，光看总分掉 3 分是不够的；
4. 新绿的题（顺手记一下）。

两个刻意的设计：

* **比对范围取两份报告的交集**，按题目 `id` 算。`run_eval.py` 的 `--only` 是单选，
  一份 55 题的完整报告和一份只跑 `retrieval` 的报告直接比总分是没有意义的
  （那会"掉"92 分），但只比两边都有的题就能比。落在交集外的题会列出来提醒。
* **白名单只放"已知会红、且已经认了"的题**，默认是空的。它存在的意义是让这条
  命令能被接进流水线而不至于天天误报；往里面加题要跟加注释一样慎重，写明为什么。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

#: 已知会红的题号 -> 为什么认了。**默认空**：没有白名单的日子才是好日子。
KNOWN_FAILURES: dict[str, str] = {}

MARK = {"up": "↑", "down": "↓", "same": "="}

#: 分类表的排列顺序，只是为了好看；报告里有而这里没有的类别排在后面。
CATEGORY_HINT = ["metrics", "retrieval", "data", "doc", "version",
                 "hybrid", "multi_turn", "refusal", "safety", "health"]


def load_report(path: str) -> dict:
    """参数给目录就读目录下的 report.json，给文件就直接读。"""
    if os.path.isdir(path):
        path = os.path.join(path, "report.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def index_questions(report: dict) -> dict[str, dict]:
    return {q["id"]: q for q in report.get("questions") or []}


def delta(before: float, after: float) -> str:
    return "%s%.2f" % (MARK["up"] if after > before else MARK["down"] if after < before else MARK["same"],
                       after - before)


def failed_checks(turn: dict) -> list[dict]:
    return [c for c in turn.get("checks") or [] if not c.get("passed")]


def describe_turn(turn: dict) -> list[str]:
    """一条失败轮次展开成人话：哪一轮问的什么、哪几条检查没过、期望与实际各是什么。"""
    lines = []
    for check in failed_checks(turn):
        lines.append("      · %s：%s" % (check.get("name"), check.get("reason") or "未通过"))
        if "expected" in check:
            lines.append("        期望 %s" % json.dumps(check["expected"], ensure_ascii=False))
            lines.append("        实际 %s" % json.dumps(check["actual"], ensure_ascii=False))
    return lines


def compare(before: dict, after: dict, out=sys.stdout) -> int:
    qb, qa = index_questions(before), index_questions(after)
    common = [qid for qid in qb if qid in qa]
    only_before = [qid for qid in qb if qid not in qa]
    only_after = [qid for qid in qa if qid not in qb]

    def points(report, qids):
        return sum(q["points"] for q in index_questions(report).values() if q["id"] in qids)

    def earned(report, qids):
        return sum(q["earned"] for q in index_questions(report).values() if q["id"] in qids)

    def passed(report, qids):
        return sum(1 for q in index_questions(report).values() if q["id"] in qids and q["passed"])

    if not common:
        print("两份报告没有一道共同的题，比不了。", file=out)
        return 2

    print("比对范围：%d 道共同题" % len(common), file=out)
    def list_ids(qids: list[str]) -> str:
        """题号列表太长就不铺开——子集比对每次都会砍掉几十道，铺开来是噪音。
        真要看全的，上面的"比对范围"已经说了总数，题号去报告里看。"""
        return " ".join(qids) if len(qids) <= 12 else " ".join(qids[:12]) + " …（%d 道）" % len(qids)

    if only_before or only_after:
        print("  只在基线里（%d）：%s" % (len(only_before), list_ids(only_before) or "—"), file=out)
        print("  只在新报告里（%d）：%s" % (len(only_after), list_ids(only_after) or "—"), file=out)
        print("  这两部分不参与比对——`--only` 跑出来的子集报告就是靠这个跟完整报告比的。",
              file=out)
    print(file=out)

    p_before, p_after = points(before, common), points(after, common)
    e_before, e_after = earned(before, common), earned(after, common)
    n_before, n_after = passed(before, common), passed(after, common)
    ratio_before = e_before / p_before * 100 if p_before else 0.0
    ratio_after = e_after / p_after * 100 if p_after else 0.0

    print("总分   %.2f / %.2f（%.2f%%）  →  %.2f / %.2f（%.2f%%）  %s"
          % (e_before, p_before, ratio_before, e_after, p_after, ratio_after,
             delta(ratio_before, ratio_after) + " 个百分点"), file=out)
    print("通过   %d / %d  →  %d / %d  %s"
          % (n_before, len(common), n_after, len(common), delta(n_before, n_after)), file=out)
    print(file=out)

    # 只列**参与比对的题**所在的类别。拿两份报告的 per_category 求并集会把没跑的
    # 类别也列进来（子集报告里那些类目是 0），显示成"↓-16.00"——看着像掉了 16 分，
    # 其实那一类这次压根没跑，是句纯粹的谎话。
    cat_of = {q["id"]: q["category"] for q in qb.values() if q["id"] in set(common)}
    cats = [c for c in CATEGORY_HINT if c in set(cat_of.values())]
    cats += sorted(set(cat_of.values()) - set(cats))
    print("分类：", file=out)
    print("  %-12s %8s %8s %8s   %s" % ("类别", "基线", "现在", "涨跌", "通过"), file=out)
    for cat in cats:
        b = (before.get("per_category") or {}).get(cat) or {}
        a = (after.get("per_category") or {}).get(cat) or {}
        print("  %-12s %7.2f %8.2f %8s   %d/%d → %d/%d"
              % (cat, b.get("earned", 0.0), a.get("earned", 0.0),
                 delta(b.get("earned", 0.0), a.get("earned", 0.0)),
                 b.get("passed", 0), b.get("questions", 0),
                 a.get("passed", 0), a.get("questions", 0)), file=out)
    print(file=out)

    turned_red = [qid for qid in common if qb[qid]["passed"] and not qa[qid]["passed"]]
    turned_green = [qid for qid in common if not qb[qid]["passed"] and qa[qid]["passed"]]

    if turned_green:
        print("新绿（上次没过、这次过了）：%s" % " ".join(turned_green), file=out)
        print(file=out)

    if turned_red:
        print("新红 %d 道：" % len(turned_red), file=out)
        for qid in turned_red:
            q = qa[qid]
            print("  %s（%s，%s 分）：" % (qid, q["category"], q["points"]), file=out)
            for turn in q.get("turns") or []:
                if turn.get("passed"):
                    continue
                print("    问：%s" % turn.get("question"), file=out)
                print("    答（%s）：%s" % (turn.get("answer_type"),
                                          (turn.get("answer") or "")[:200]), file=out)
                for line in describe_turn(turn):
                    print(line, file=out)
                if turn.get("trace_id"):
                    print("    trace：%s" % turn["trace_id"], file=out)
    else:
        print("没有新红的题。", file=out)

    waived = [qid for qid in turned_red if qid in KNOWN_FAILURES]
    new_failures = [qid for qid in turned_red if qid not in KNOWN_FAILURES]

    # 判定用的分数**把白名单的题两边都扣掉**。只豁免"新红名单"、却仍拿含它的总分
    # 去比，白名单就等于没写：那几道题的分差会照样把结论顶成"掉分"。
    counted = [qid for qid in common if qid not in KNOWN_FAILURES]
    c_before, c_after = earned(before, counted), earned(after, counted)
    if waived:
        print(file=out)
        print("其中已在白名单里、不算掉分的 %d 道：" % len(waived), file=out)
        for qid in waived:
            print("  %s：%s" % (qid, KNOWN_FAILURES[qid]), file=out)
        print("  判定用的分数把这几道两边都扣掉了：%.2f → %.2f"
              % (c_before, c_after), file=out)

    if new_failures or c_after < c_before - 1e-9:
        print(file=out)
        print("结论：掉分了。", file=out)
        return 1
    print(file=out)
    print("结论：没有掉分。", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="比两份评测报告，掉分则退出码非零")
    parser.add_argument("before", help="基线报告：目录或 report.json 路径")
    parser.add_argument("after", help="新的报告：目录或 report.json 路径")
    parser.add_argument("--allow", default="",
                        help="额外的白名单题号，逗号分隔（默认只有脚本里写死的那几个）")
    args = parser.parse_args(argv)

    for qid in filter(None, (x.strip() for x in args.allow.split(","))):
        KNOWN_FAILURES.setdefault(qid, "用 --allow 在命令行上临时放行")

    for label, path in (("基线", args.before), ("新报告", args.after)):
        target = os.path.join(path, "report.json") if os.path.isdir(path) else path
        if not os.path.isfile(target):
            print("找不到%s报告：%s" % (label, target), file=sys.stderr)
            return 2
    before, after = load_report(args.before), load_report(args.after)
    print("基线  %s（%s）" % (before.get("generated_at"), before.get("only") or "全部类别"))
    print("新报告 %s（%s）" % (after.get("generated_at"), after.get("only") or "全部类别"))
    print()
    return compare(before, after)


if __name__ == "__main__":
    sys.exit(main())
