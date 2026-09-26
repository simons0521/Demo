"""追踪：一次问答的每一步、耗时、错误都记下来，调试面板用。"""

from __future__ import annotations

import threading
import time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Trace:
    trace_id: str
    question: str
    session_id: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="milliseconds"))
    steps: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    def step(self, name: str, payload: Any = None, started: Optional[float] = None) -> None:
        now = time.perf_counter()
        self.steps.append(
            {
                "step": name,
                "at_ms": round((now - self._t0) * 1000, 1),
                "took_ms": round((now - started) * 1000, 1) if started else None,
                "detail": payload,
            }
        )

    def error(self, where: str, exc: BaseException) -> None:
        """真实原因要留下来：类型、消息、堆栈，一个都不少。"""
        self.errors.append(
            {
                "where": where,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
        )

    def llm(self, payload: dict) -> None:
        self.llm_calls.append(payload)

    def as_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "question": self.question,
            "started_at": self.started_at,
            "total_ms": round((time.perf_counter() - self._t0) * 1000, 1),
            "steps": self.steps,
            "llm_calls": self.llm_calls,
            "errors": self.errors,
        }


class TraceStore:
    def __init__(self, capacity: int = 200) -> None:
        self._data: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = threading.Lock()
        self.capacity = capacity
        self._counter = 0

    def new_id(self, today: str) -> str:
        with self._lock:
            self._counter += 1
            return "t-%s-%04d" % (today.replace("-", ""), self._counter)

    def save(self, trace: Trace) -> None:
        with self._lock:
            self._data[trace.trace_id] = trace.as_dict()
            self._data.move_to_end(trace.trace_id)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def get(self, trace_id: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(trace_id)

    def recent(self, limit: int = 20) -> list[dict]:
        """最近几次问答的**摘要**，调试面板用来挑一条进去看。

        只给摘要，**绝不返回完整 `steps`**：一次问答的 trace 有大几十 KB
        （检索明细是大头），20 份叠起来这个响应就没法用了；面板在这儿要的
        本来也只是"有哪些次问答"。要看细节走 `/api/trace/{trace_id}`。

        最新的排最前面：列表是给人从上往下看的。
        """
        with self._lock:
            items = list(self._data.values())[-max(1, limit) :]
        return [self._summary(payload) for payload in reversed(items)]

    @staticmethod
    def _summary(payload: dict) -> dict:
        return {
            "trace_id": payload.get("trace_id"),
            "session_id": payload.get("session_id"),
            "question": payload.get("question"),
            "started_at": payload.get("started_at"),
            "total_ms": payload.get("total_ms"),
            # 三个都是**计数**，不是内容。名字里带 `count` 是故意的：完整 trace 里
            # `llm_calls` 是个列表，摘要里同名字段放个整数，读的人迟早会当成列表用。
            "step_count": len(payload.get("steps") or []),
            "llm_call_count": len(payload.get("llm_calls") or []),
            "error_count": len(payload.get("errors") or []),
        }
