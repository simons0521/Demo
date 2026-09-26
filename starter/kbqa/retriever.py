"""检索：打分、按元数据过滤、取 top-k。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from .chunker import Chunk
from .entities import wants_historical
from .index import BM25Index, load_index
from .tokenizer import content_tokens, tokenize

ALIAS_WEIGHT = 0.6
#: 单字（“月”“日”“店”）在二元组的世界里基本是噪声，降权但不丢弃。
SINGLE_CHAR_WEIGHT = 0.3
YEAR_PENALTY = 0.25
FUTURE_PENALTY = 0.6
STORE_HINT_BOOST = 1.15
#: 问某个时间窗里“出了什么事”时，正好在这个窗里生效的文档最可能是答案。
WINDOW_BOOST = 1.8
#: 文档级先验：一篇文档整体命中得好，它的其它片段也更可能是答案所在。
#: 英文邮件里“赔了多少钱”的那一段本身不含任何中文查询词，靠的就是这一项。
DOC_PRIOR = 0.35
#: 别名词典本身不是答案，得压一压，不然它永远排第一。
ALIAS_DOC_PENALTY = 0.5
#: 周报、纪要里的数字是人工估的，问数字的时候给它们降点权。
ESTIMATE_DOC_PENALTY = 0.7
#: top-k 里一篇文档最多占一格：多留几篇不同的文档，比同一篇留两段有用；
#: 回答需要更多段落时另外按 doc_id 取。
MAX_CHUNKS_PER_DOC = 1

# -- 调试明细（契约 §6）的上限：面板要能加载得动，明细再多也不能撑爆 trace ---------
#: 一个片段正文在 trace 里最多留多少字。
MAX_TRACE_TEXT = 240
#: trace 里最多列几段命中片段。
MAX_TRACE_HITS = 20
#: trace 里最多列几段被丢弃的片段。
MAX_TRACE_DROPPED = 30

#: 被 `MAX_CHUNKS_PER_DOC` 挤掉。
_ONE_PER_DOC = "同一篇文档已占一格（每篇最多 1 格）"
#: 取满 top_k 之后，剩下的候选根本没被考察。
_CUT_BY_TOP_K = "已取满 top_k 条，未再考察"


def _best_by_doc(chunks, scores: dict) -> dict:
    """每篇文档的最高分。

    `DOC_PRIOR` 用的就是它。在线打分与反事实各跑一次，所以抽出来只写一遍——
    两处各写一份的话，改了一处忘了另一处，反事实分就悄悄不对了。
    """
    best: dict[str, float] = {}
    for position, score in scores.items():
        doc_id = chunks[position].doc_id
        best[doc_id] = max(best.get(doc_id, 0.0), score)
    return best


def _dropped_item(chunk: Chunk, score: float, reason: str, phase: str) -> dict:
    """被规则挤掉的片段进 trace 的形状。

    `phase` 说明它是在哪一轮被挤掉的（`rank` 排序取 top_k / `rank_tail` 取满之后的
    尾巴 / `fill` 补齐），排查"为什么这条没进结果"时先看这个。
    """
    return {
        "doc_id": chunk.doc_id,
        "chunk_id": chunk.chunk_id,
        "score": score,
        "reason": reason,
        "phase": phase,
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _filtered_trace_item(item: dict) -> dict:
    """被过滤的文档进 trace 的形状。

    `would_be_score` / `would_be_rank` 只在开了明细时才算得出来，所以有无都要能走。
    """
    out = {"doc_id": item["doc_id"], "reason": item["reason"]}
    if "would_be_score" in item:
        out["would_be_score"] = round(item["would_be_score"], 4)
        out["would_be_rank"] = item["would_be_rank"]
    return out


@dataclass
class Hit:
    doc_id: str
    chunk_id: str
    score: float
    text: str
    source_text: str
    meta: dict
    heading: str = ""
    """片段在文档里的位置，形如 `总标题 > 二级`。调试面板靠它认人。"""
    kind: str = "text"
    table_header: list[str] = field(default_factory=list)
    dropped_instructions: list[str] = field(default_factory=list)
    padded: bool = False
    """凑数补上的：契约 §4 要求恰好返回 top_k 条，但问答链路不会用它作答。"""

    def as_result(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "score": round(self.score, 4),
            "text": self.text,
        }


@dataclass
class SearchResult:
    hits: list[Hit]
    query: str
    terms: list[str]
    expansions: list[str]
    filtered: list[dict]
    coverage: float = 0.0
    #: 被**规则**挤掉的片段（每篇一格、top_k 截断）。`filtered` 记的是被**元数据**
    #: 挡掉的文档，两者是"没进结果"的两类原因，契约 §6 要的是合起来能看清为什么。
    dropped: list[dict] = field(default_factory=list)
    dropped_total: int = 0
    dropped_counts: dict = field(default_factory=dict)
    #: 这次检索的入参。排查"为什么没按门店过滤"时，先得知道门店到底传没传进来。
    args: dict = field(default_factory=dict)

    @property
    def ranked(self) -> list[Hit]:
        """真正命中的片段（不含为了凑满 top_k 补上的那些）。"""
        return [hit for hit in self.hits if not hit.padded]

    def as_trace(self) -> dict:
        return {
            "query": self.query,
            "terms": self.terms,
            "args": self.args,
            "expansions": self.expansions,
            "coverage": round(self.coverage, 3),
            "hits": [
                {
                    "doc_id": hit.doc_id,
                    "chunk_id": hit.chunk_id,
                    "score": round(hit.score, 4),
                    "heading": hit.heading,
                    "kind": hit.kind,
                    "text": _clip(hit.text, MAX_TRACE_TEXT),
                    "text_chars": len(hit.text),
                    "padded": hit.padded,
                    "dropped_instructions": hit.dropped_instructions,
                }
                for hit in self.hits[:MAX_TRACE_HITS]
            ],
            "hits_total": len(self.hits),
            "filtered": [_filtered_trace_item(item) for item in self.filtered],
            "dropped": [dict(item, score=round(item["score"], 4)) for item in self.dropped],
            "dropped_total": self.dropped_total,
            "dropped_counts": self.dropped_counts,
        }


class Retriever:
    def __init__(self, index: BM25Index, today: date) -> None:
        self.index = index
        self.today = today
        self._effective_to: dict[str, Optional[str]] = {}
        self._in_chain: set[str] = set()
        for doc_id, meta in index.docs_meta.items():
            successor = meta.get("superseded_by")
            if successor and successor in index.docs_meta:
                self._effective_to[doc_id] = index.docs_meta[successor].get("effective_from")
                self._in_chain.add(doc_id)
                self._in_chain.add(successor)

    # -- 元数据过滤 -------------------------------------------------------------

    def _eligible(
        self, doc_id: str, as_of: date, store_id: Optional[str], historical: bool = False
    ) -> Optional[str]:
        """返回排除原因；返回 None 表示这篇文档可以进入打分。

        问的就是“以前那一版”时（`historical`），不再按生效时间过滤：
        否则已废止的文档永远取不回来，而它恰恰是答案。
        """
        meta = self.index.docs_meta.get(doc_id, {})
        if store_id and meta.get("stores_explicit") and store_id not in (meta.get("stores") or []):
            return "文档声明只适用于 %s，与问题里的 %s 不符" % (",".join(meta.get("stores") or []), store_id)
        if historical:
            return None
        ends = self._effective_to.get(doc_id)
        # 只有标了“已废止”的才按取代关系挡掉，别的版本照常参与打分。
        if meta.get("status") == "已废止" and ends and as_of.isoformat() >= ends:
            return "该版本自 %s 起已被 %s 取代" % (ends, meta.get("superseded_by"))
        starts = meta.get("effective_from")
        if starts and starts > as_of.isoformat() and doc_id in self._in_chain:
            return "该版本自 %s 起才生效，晚于问题所指的 %s" % (starts, as_of.isoformat())
        return None

    def _multiplier(
        self,
        doc_id: str,
        as_of: date,
        store_id: Optional[str],
        year: Optional[int],
        window: Optional[tuple[str, str]],
        numeric: bool = False,
    ) -> float:
        meta = self.index.docs_meta.get(doc_id, {})
        factor = 1.0
        title_year = meta.get("title_year")
        if year and title_year and int(title_year) != int(year):
            factor *= YEAR_PENALTY
        starts = meta.get("effective_from")
        if starts and starts > as_of.isoformat():
            factor *= FUTURE_PENALTY
        if store_id and store_id in (meta.get("stores") or []):
            factor *= STORE_HINT_BOOST
        if window and starts and window[0] <= starts <= window[1]:
            factor *= WINDOW_BOOST
        if doc_id == self.index.aliases.source_doc:
            factor *= ALIAS_DOC_PENALTY
        if numeric and meta.get("estimates_only"):
            factor *= ESTIMATE_DOC_PENALTY
        return factor

    # -- 检索 -------------------------------------------------------------------

    def _weights(self, query: str) -> dict[str, float]:
        weights: dict[str, float] = {}
        for token in tokenize(query):
            weight = SINGLE_CHAR_WEIGHT if len(token) == 1 else 1.0
            weights[token] = weights.get(token, 0.0) + weight
        return weights

    def _concept_scores(
        self, query: str, allowed: set[int]
    ) -> tuple[dict[int, float], list[str]]:
        """别名按“同一个东西”合并：一个概念只算它最像的那一种写法，不叠加。

        不这么做的话，同时列出全部写法的别名词典自己会永远排第一。
        """
        merged: dict[int, float] = {}
        expansions: list[str] = []
        for canonical in self.index.aliases.mentions(query) + self._store_concepts(query):
            variants = self.index.aliases.variants(canonical)
            best: dict[int, float] = {}
            for variant in variants:
                weights = {token: ALIAS_WEIGHT for token in tokenize(variant)}
                if not weights:
                    continue
                for position, score in self.index.score_terms(weights, allowed).items():
                    if score > best.get(position, 0.0):
                        best[position] = score
            for position, score in best.items():
                merged[position] = merged.get(position, 0.0) + score
            expansions.extend(variants)
        return merged, expansions

    def _history_factor(self, doc_id: str, historical: Optional[bool]) -> float:
        """问旧口径时，已废止的那一版才是答案，给它加权。"""
        if not historical:
            return 1.0
        meta = self.index.docs_meta.get(doc_id, {})
        return 1.6 if meta.get("superseded_by") else 0.8

    def _store_concepts(self, query: str) -> list[str]:
        import re

        found = []
        for code in re.findall(r"\bs\d{2}\b", query.lower()):
            canonical = self.index.aliases.by_store_code(code)
            if canonical:
                found.append(canonical)
        return found

    def _hit(self, position: int, score: float, filtered: list[dict], padded: bool = False) -> Hit:
        chunk = self.index.chunks[position]
        return Hit(
            doc_id=chunk.doc_id,
            chunk_id=chunk.chunk_id,
            score=score,
            text=chunk.text,
            source_text=chunk.source_text,
            meta=self.index.docs_meta.get(chunk.doc_id, {}),
            heading=chunk.heading,
            kind=chunk.kind,
            table_header=chunk.table_header,
            padded=padded,
        )

    def search(
        self,
        query: str,
        top_k: int = 5,
        as_of: Optional[date] = None,
        store_id: Optional[str] = None,
        year: Optional[int] = None,
        window: Optional[tuple[str, str]] = None,
        numeric: bool = False,
        historical: Optional[bool] = None,
        explain: bool = False,
    ) -> SearchResult:
        """检索并返回结果。

        `explain=True` 时额外算出调试明细（契约 §6）：每篇被过滤的文档"本来会得
        多少分、排第几"，以及被每篇一格 / top_k 截断挤掉的片段。这些**只读**，
        不参与排序——`explain` 开与不开，`hits` 与 `filtered` 的 `doc_id/reason`
        必须逐位一致（有一组测试专门盯这件事）。默认关：`/api/retrieve` 每次
        检索都算一遍是白花的力气，它的响应形状也是固定的。
        """
        as_of = as_of or self.today
        if historical is None:
            # `/api/retrieve` 没有规划器，问句里的“旧口径/以前”只能在这里认。
            historical = wants_historical(query)

        def final_score(raw: float, best: float, doc_id: str) -> float:
            """一块片段的最终分。

            在线排序与反事实各要算一次，公式只留这一处：抄成两份的话，改了一处
            忘了另一处，反事实分就会悄悄不对——而它看起来永远"合理"。
            六个入参直接闭包捕获，省得将来有人漏传一个。
            """
            total = raw + DOC_PRIOR * best
            return (
                total
                * self._multiplier(doc_id, as_of, store_id, year, window, numeric)
                * self._history_factor(doc_id, historical)
            )

        filtered: list[dict] = []
        excluded: set[str] = set()
        for doc_id in self.index.docs_meta:
            reason = self._eligible(doc_id, as_of, store_id, historical)
            if reason:
                excluded.add(doc_id)
                filtered.append({"doc_id": doc_id, "reason": reason})
        # 契约 §4：被过滤的文档不能占名额。先把它剔除再打分——
        # 打分时放进来、取完 top-k 再过滤的话，名额已经被它吃掉了，
        # 结果会凑不满 top_k（实测 3 条只剩 1 条）。
        allowed = {
            position
            for position, chunk in enumerate(self.index.chunks)
            if chunk.doc_id not in excluded
        }

        weights = self._weights(query)
        scores = self.index.score_terms(weights, allowed)
        concepts, expansions = self._concept_scores(query, allowed)
        for position, score in concepts.items():
            scores[position] = scores.get(position, 0.0) + score
        best_of_doc = _best_by_doc(self.index.chunks, scores)
        adjusted: list[tuple[float, int]] = []
        for position, score in scores.items():
            doc_id = self.index.chunks[position].doc_id
            adjusted.append((final_score(score, best_of_doc.get(doc_id, 0.0), doc_id), position))
        adjusted.sort(key=lambda item: (-item[0], item[1]))

        hits: list[Hit] = []
        taken: set[int] = set()
        matched: list[int] = []  # 真正命中、且真的进了结果的位置，用来算覆盖率
        per_doc: dict[str, int] = {}
        dropped: list[dict] = []
        noted: set[int] = set()
        stopped_at: Optional[int] = None

        def note_dropped(position: int, score: float, reason: str, phase: str) -> None:
            """记一条被挤掉的片段。

            **同一块只记第一次**：主循环和补齐循环都会碰到同一块，记两遍的话
            面板上就是同一行出现两次，还白白占掉 trace 里的条目上限。
            主循环没 `break` 时（也就是补齐循环唯一进得去的那种情况）它已经
            看过所有**有分**的片段了，补齐循环真正新增的是那些**零分**的。
            """
            if not explain or position in noted:
                return
            noted.add(position)
            dropped.append(_dropped_item(self.index.chunks[position], score, reason, phase))

        for rank, (score, position) in enumerate(adjusted):
            chunk = self.index.chunks[position]
            if per_doc.get(chunk.doc_id, 0) >= MAX_CHUNKS_PER_DOC:
                # 这一条本来是静默跳过的：一篇文档的第二个高分片段被挤掉之后，
                # 面板上看不出"它也在候选里"。
                note_dropped(position, score, _ONE_PER_DOC, "rank")
                continue
            per_doc[chunk.doc_id] = per_doc.get(chunk.doc_id, 0) + 1
            taken.add(position)
            matched.append(position)
            hits.append(self._hit(position, score, filtered))
            if len(hits) >= top_k:
                stopped_at = rank
                break
        if stopped_at is not None:
            for score, position in adjusted[stopped_at + 1 :]:
                note_dropped(position, score, _CUT_BY_TOP_K, "rank_tail")

        # 契约 §4：索引里的片段够的时候必须恰好给 top_k 条。
        # 每篇文档只占一格的规则、以及“一个词都没命中”的片段，都可能让结果不足，
        # 这里按分数从高到低补齐；补上的标成 padded，问答链路不会拿它们作答。
        if len(hits) < top_k:
            scored = {position for _, position in adjusted}
            remaining = [(score, position) for score, position in adjusted if position not in taken]
            # 一个词都没命中的片段用来垫最后几格：每篇文档先出一段，
            # 同一篇连着占满几格没什么意义。
            unscored: dict[str, list[int]] = {}
            for position in sorted(allowed):
                if position in taken or position in scored:
                    continue
                unscored.setdefault(self.index.chunks[position].doc_id, []).append(position)
            while any(unscored.values()):
                for positions in unscored.values():
                    if positions:
                        remaining.append((0.0, positions.pop(0)))
            for score, position in remaining:
                if len(hits) >= top_k:
                    break
                chunk = self.index.chunks[position]
                # 补齐也要守"一篇文档最多一格"：原实现只管取 top-k 的那一轮，
                # 补齐这一轮会把同一篇的片段连着塞进来（实测 KB-001 出现三次）。
                if per_doc.get(chunk.doc_id, 0) >= MAX_CHUNKS_PER_DOC:
                    note_dropped(position, score, _ONE_PER_DOC, "fill")
                    continue
                per_doc[chunk.doc_id] = per_doc.get(chunk.doc_id, 0) + 1
                taken.add(position)
                hits.append(self._hit(position, score, filtered, padded=True))
            # 契约 §4 还要求“按相关性从高到低”：补齐之后整体再排一次。
            # 每篇文档只占一格是挑片段的规则，不是排序的规则。
            hits.sort(key=lambda hit: -hit.score)

        if explain and filtered:
            # 反事实：把元数据过滤整个关掉，看这些文档本来会得多少分、排第几。
            # 只用被过滤的那几篇，结果**只写进 filtered**，不参与上面的排序。
            # `score_terms(w, None)` 与 `score_terms(w, allowed)` 对同一个块逐位相等
            # （score_terms 是按块独立求和的），所以这一遍算出来的就是它"本来会拿到的分"。
            full_scores = self.index.score_terms(weights, None)
            full_concepts, _ = self._concept_scores(query, None)
            for position, score in full_concepts.items():
                full_scores[position] = full_scores.get(position, 0.0) + score
            full_best = _best_by_doc(self.index.chunks, full_scores)
            # 名次是**文档**名次，不是片段名次：真实结果里一篇文档最多占一格
            # （`MAX_CHUNKS_PER_DOC`），所以只数"分比它高的文档"有几个。
            # 拿 `adjusted` 里的片段去数会偏大——同一篇文档的第二个高分片段
            # 根本不会进结果，却会被算成排在它前面。
            # 每篇文档的分数 = 它最高分那块（`best` 与 `raw` 在这里是同一个值）。
            doc_scores = [final_score(raw, raw, doc_id) for doc_id, raw in full_best.items()]
            for item in filtered:
                # 一篇文档的所有块都没命中查询词时按 0.0 算——与在线打分里
                # `best_of_doc.get(doc_id, 0.0)` 是同一个约定。
                raw = full_best.get(item["doc_id"], 0.0)
                would_be = final_score(raw, raw, item["doc_id"])
                item["would_be_score"] = would_be
                item["would_be_rank"] = 1 + sum(1 for score in doc_scores if score > would_be)

        dropped.sort(key=lambda item: -item["score"])
        dropped_total = len(dropped)
        dropped_counts: dict[str, int] = {}
        for item in dropped:
            dropped_counts[item["reason"]] = dropped_counts.get(item["reason"], 0) + 1

        return SearchResult(
            hits=hits,
            query=query,
            terms=content_tokens(query),
            expansions=expansions,
            filtered=filtered,
            coverage=self._coverage(query, matched),
            dropped=dropped[:MAX_TRACE_DROPPED],
            dropped_total=dropped_total,
            dropped_counts=dropped_counts,
            args={
                "as_of": as_of.isoformat(),
                "store_id": store_id,
                "year": year,
                "window": list(window) if window else None,
                "numeric": numeric,
                "historical": historical,
                "top_k": top_k,
            },
        )

    def _coverage(self, query: str, positions: list[int]) -> float:
        """问题被**实际返回**的那几个片段覆盖了多少。

        只看真正命中的片段：一个词都没命中时（“zzzqqq”），覆盖率就是 0，
        这是定义，不是异常——为了凑满 top_k 补上的片段不参与这个判断。

        取的是实际返回的位置，不是"排序后的前 top_k 个"：被元数据过滤掉的
        文档不返回，就不该拿它的覆盖率来代表这次检索。
        """
        if not positions:
            return 0.0
        terms = content_tokens(query)
        return max(self.index.coverage(terms, position) for position in positions)


def build_retriever(kb_dir: Path, index_path: Path, today: date, rebuild: bool = False) -> Retriever:
    return Retriever(load_index(kb_dir, index_path, rebuild=rebuild), today)
