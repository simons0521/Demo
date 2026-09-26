"""分词。中文按二元组切，英文与数字按整词。

中文没有空格，按空白切词的话一整句话只会切出一个 token，BM25 的 TF/IDF
根本无从算起。中文用**重叠二元组**（"外卖订单"→ 外卖/卖订/订单）：
不引第三方分词库（契约 §8 要求 `make rebuild` 在干净环境可跑），
而两字窗口对"退款/订单/门店"这类业务词足够准，也不会像单字那样把
"外卖"和"卖外"混成一堆。

`docfacts.py` 里按 `len(term) == 2` 给含虚字的词打折、`content_tokens`
按 `all(char in STOP_CHARS ...)` 过滤，本来就是照二元组写的。
"""

from __future__ import annotations

import re
import unicodedata

#: 分词规则变了，索引缓存必须失效。
TOKENIZER_VERSION = "tokenizer-3"

#: 中文里几乎不携带信息的字。只用在“查询覆盖率”上，索引照常保留全部词。
STOP_CHARS = frozenset("的了吗呢是在有和与及或就都也还把被给对从向于个些这那哪什么怎样如何多少几请帮我你他它可以能要想会一下少吧啊呀们么样过得着为所")
STOP_WORDS = frozenset("the a an of to in is are and or for on at it this that how what".split())

#: CJK 统一表意文字（含扩展 A 与兼容区）。相邻两字成词，切到这里就断。
_CJK = r"㐀-䶿一-鿿豈-﫿"
#: 英文单词与数字。`poke` 切成 `po`/`ok` 只会引噪声，所以整词保留。
_WORD = r"a-z0-9"
_RUN = re.compile("[%s]+|[%s]+" % (_CJK, _WORD))


def normalise(text: str) -> str:
    """全角转半角、统一大小写，比较与分词都走这一层。"""
    return unicodedata.normalize("NFKC", text or "").lower()


def tokenize(text: str) -> list[str]:
    """把一段文本切成检索用的词。

    * 中文段（长度 ≥ 2）：全部相邻二元组，逐字滑动。单字段就是它自己。
    * 英文与数字段：整词保留。
    * 标点、空格、中英边界都断开，所以 `退款，政策` 不会切出 `款，`，
      `牛肉poke` 也不会切出 `肉p`。
    """
    tokens: list[str] = []
    for match in _RUN.finditer(normalise(text)):
        run = match.group(0)
        if run[0].isascii():
            tokens.append(run)
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def content_tokens(text: str) -> list[str]:
    """去掉虚词之后的查询词，用来算“这个问题被文档覆盖了多少”。

    “虚词”指**整词都是虚字**：`的` 会被丢掉，`的可`（两个字都是虚字）也会，
    但 `在售` 要留下——它是真词，只是恰好含一个虚字。
    """
    kept = []
    for token in tokenize(text):
        if token in STOP_WORDS:
            continue
        if all(char in STOP_CHARS for char in token):
            continue
        kept.append(token)
    return kept
