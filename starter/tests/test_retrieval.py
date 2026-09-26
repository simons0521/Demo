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

from kbqa.chunker import CHUNK_SIZE, chunk_document
from kbqa.index import content_key
from kbqa.loader import Document, load_knowledge_base
from kbqa.tokenizer import content_tokens, normalise, tokenize

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


# -- 真实知识库的不变量（不写死任何数字） ----------------------------------------


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
