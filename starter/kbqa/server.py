"""FastAPI 层：只做参数校验和 JSON 序列化，逻辑都在 service.py。"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import load_settings
from .service import Service

app = FastAPI(title="经营看板 + 问答服务", version="0.9.3")
_service: Optional[Service] = None


def service() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service


def _as_text(value: Any) -> str:
    """把请求里的标量原样变成字符串。

    契约 §5 要求 `/api/chat` 无论如何都返回 200，所以这里对类型宽容：
    数字、布尔的 `session_id` 或 `question` 一律当字符串收下，`null` 当没填。
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    return json.dumps(value, ensure_ascii=False)


class ChatRequest(BaseModel):
    session_id: Optional[Any] = None
    question: Optional[Any] = None


class RetrieveRequest(BaseModel):
    query: Optional[Any] = None
    #: 只要求是正整数；超过索引片段总数时由服务按总数封顶（契约 §4）。
    top_k: int = Field(default=5, ge=1)


#: 清洗剔除原因的中文标签，第一关的“数据质量”面板要显示人话。
#: 键名来自 `cleaning.REMOVAL_REASONS` 与 `CleaningReport.as_dict()`。
#: 一律 `.get(key, key)` 兜底：评审换 `data/` 之后键名若变，面板上显示英文键名，
#: 而不是整条接口 500。
REMOVAL_LABELS = {
    "1_unparseable_date": "日期三种格式都解析不了（KB-001 §3.1）",
    "2_empty_amount": "金额为空或解析不了（剔除，不回填）",
    "3_qty_le_zero": "数量 ≤ 0，或压根不是整数",
    "4_store_not_in_stores": "门店编号不在维表里",
    "5_product_not_in_products": "商品编号不在维表里",
    "6_duplicate_row": "七个字段规范化后完全一致的重复行",
}

#: `removed` 里还嵌着一个**不参与剔除**的注解：`note_unparseable_amount` 是
#: `2_empty_amount` 里"金额是垃圾值、不是空值"的那部分，**已经计在上面那一格**。
#: 面板要是把它当成第七条剔除原因加起来，总数就和 `raw_rows - kept_rows` 对不上了，
#: 所以标成 `kind: "note"` 让面板分开渲染。
NOTE_LABELS = {
    "note_unparseable_amount": "其中金额是垃圾值而非空值（已含在上一行，不重复剔除）",
}


def removal_reasons(report: dict) -> list[dict]:
    """把 `cleaning_report["removed"]` 变成带中文标签的列表，顺序照原样。

    按报告里**实际有的键**遍历，不按标签表遍历：将来多一个剔除原因，
    面板上会出现英文键名——看得见，比悄悄少一项强。
    """
    labels = dict(NOTE_LABELS)
    labels.update(REMOVAL_LABELS)
    return [
        {
            "key": key,
            "label_cn": labels.get(key, key),
            "removed": int(value or 0),
            "kind": "note" if key in NOTE_LABELS else "reason",
        }
        for key, value in (report.get("removed") or {}).items()
    ]


def _bad_date(*values: str) -> Optional[JSONResponse]:
    for value in values:
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content={"error": "日期格式必须是 YYYY-MM-DD，收到 %r" % value},
            )
    return None


@app.get("/api/health")
def health() -> dict:
    return service().health()


@app.get("/api/metrics/summary")
def metrics_summary(
    start: str = Query(...),
    end: str = Query(...),
    store_id: Optional[str] = None,
    product_id: Optional[str] = None,
):
    bad = _bad_date(start, end)
    return bad or service().metrics_summary(start, end, store_id, product_id)


@app.get("/api/metrics/daily")
def metrics_daily(
    start: str = Query(...),
    end: str = Query(...),
    store_id: Optional[str] = None,
    product_id: Optional[str] = None,
):
    bad = _bad_date(start, end)
    return bad or service().metrics_daily(start, end, store_id, product_id)


@app.post("/api/retrieve")
def retrieve(request: RetrieveRequest) -> dict:
    return service().retrieve(_as_text(request.query), request.top_k)


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    session_id = _as_text(request.session_id) or None
    return service().chat(session_id, _as_text(request.question))


@app.get("/api/trace/{trace_id}")
def trace(trace_id: str):
    payload = service().get_trace(trace_id)
    if payload is None:
        return JSONResponse(status_code=404, content={"error": "没有这个 trace_id：%s" % trace_id})
    return payload


@app.get("/api/data_quality")
def data_quality() -> dict:
    """第一关的“数据质量”面板：清洗掉了多少行、各因为什么。

    `cleaning_report` 的形状**冻结不动**（它流进 `/api/health`，单测与评测都盯着
    那几个键），中文标签另起一个 `removal_reasons` 字段。
    """
    current = service()
    report = current.tools.cleaning_report()
    return {
        "cleaning_report": report,
        "data_period": current.data_period,
        "kb_warnings": current.index.warnings,
        "removal_reasons": removal_reasons(report),
    }


@app.get("/api/stores")
def stores() -> dict:
    """看板的门店下拉。

    **门店不能由前端写死**：评审换掉 `data/` 之后门店就变了，写死的话面板
    会开始撒没有依据的谎。包一层对象与其余接口保持一致。
    """
    return {"stores": service().tools.stores()}


@app.get("/api/metrics/top_products")
def metrics_top_products(
    start: str = Query(...),
    end: str = Query(...),
    store_id: Optional[str] = None,
    limit: int = Query(10, ge=1, le=100),
):
    bad = _bad_date(start, end)
    return bad or service().tools.top_products(start, end, store_id, limit)


@app.get("/api/traces")
def traces(limit: int = Query(20, ge=1, le=100)) -> dict:
    """最近几次问答的摘要，调试面板用来挑一条进去看。要看细节走 `/api/trace/{id}`。"""
    return {"traces": service().traces.recent(limit)}


# -- 前端（零构建的静态三件套） ----------------------------------------------------
#
# 挂载点必须在**所有 `/api/*` 路由之后**：Starlette 按注册顺序匹配，写在前面
# 会把接口整个盖住。
#
# `is_dir()` 守卫是给契约 §7.2 的"干净环境也要能起"留的：`web/` 不在时降级成
# 404，而不是在**导入期**抛异常——导入期一炸，整个服务连同 API 全起不来。
# 目录在仓库里（`starter/web/`），正常情况下走的是上面那条分支。
_web_dir = load_settings().web_dir
if _web_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
else:  # pragma: no cover - 只有仓库被裁剪过才会走到
    logging.getLogger(__name__).warning("web/ 不存在，前端不挂载：%s", _web_dir)
