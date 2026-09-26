"""对话历史。

按 `session_id` 分开存：一段会话的追问只能看到**它自己**的上文。

原来是一个全局列表，`session_id` 收下就扔进 `_turns` 的同一个桶里，
`max_sessions` 存了也从没用过。后果有两个方向：

* **串话**：A 问完"6 月的净营业额"，B 再问"那 7 月呢"会被补成 A 那一轮的
  追问——答的是别人的问题；
* **互相挤**：`max_turns` 截的是整个桶的尾巴，别人多问几句就把你的上文挤没了。

所以键是必须的，匿名请求单独处理：没有 `session_id` 就**不参与会话**。
宁可把它当成孤立的单轮（追问会得到"补完整一点"的反问），也不能塞进一个
公共桶里。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional

MAX_TURNS = 6
MAX_SESSIONS = 500


class SessionStore:
    """最近几轮对话，够解追问就行。"""

    def __init__(self, max_sessions: int = MAX_SESSIONS, max_turns: int = MAX_TURNS) -> None:
        self._turns: "OrderedDict[str, list[dict]]" = OrderedDict()
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.max_turns = max_turns

    @staticmethod
    def _key(session_id: Optional[str]) -> Optional[str]:
        """规范化会话标识；没给出有效的就返回 None，表示"不参与会话"。"""
        return (session_id or "").strip() or None

    def history(self, session_id: Optional[str]) -> list[dict]:
        key = self._key(session_id)
        if key is None:
            return []
        with self._lock:
            # 交出去的是副本：调用方（比如规划器的追问还原）改它不该动到存着的那份。
            return list(self._turns.get(key, ()))

    def append(self, session_id: Optional[str], turn: dict) -> None:
        key = self._key(session_id)
        if key is None:
            return
        with self._lock:
            turns = self._turns.setdefault(key, [])
            turns.append(turn)
            del turns[: max(0, len(turns) - self.max_turns)]
            # 刚聊过就算"最近用过"，淘汰时排在后面。
            self._turns.move_to_end(key)
            while len(self._turns) > self.max_sessions:
                self._turns.popitem(last=False)

    def clear(self, session_id: Optional[str] = None) -> None:
        """清掉一段会话；不传就全清。"""
        key = self._key(session_id)
        with self._lock:
            if key is None:
                self._turns.clear()
                return
            self._turns.pop(key, None)
