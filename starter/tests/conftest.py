"""测试夹具。

检索这块在测试里整个换成固定返回，这样测试就不用跟着知识库一起改，
跑起来也快。要看真实检索效果直接起服务问两句就行。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAKE_TEXT = "退款政策 v2 > 三、时限：外卖订单在订单送达后 24 小时内可以申请退款。"


@pytest.fixture
def client(tmp_path_factory):
    """接口测试用的客户端，检索换成固定返回。

    替换和还原都走同一个 `MonkeyPatch`：原来是在 `Retriever` 类上直接赋值
    （`Retriever.search = fake_search`），又是 session 作用域，所以从第一个
    用到 `client` 的测试之后，整个进程里的检索都被换成了这个假结果——
    检索自己的测试也跟着拿到 `KB-013`，却看不出来是哪儿来的。
    环境变量的改动同样要还原，不然会漏给别的测试。
    """
    patch = pytest.MonkeyPatch()
    patch.setenv("VAR_DIR", str(tmp_path_factory.mktemp("var")))
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        patch.delenv(key, raising=False)

    from fastapi.testclient import TestClient

    from kbqa import retriever as retriever_module
    from kbqa import server

    def fake_search(self, query, top_k=5, **kwargs):
        hit = retriever_module.Hit(
            doc_id="KB-013",
            chunk_id="KB-013#1",
            score=42.0,
            text=FAKE_TEXT,
            source_text=FAKE_TEXT,
            meta={"title": "退款政策 v2", "status": "现行"},
        )
        return retriever_module.SearchResult(
            hits=[hit][:top_k],
            query=query,
            terms=[],
            expansions=[],
            filtered=[],
            coverage=1.0,
        )

    patch.setattr(retriever_module.Retriever, "search", fake_search)
    try:
        yield TestClient(server.app)
    finally:
        patch.undo()
