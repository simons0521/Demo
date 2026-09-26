"""把文档切成检索用的小块。

切块的粒度直接决定检索能不能成立：块太大，一个块里混着好几件事，
BM25 算不出焦点；块太小，一句话被拦腰切断，问句里的词和正文里的词对不上。

所以按**结构**切，不按固定字数切：

* 标题行起新的一段，标题**跟着它下面的正文走**（`heading` 记成
  `"总标题 > 二级 > 三级"`，不带 `#`）。
* 空行分段，段内再把正文和表格分开。
* 表格**整行不切断**，超过 `CHUNK_SIZE` 时按行分组，**每组都重复表头**——
  `docfacts.render_row` 靠表头把 `| P007 | 商品007 | S03 | 7.00 |` 拼成
  "商品名称：商品007，售价：7.00"，没了表头就只剩一串光秃秃的数字。
* 正文按行累加到 `CHUNK_SIZE`，一行本身超长才按句子边界切。
* **一行正文都不许丢**。原实现是 `range(0, len(text) - CHUNK_SIZE, CHUNK_SIZE)`，
  上界少了整整一块：真实知识库 35 篇文档每篇都丢字，合计 6107 字（18.6%），
  正文丢了不报错，只会莫名其妙地召不回。

块在文档里的**先后顺序不能乱**：`units.py` 按顺序定位引用，
`docfacts` 遇到同分也优先取靠前的那一句。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .loader import Document
from .sanitize import split_sentences

#: 切块参数变了，索引缓存必须失效，所以写进缓存键里。
CHUNKER_VERSION = "chunker-3"

CHUNK_SIZE = 300

#: 标题行：`#` 到 `######` 加一个空格。
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
#: 表格的分隔行 `| --- | :--: |`，不是数据。和 `units.py` 用的是同一条规则。
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
#: 表格至少要两行才算数：表头 + 分隔行，或表头 + 数据行。
#: 单独一行带竖线的正文不算表格。
_TABLE_MIN_LINES = 2


@dataclass
class Chunk:
    doc_id: str
    chunk_id: str
    text: str
    source_text: str
    heading: str = ""
    kind: str = "text"
    table_header: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "text": self.text,
            "source_text": self.source_text,
            "heading": self.heading,
            "kind": self.kind,
            "table_header": self.table_header,
        }


def _heading_line(line: str) -> tuple[int, str]:
    """标题行 → （层级, 去掉 `#` 的标题文字）。不是标题就是 (0, "")。"""
    match = _HEADING.match(line)
    if not match:
        return 0, ""
    return len(match.group(1)), match.group(2).strip()


def _blocks(text: str) -> list[tuple[list[str], list[str]]]:
    """把正文按标题切成（标题路径, 行列表）。

    标题行自己起一个块并且**留在块里**——`units.py` 要拿标题行当可引用单位，
    块里没有它，标题就认不出来。

    只按标题分段，**不按空行**：空行分出来的段常常只有一句话，
    真实知识库上会切出一堆 7 字、11 字的碎片。BM25 的长度归一化专门优待短块，
    这些碎片会把真正的答案顶下去。段内的长度由 `CHUNK_SIZE` 兜着。
    """
    blocks: list[tuple[list[str], list[str]]] = []
    stack: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if any(line.strip() for line in current):
            blocks.append((list(stack), list(current)))
        current.clear()

    for line in text.splitlines():
        level, title = _heading_line(line)
        if level:
            flush()
            del stack[level - 1 :]  # 同级或更深的标题出栈，浅的留着
            stack.append(title)
            current.append(line)
            continue
        current.append(line)
    flush()
    return blocks


def _is_separator(line: str) -> bool:
    return bool(_TABLE_SEP.match(line.strip()))


def _table_span(lines: list[str]) -> tuple[int, int] | None:
    """从 `start` 起是不是一张表；是就返回（正文行数, 表格行数）。

    表头行不计入正文——表头跟着表格走。
    """
    run = 0
    for line in lines:
        if line.strip().startswith("|"):
            run += 1
            continue
        break
    if run >= _TABLE_MIN_LINES:
        return run
    return None


def _table_header_of(lines: list[str]) -> tuple[str, list[str], int]:
    """（表头原样含分隔行, 表头单元格, 表头占了几行）。

    分隔行跟着表头一起复制到每一块里：`units.py` 靠它把表头行和数据行分开。
    """
    head = 0
    while head < len(lines) and _is_separator(lines[head]):
        head += 1
    if head >= len(lines):
        return "", [], 0
    prefix = [lines[head].strip()]
    if head + 1 < len(lines) and _is_separator(lines[head + 1]):
        prefix.append(lines[head + 1].strip())
    cells = [cell.strip() for cell in prefix[0].strip("|").split("|")]
    return "\n".join(prefix), cells, len(prefix)


def _table_pieces(header: str, rows: list[str]) -> list[str]:
    """数据行按 `CHUNK_SIZE` 分组，**每组都重复表头**。"""
    pieces: list[str] = []
    current: list[str] = []
    used = len(header) + 1
    for row in rows:
        stripped = row.strip()
        if current and used + len(stripped) + 1 > CHUNK_SIZE:
            pieces.append(header + "\n" + "\n".join(current))
            current, used = [], len(header) + 1
        current.append(stripped)
        used += len(stripped) + 1
    if current:
        pieces.append(header + "\n" + "\n".join(current))
    return pieces


def _split_long_line(line: str) -> list[str]:
    """超长的一行按句子切开。一整句还超长就只能硬切，这是最后的兜底。"""
    pieces: list[str] = []
    buffer = ""
    for sentence in split_sentences(line):
        if buffer and len(buffer) + len(sentence) > CHUNK_SIZE:
            pieces.append(buffer)
            buffer = ""
        if len(sentence) > CHUNK_SIZE:
            if buffer:
                pieces.append(buffer)
                buffer = ""
            pieces.extend(
                sentence[start : start + CHUNK_SIZE]
                for start in range(0, len(sentence), CHUNK_SIZE)
            )
            continue
        buffer += sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def _prose_pieces(lines: list[str]) -> list[str]:
    """正文按行累加到 `CHUNK_SIZE`；一行本身超长才按句子边界切。"""
    pieces: list[str] = []
    buffer: list[str] = []
    used = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) > CHUNK_SIZE:
            if buffer:
                pieces.append("\n".join(buffer))
                buffer, used = [], 0
            pieces.extend(_split_long_line(stripped))
            continue
        if buffer and used + len(stripped) + 1 > CHUNK_SIZE:
            pieces.append("\n".join(buffer))
            buffer, used = [], 0
        buffer.append(stripped)
        used += len(stripped) + 1
    if buffer:
        pieces.append("\n".join(buffer))
    return pieces


def _block_chunks(document: Document, heading: str, lines: list[str]) -> list[Chunk]:
    """一个块：表格与正文按**原文顺序**交替产出。"""
    chunks: list[Chunk] = []
    position = 0
    prose: list[str] = []

    def flush_prose() -> None:
        for piece in _prose_pieces(prose):
            chunks.append(
                Chunk(
                    doc_id=document.doc_id,
                    chunk_id="",
                    text=piece,
                    source_text=piece,
                    heading=heading,
                    kind="text",
                )
            )
        prose.clear()

    while position < len(lines):
        run = _table_span(lines[position:])
        if run is None:
            prose.append(lines[position])
            position += 1
            continue
        flush_prose()
        segment = lines[position : position + run]
        header, cells, consumed = _table_header_of(segment)
        if not header:
            # 整段都是分隔行，不是表，当正文处理。
            prose.extend(segment)
            position += run
            continue
        rows = [line for line in segment[consumed:] if not _is_separator(line)]
        for piece in _table_pieces(header, rows):
            chunks.append(
                Chunk(
                    doc_id=document.doc_id,
                    chunk_id="",
                    text=piece,
                    source_text=piece,
                    heading=heading,
                    kind="table",
                    table_header=cells,
                )
            )
        position += run

    flush_prose()
    return chunks


def chunk_document(document: Document) -> list[Chunk]:
    """按结构切块，一行正文都不丢。"""
    chunks: list[Chunk] = []
    for path, lines in _blocks(document.text):
        #: `units.py` 拿这个字符串去和标题行比对，所以只放标题文字、不放 `#`。
        heading = " > ".join(path) or document.title
        chunks.extend(_block_chunks(document, heading, lines))

    if not chunks:
        piece = document.text.strip() or document.title
        chunks.append(
            Chunk(
                doc_id=document.doc_id,
                chunk_id="",
                text=piece,
                source_text=piece,
                heading=document.title,
            )
        )

    for number, chunk in enumerate(chunks, start=1):
        chunk.chunk_id = "%s#%d" % (document.doc_id, number)
    return chunks


def chunk_documents(documents: list[Document]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document))
    return chunks
