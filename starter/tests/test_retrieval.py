"""检索基础设施的回归测试：分词、加载器、切块、索引缓存、检索器。

和另外两个测试文件一个路子：

* **不写死真实数据集的数字**（评审会换 `data/` 与 `knowledge_base/`）。
  格式与编码这些用临时目录里的合成文件来压；真实知识库上只断言
  与内容无关的不变量（"目录里有几篇带编号的文档，索引里就该有几篇"）。
* **只测行为**。分词的切法、切块的边界，全部通过公开函数的返回值来验证。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from datetime import date

from kbqa.aliases import AliasTable
from kbqa.chunker import CHUNK_SIZE, Chunk, chunk_document
from kbqa.index import BM25Index, build_index, content_key, load_index
from kbqa.loader import Document, load_knowledge_base
from kbqa.retriever import Retriever
from kbqa.tokenizer import content_tokens, normalise, tokenize

import kbqa.index as index_module

KB_ROOT = Path(__file__).resolve().parents[2] / "knowledge_base"

_DOC_ID_IN_NAME = re.compile(r"^(KB-\d{3})_")


# -- 分词 -----------------------------------------------------------------------

#: KB-013 里的一句话，关键词都在。检索能成立的前提就是问句和正文切出同样的词。
_DOC_SENTENCE = "外卖订单在订单送达后 24 小时内可以申请退款"
_QUERY = "外卖订单多久内可以申请退款"


def test_chinese_runs_become_overlapping_bigrams():
    """中文按**二元组**切，相邻两字成词，逐字滑动。

    KB-001 的正文里写着"含虚字的二元组"、`docfacts.py` 里按 `len(term) == 2`
    给权重打折，检索层本来就是按二元组设计的。
    """
    assert tokenize("外卖订单退款") == ["外卖", "卖订", "订单", "单退", "退款"]
    assert tokenize("退款") == ["退款"]


def test_a_whole_sentence_is_never_a_single_token():
    """这是最要命的那种错：整句成了一个 token，BM25 就完全失效了。

    原实现是 `normalise(text).split()`，中文没有空格，一句话永远切出一个 token。
    """
    tokens = tokenize("外卖订单多久内可以申请退款？")
    assert len(tokens) > 5
    assert "外卖订单多久内可以申请退款" not in tokens
    assert "外卖" in tokens
    assert "退款" in tokens


def test_query_and_document_share_tokens():
    """问句与正文必须切得出共同的词，否则再好的 BM25 也算不出相关性。"""
    query_tokens = set(tokenize(_QUERY))
    doc_tokens = set(tokenize(_DOC_SENTENCE))
    shared = query_tokens & doc_tokens
    assert "外卖" in shared
    assert "订单" in shared
    assert "退款" in shared
    # 整句相同长度才可能只有一个词，这里两侧都是十几个词
    assert len(query_tokens) > 5 and len(doc_tokens) > 5


def test_latin_and_digits_stay_whole_words():
    """英文与数字按整词，不做二元组——`poke` 切成 `po`/`ok` 只会引入噪声。"""
    assert tokenize("Makai Poke") == ["makai", "poke"]
    assert tokenize("24 小时") == ["24", "小时"]
    assert tokenize("S03") == ["s03"]


def test_bigrams_do_not_cross_script_or_punctuation():
    """标点、空格、中英边界都要断开，不能让"款，"这种残渣进索引。"""
    assert tokenize("退款，政策") == ["退款", "政策"]
    assert tokenize("牛肉poke") == ["牛肉", "poke"]
    # 中文段照切二元组，英文段整词——两者互不影响
    assert tokenize("Makai Poke 三文鱼") == ["makai", "poke", "三文", "文鱼"]


def test_normalise_is_still_applied():
    """全角转半角、统一小写这一层不能丢。"""
    assert normalise("ＰＯＫＥ Ｓ０３") == "poke s03"
    assert tokenize("ＰＯＫＥ") == ["poke"]


def test_stop_chars_filter_works_on_bigrams():
    """`content_tokens` 去掉的是"两个字都是虚词"的二元组，不是含虚字的全部。

    这条逻辑（`all(char in STOP_CHARS for char in token)`）在二元组下才成立，
    是按空白切词之后就一直没生效过。
    """
    assert "退款" in content_tokens("退款政策")
    # “的”单独成词会被丢掉；“的可”两个字都是虚词，也丢掉
    assert "的" not in content_tokens("的 的")
    assert "的可" not in content_tokens("的可")
    # 含一个虚字的二元组要留下，它是真词（“在售”）
    assert "在售" in content_tokens("在售")


# -- 加载器 ---------------------------------------------------------------------


def _write_kb(root: Path) -> None:
    (root / "a").mkdir(parents=True, exist_ok=True)
    (root / "a" / "KB-900_手写文档.md").write_text(
        "---\ntitle: 手写文档\n---\n\n## 一、营业时间\n\n门店营业时间为 10:00 至 22:00。\n",
        encoding="utf-8",
    )
    (root / "a" / "KB-901_纯文本通知.txt").write_text(
        "通知\n\n自 2026 年 3 月 1 日起，营业时间调整为 09:00 至 21:00。\n",
        encoding="utf-8",
    )
    (root / "a" / "KB-902_常见问题.html").write_text(
        "<!DOCTYPE html>\n<html><head><title>常见问题 FAQ - 合味餐饮</title>\n"
        "<style>body { color: #b4432a; font-size: 14px; }</style>\n"
        "<script>var x = '退款';</script>\n</head><body>\n"
        "<h1>常见问题</h1>\n<p>发票：可以开电子发票。</p>\n</body></html>\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("知识库说明，没有编号。\n", encoding="utf-8")


def test_text_and_html_files_are_indexed(tmp_path):
    """契约 §0：编号在文件名开头，**与文件格式无关**。

    原实现只收 `.md`/`.markdown`，`.txt` 与 `.html` 直接不进索引，
    所以知识库里 36 个文件只有 32 篇进得来，契约的 `kb_docs: 35` 对不上。
    """
    kb = tmp_path / "kb"
    _write_kb(kb)
    docs, _ = load_knowledge_base(kb)
    ids = {doc.doc_id for doc in docs}
    assert ids == {"KB-900", "KB-901", "KB-902"}


def test_text_and_html_get_the_right_format(tmp_path):
    kb = tmp_path / "kb"
    _write_kb(kb)
    docs, _ = load_knowledge_base(kb)
    by_id = {doc.doc_id: doc for doc in docs}
    assert by_id["KB-900"].fmt == "md"
    assert by_id["KB-901"].fmt == "txt"
    assert by_id["KB-902"].fmt == "html"


def test_gbk_file_is_decoded_not_mangled(tmp_path):
    """KB-062 是旧 OA 用 GBK 导出的 `.txt`，按 UTF-8 硬读会得到一片乱码。

    原实现 `raw.decode("utf-8", errors="ignore")` 会把解不出来的字节**直接删掉**，
    正文缺一块却没有任何报错——比抛异常更难发现。
    """
    kb = tmp_path / "kb"
    kb.mkdir()
    body = "自 2026 年 3 月 1 日起，营业时间调整为 10:00 至 22:00。"
    (kb / "KB-903_旧OA导出_营业时间调整通知.txt").write_bytes(body.encode("gb18030"))

    docs, _ = load_knowledge_base(kb)
    assert len(docs) == 1
    text = docs[0].text
    assert "营业时间调整" in text
    assert "22:00" in text
    # 不能有替换字符，也不能缺字
    assert "�" not in text
    assert len(text) >= len(body)


def test_html_tags_and_scripts_are_stripped(tmp_path):
    """HTML 要按可见正文入库：`<style>`/`<script>` 与标签都得去掉。

    留着标签的话，`<style>` 里那堆 CSS 会切成一大片噪声二元组，
    引用的 quote 里也会夹着 `<p>` 这种东西。
    """
    kb = tmp_path / "kb"
    _write_kb(kb)
    docs, _ = load_knowledge_base(kb)
    html_doc = next(doc for doc in docs if doc.doc_id == "KB-902")

    assert "<style>" not in html_doc.text
    assert "<script>" not in html_doc.text
    assert "<p>" not in html_doc.text
    assert "box-sizing" not in html_doc.text
    assert "font-size" not in html_doc.text
    # 可见正文要留下
    assert "发票" in html_doc.text
    assert "常见问题" in html_doc.text


def test_files_without_a_doc_id_are_skipped_with_a_warning(tmp_path):
    """`README.md` 没有 KB 编号，跳过，但要留一条告警。"""
    kb = tmp_path / "kb"
    _write_kb(kb)
    docs, warnings = load_knowledge_base(kb)
    assert all(doc.doc_id.startswith("KB-") for doc in docs)
    assert any("README.md" in warning for warning in warnings)


# -- 切块 -----------------------------------------------------------------------


def _document(text: str, doc_id: str = "KB-900", title: str = "合成文档") -> Document:
    """内存里的文档，不用落盘——切块只吃 `Document`。"""
    return Document(doc_id=doc_id, title=title, text=text, path=Path("%s_x.md" % doc_id), fmt="md")


def _lines_of_text(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip()}


def _chunk_lines(chunks) -> set[str]:
    return {line.strip() for chunk in chunks for line in chunk.source_text.splitlines() if line.strip()}


_SECTION_DOC = """\
# 手册

## 一、营业时间

门店营业时间为 10:00 至 22:00。节假日照常营业。

## 二、退款

外卖订单在订单送达后 24 小时内可以申请退款。
"""


def _long_doc() -> str:
    """一篇远超 CHUNK_SIZE 的文档，最后一行是只出现一次的标记。"""
    lines = ["# 长文档"]
    for number in range(1, 60):
        lines.append("第 %02d 条：门店营业时间与排班规则说明，适用于全部直营门店。" % number)
    lines.append("尾行标记 Z9 仅此一处")
    return "\n".join(lines)


def test_no_line_is_lost_including_the_tail():
    """切块**不许丢正文**——这是它最要命的错法。

    原实现是 `range(0, len(text) - CHUNK_SIZE, CHUNK_SIZE)`：上界少了整整一块，
    最后 `CHUNK_SIZE` 个字符直接不进索引。真实知识库里 35 篇文档**每一篇**
    都丢字，合计 6107 字（18.6%）；一篇 587 字的文档只索引了前 300 字。
    正文丢了不会有任何报错，只会莫名其妙地召不回——比抛异常难查得多。
    """
    text = _long_doc()
    chunks = chunk_document(_document(text))

    missing = _lines_of_text(text) - _chunk_lines(chunks)
    assert not missing, "这些正文没有进索引：%s" % sorted(missing)[:5]
    assert "尾行标记 Z9 仅此一处" in _chunk_lines(chunks)


def test_the_whole_document_is_covered_not_just_the_head():
    """一篇 587 字的文档要全部进索引，而不是只有前 300 字。

    原实现只在**短于 CHUNK_SIZE** 时才走"整篇一块"的兜底分支，
    落在 300 字到 600 字之间的文档恰好被砍掉尾巴。
    """
    #: 每行都得不一样。全用同一句的话，集合去重之后前 300 字就把"所有行"
    #: 覆盖完了，这条测试会假绿——不会红的测试比没有测试更危险。
    text = "# 通知\n\n" + "".join(
        "第 %02d 条：营业时间调整为 09:00 至 21:00，请各店按新时间排班。\n" % number
        for number in range(1, 9)
    )
    assert 300 < len(text) < 600  # 正好落在会丢尾巴的区间里
    chunks = chunk_document(_document(text))
    missing = _lines_of_text(text) - _chunk_lines(chunks)
    assert not missing, "后半篇没进索引：%s" % sorted(missing)


def test_prose_chunks_stay_within_the_chunk_size():
    """正常长度的句子，切出来的一块不能超过 `CHUNK_SIZE`。"""
    chunks = chunk_document(_document(_long_doc()))
    over = [chunk.chunk_id for chunk in chunks if len(chunk.text) > CHUNK_SIZE]
    assert not over, "这些块超长：%s" % over


_TABLE_HEADER = "| 商品编码 | 商品名称 | 门店 | 售价 |"
_TABLE_SEP = "| --- | --- | --- | --- |"


def _table_doc(rows: int = 40) -> str:
    body = [_TABLE_HEADER, _TABLE_SEP]
    for number in range(1, rows + 1):
        body.append("| P%03d | 商品%03d | S%02d | %d.00 |" % (number, number, number % 5 + 1, number))
    return "# 价目表\n\n" + "\n".join(body) + "\n"


def test_a_table_becomes_a_table_chunk_with_its_header():
    """整表切块要产出 `kind="table"` 与表头。

    这两个字段现在是死代码：原实现一律 `kind="text"`、`table_header=[]`，
    于是 `units.py` 的表格分支永远进不去，`docfacts.render_row` 也拼不出
    "商品名称：三文鱼，售价：35.00" 这种可读的行——答案里会直接甩一根竖线。
    """
    chunks = chunk_document(_document(_table_doc()))
    tables = [chunk for chunk in chunks if chunk.kind == "table"]
    assert tables, "整张表一块都没切成 table"
    assert all(chunk.table_header == ["商品编码", "商品名称", "门店", "售价"] for chunk in tables)


def test_a_table_is_not_cut_away_from_its_header():
    """表格可以分成几块，但**每一块都要带表头**。

    KB-040 的表有 23 行 1149 字，超过 CHUNK_SIZE 必须拆；
    拆出去的那几行如果没有表头，"| P007 | 商品007 | S03 | 7.00 |"
    就只剩一串光秃秃的数字，既检索不到"售价"，也拼不成一句人话。
    """
    text = _table_doc()
    chunks = chunk_document(_document(text))
    tables = [chunk for chunk in chunks if chunk.kind == "table"]
    assert len(tables) > 1, "这张表本来就该拆成多块，否则测不到拆表"

    for chunk in tables:
        assert _TABLE_HEADER in chunk.source_text, "这块表丢了表头：%s" % chunk.chunk_id

    # 表格行必须整行进索引，不能被拦腰切断
    assert not (_lines_of_text(text) - _chunk_lines(chunks))


def test_heading_path_is_recorded_without_the_hashes():
    """块的 `heading` 是标题路径，用 ` > ` 连接，**不带 `#`**。

    `units.py` 拿 `chunk.heading.split(" > ")` 的结果去和
    `sentence.strip().lstrip("#")` 比对（第 119、145 行）；
    带着 `#` 的话比对永远不相等，标题就永远标不成 `kind="heading"`。
    """
    chunks = chunk_document(_document(_SECTION_DOC))
    by_heading = {chunk.heading: chunk.source_text for chunk in chunks}

    refund = [chunk for chunk in chunks if "外卖订单" in chunk.source_text]
    assert len(refund) == 1
    assert refund[0].heading == "手册 > 二、退款"
    assert not any(part.startswith("#") for part in refund[0].heading.split(" > "))

    hours = [chunk for chunk in chunks if "10:00 至 22:00" in chunk.source_text]
    assert len(hours) == 1
    assert hours[0].heading == "手册 > 一、营业时间"
    assert by_heading  # 每块都要有 heading，不能是空串


def test_chunk_ids_are_unique():
    chunks = chunk_document(_document(_long_doc()))
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    assert all(chunk.doc_id == "KB-900" for chunk in chunks)


# -- 索引缓存 -------------------------------------------------------------------


def _write_kb_v2(root: Path, notice: str = "自 2026 年 3 月 1 日起，营业时间调整为 09:00 至 21:00。") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "KB-910_营业时间通知.md").write_text(
        "# 营业时间通知\n\n" + notice + "\n", encoding="utf-8"
    )


def test_content_key_is_stable_for_the_same_content(tmp_path):
    kb_a = tmp_path / "a"
    kb_b = tmp_path / "b"
    _write_kb_v2(kb_a)
    _write_kb_v2(kb_b)
    assert content_key(kb_a) == content_key(kb_a)
    # 换个目录、内容一样，键也该一样（键里不能混进绝对路径）
    assert content_key(kb_a) == content_key(kb_b)


def test_content_key_changes_when_the_knowledge_base_changes(tmp_path):
    """内容变了，键就得变。

    原实现只哈希三个版本号（`INDEX_VERSION|CHUNKER_VERSION|TOKENIZER_VERSION`），
    **一点知识库内容都不看**。改了正文而没改版本号，键原样不变，
    缓存永远"命中"，跑的还是旧索引。
    """
    kb = tmp_path / "kb"
    _write_kb_v2(kb)
    before = content_key(kb)

    _write_kb_v2(kb, notice="自 2026 年 4 月 1 日起，营业时间调整为 08:00 至 20:00。")
    assert content_key(kb) != before, "正文改了，缓存键却没变"

    # 改文件名也算改内容
    _write_kb_v2(kb)
    renamed = content_key(kb)
    (kb / "KB-910_营业时间通知.md").rename(kb / "KB-910_营业时间调整通知.md")
    assert content_key(kb) != renamed


def test_a_stale_cache_is_not_served(tmp_path):
    """端到端：正文改了之后，`load_index` 必须给出新内容。

    仓库里那份 `.cache/index.json` 就是这么过期并且被提交进 git 的。
    """
    kb = tmp_path / "kb"
    cache = tmp_path / "index.json"
    _write_kb_v2(kb)

    first = load_index(kb, cache)
    assert any("09:00" in chunk.text for chunk in first.chunks)

    _write_kb_v2(kb, notice="自 2026 年 4 月 1 日起，营业时间调整为 08:00 至 20:00。")
    second = load_index(kb, cache)  # 不给 rebuild，缓存该自己失效

    assert any("08:00" in chunk.text for chunk in second.chunks)
    assert not any("09:00" in chunk.text for chunk in second.chunks), "还在吃旧缓存"


def test_a_fresh_cache_is_reused(tmp_path, monkeypatch):
    """内容没变时要真的复用缓存——每次都重建就等于没有缓存。"""
    cache = tmp_path / "index.json"
    _write_kb_v2(tmp_path / "kb")
    load_index(tmp_path / "kb", cache)
    assert cache.exists()

    def boom(_kb_dir):
        raise AssertionError("缓存应该命中，不该重建")

    monkeypatch.setattr(index_module, "build_index", boom)
    index = load_index(tmp_path / "kb", cache)
    assert index.chunks


def test_a_corrupt_cache_is_rebuilt(tmp_path):
    """缓存文件坏了（写了一半、手工改过）要能自己重建，不能让服务起不来。"""
    kb = tmp_path / "kb"
    cache = tmp_path / "index.json"
    _write_kb_v2(kb)
    cache.write_text("{ 这不是 JSON", encoding="utf-8")

    index = load_index(kb, cache)
    assert index.chunks
    assert cache.read_text(encoding="utf-8").startswith("{")  # 已经被正常内容覆盖


# -- 检索器 ---------------------------------------------------------------------


def _synthetic_index(pairs, meta=None):
    """用（doc_id, 块文本）造一个小索引，不落盘、不碰真实知识库。"""
    chunks = [
        Chunk(doc_id=doc_id, chunk_id="%s#%d" % (doc_id, number), text=text, source_text=text)
        for doc_id, number, text in pairs
    ]
    return BM25Index(chunks, meta or {}, AliasTable.from_json({}), "test-key")


def _synthetic_retriever(pairs, meta=None):
    return Retriever(_synthetic_index(pairs, meta), date(2026, 9, 1))


def test_every_hit_reports_its_own_doc_id():
    """`hit.doc_id` 必须是这块片段**自己**所属的文档。

    原实现第 276 行 `hit.doc_id = ordered[len(hits)].doc_id`：
    它拿"全局第几条"去另一个数组里取文档。只要前面有片段被跳过
    （同一篇文档的第二个片段就会被 `MAX_CHUNKS_PER_DOC` 跳过），
    两个下标就错开了，`doc_id` 挂到别人的片段上。

    实测就是这个样子：`doc_id=KB-012 chunk_id=KB-013#2`、
    `doc_id=KB-041 chunk_id=KB-022#2`——返回给评测的文档号全是错的，
    正确答案明明被检索到了，报上去的却是另一篇。
    """
    # KB-A 的两块必须是最高分：只有"同一篇的第二个片段被跳过"之后，
    # 两个下标才会错开。写得比别家长的话，BM25 的长度归一化会把它们压到后面，
    # 跳过根本不会发生，这条测试就成了假绿。
    retriever = _synthetic_retriever(
        [
            ("KB-A", 1, "苹果苹果苹果价格"),
            ("KB-A", 2, "苹果苹果苹果规格"),
            ("KB-B", 1, "苹果的陈列要求，苹果摆放位置，苹果面向顾客。"),
            ("KB-C", 1, "苹果的采购周期，苹果进货频次，苹果验收标准。"),
        ]
    )
    result = retriever.search("苹果", top_k=3)

    for hit in result.hits:
        assert hit.chunk_id.startswith(hit.doc_id), (
            "doc_id 与 chunk_id 对不上：doc_id=%s chunk_id=%s" % (hit.doc_id, hit.chunk_id)
        )
    assert {hit.doc_id for hit in result.hits} == {"KB-A", "KB-B", "KB-C"}


def test_a_document_never_takes_more_than_one_slot():
    """`MAX_CHUNKS_PER_DOC = 1`：多留几篇不同的文档，比同一篇留两段有用。"""
    retriever = _synthetic_retriever(
        [
            ("KB-A", 1, "苹果的售价与规格说明，苹果分级标准。"),
            ("KB-A", 2, "苹果的产地与运输方式，苹果冷链要求。"),
            ("KB-A", 3, "苹果的包装与损耗标准，苹果验收规则。"),
            ("KB-B", 1, "苹果的陈列要求，苹果摆放位置。"),
        ]
    )
    result = retriever.search("苹果", top_k=3)
    doc_ids = [hit.doc_id for hit in result.hits if not hit.padded]
    assert len(doc_ids) == len(set(doc_ids)), "同一篇文档占了两格：%s" % doc_ids


def test_filtered_chunks_never_take_a_slot():
    """被元数据过滤掉的文档，它的片段**不能占名额**。

    契约 §4 写明："先取前 `top_k` 再做过滤、结果只剩两三条的实现，不符合这一条"。
    原实现 `allowed = set(range(len(chunks)))` 把全部片段都放进去打分，
    过滤却留到最后一步才做（第 307 行），于是结果常常不足 `top_k`。
    """
    meta = {
        "KB-OLD": {"status": "已废止", "superseded_by": "KB-NEW", "effective_from": "2026-01-01"},
        "KB-NEW": {"status": "现行", "effective_from": "2026-05-01"},
    }
    retriever = _synthetic_retriever(
        [
            # 已废止的那一篇分数最高，最容易被它挤掉名额
            ("KB-OLD", 1, "苹果的售价与规格说明，苹果分级标准，苹果验收。"),
            ("KB-OLD", 2, "苹果的产地与运输方式，苹果冷链，苹果到货。"),
            ("KB-NEW", 1, "苹果的售价与规格说明。"),
            ("KB-B", 1, "苹果的陈列要求。"),
            ("KB-C", 1, "苹果的采购周期。"),
        ],
        meta,
    )
    result = retriever.search("苹果", top_k=3)

    assert len(result.hits) == 3, "只剩下 %d 条，过滤不该吃掉名额" % len(result.hits)
    assert "KB-OLD" not in {hit.doc_id for hit in result.hits}
    assert filtered_docs(result) == {"KB-OLD"}


def filtered_docs(result) -> set:
    return {item["doc_id"] for item in result.filtered}


def test_hits_are_sorted_by_score_descending():
    """契约 §4：按相关性从高到低。补齐之后也得重排。"""
    retriever = _synthetic_retriever(
        [
            ("KB-A", 1, "苹果"),
            ("KB-B", 1, "苹果的售价与规格说明，苹果分级标准。"),
            ("KB-C", 1, "苹果"),
            ("KB-D", 1, "苹果"),
        ]
    )
    result = retriever.search("苹果", top_k=3)
    scores = [hit.score for hit in result.hits]
    assert scores == sorted(scores, reverse=True), scores


def test_a_tiny_index_returns_what_it_has():
    """索引里的片段本来就不足 `top_k` 时，给少一点是允许的——但不能补出空块。"""
    retriever = _synthetic_retriever([("KB-A", 1, "苹果的售价。"), ("KB-B", 1, "香蕉的价格。")])
    result = retriever.search("苹果", top_k=5)
    assert len(result.hits) == 2
    assert all(hit.text.strip() for hit in result.hits)


# -- 真实知识库的不变量（不写死任何数字） ----------------------------------------


def test_no_line_of_the_real_knowledge_base_is_lost():
    """真实知识库**每一行**正文都要出现在某个块里。

    断言完全由目录内容推出来：换知识库也成立，且不写死任何数字。
    """
    if not KB_ROOT.exists():
        pytest.skip("没有 %s，跳过" % KB_ROOT)

    documents, _ = load_knowledge_base(KB_ROOT)
    assert documents, "一篇文档都没加载出来，测试本身失效了"

    lost: dict[str, list[str]] = {}
    for document in documents:
        missing = _lines_of_text(document.text) - _chunk_lines(chunk_document(document))
        if missing:
            lost[document.doc_id] = sorted(missing)[:3]
    assert not lost, "有文档丢了正文：%s" % lost


def test_real_knowledge_base_loads_every_numbered_file():
    """`knowledge_base/` 里有多少个带编号的文件，就该加载出多少篇文档。

    这个断言完全由目录本身推出来，换知识库也成立。
    """
    if not KB_ROOT.exists():
        pytest.skip("没有 %s，跳过" % KB_ROOT)

    expected = {
        match.group(1)
        for path in KB_ROOT.rglob("*")
        if path.is_file() and (match := _DOC_ID_IN_NAME.match(path.name))
    }
    assert expected, "知识库目录里一篇带编号的文档都没有，测试本身失效了"

    docs, warnings = load_knowledge_base(KB_ROOT)
    assert {doc.doc_id for doc in docs} == expected
    # 编号不重复，所以不该有重复告警
    assert not [w for w in warnings if "doc_id 重复" in w]


# -- 已废止的版本不该压过现行版 ---------------------------------------------------

_SUPERSEDED_DOC = """---
doc_id: KB-901
title: 退货规则 v1
type: 政策
status: 已废止
effective_from: 2025-01-01
superseded_by: KB-902
updated_at: 2026-01-01
---

# 退货规则 v1

顾客在购买后 7 天内凭小票可以申请退货，堂食、自提、外卖订单都适用。
"""

_CURRENT_DOC = """---
doc_id: KB-902
title: 退货规则 v2
type: 政策
status: 现行
effective_from: 2026-01-01
updated_at: 2026-01-01
---

# 退货规则 v2

外卖订单在送达后 24 小时内提出，超过 24 小时不再受理。
"""


@pytest.fixture
def versioned(tmp_path):
    """一对新旧版本：旧版标了"已废止"，并用 `superseded_by` 指向新版。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "KB-901_退货规则_v1.md").write_text(_SUPERSEDED_DOC, encoding="utf-8")
    (kb / "KB-902_退货规则_v2.md").write_text(_CURRENT_DOC, encoding="utf-8")
    return build_index(kb), Retriever(build_index(kb), date(2026, 9, 1))


def test_metadata_exposes_status_under_the_key_the_retriever_reads(versioned):
    """元数据里的键名必须是 `status`——取值方读的就是这个键。

    这里原本写成 `state`，而 `Retriever._eligible` 与 `DocFacts.version_note`
    读的都是 `status`：键名对不上，两边各自安好，谁也没报错。
    """
    index, _ = versioned

    assert index.docs_meta["KB-901"]["status"] == "已废止"
    assert index.docs_meta["KB-902"]["status"] == "现行"


def test_a_superseded_version_is_not_scored_after_its_successor_takes_effect(versioned):
    """取代关系生效之后，废止版就不该再参与打分。

    这条规则一直写在 `Retriever._eligible` 里，只是因为上面那个键名对不上
    而形同虚设：废止版照常参与打分，还常常排在现行版前面。
    实测里模型因此照着废止版的"7 天内"作答，而现行版是"24 小时内"。
    """
    _, retriever = versioned
    query = "外卖订单多久内可以申请退货"

    now = [hit.doc_id for hit in retriever.search(query, top_k=5).ranked]
    assert "KB-901" not in now, "废止版不该出现在现行问题的检索结果里"
    assert "KB-902" in now, "现行版必须检索得到，否则就是修过头了"


def test_asking_about_the_past_still_gets_the_superseded_version(versioned):
    """矫枉不能过正：问的就是旧版时，废止版恰恰是答案，必须取得到。"""
    _, retriever = versioned
    query = "以前的退货规则是多久内可以申请退货"

    then = [hit.doc_id for hit in retriever.search(query, top_k=5, historical=True).ranked]
    assert "KB-901" in then, "问历史时却把旧版挡掉了，等于把答案过滤没了"


def test_a_version_that_takes_effect_later_is_not_scored_yet(tmp_path):
    """还没生效的那一版同样不该参与打分——这是同一处判定的另一头。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "KB-901_退货规则_v1.md").write_text(_SUPERSEDED_DOC, encoding="utf-8")
    (kb / "KB-902_退货规则_v2.md").write_text(_CURRENT_DOC, encoding="utf-8")
    index = build_index(kb)
    retriever = Retriever(index, date(2025, 6, 1))     # 早于 v2 的生效日期

    ranked = [hit.doc_id for hit in retriever.search("外卖订单多久内可以申请退货", top_k=5).ranked]
    assert "KB-902" not in ranked
    assert "KB-901" in ranked
