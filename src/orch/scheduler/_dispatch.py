"""调度层内部：派发行只读观察（spec §9.1 恢复对账所需）。

契约（docs/m0-contract.md §2）冻结的 Store **公开面**不含"枚举 dispatching 行"或
"按 (id,target) 读 status/deadline/attempts"的方法——这些是恢复算法（§9.1）自身的
只读需求。契约 §7 已确立"读盘观察落盘真相合法"（测试的 `_dispatch_row` 即直接读盘）。
本模块据此对 `Store.thread_dir`（§8 冻结公开属性）下的 events.db 开**短生命周期只读连接**
枚举/读取派发行；所有**写**仍一律走冻结 store 原语（set_pending/bump_attempt/mark_done）。

WAL 语义：独立读连接可见全部已提交事务（§4.3 WAL）；恢复在启动时执行、读的是崩溃前
已落盘的委托真相，故只读旁路不违反"真相只在盘上"（§0 命题3、§16.9：编排器不在内存持有
不可从盘重建的状态——这里不持有任何状态，读完即弃）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _db_path(store) -> str:
    # 只依赖 §8 冻结公开属性 Store.thread_dir。
    return str(Path(store.thread_dir) / "events.db")


def dispatching_rows(store) -> list[dict]:
    """全部 status='dispatching' 的派发行，按 event_id 升序。

    每行含 event_id/target/status/deadline_ts/attempts（读盘观察，非私有实现）。
    """
    con = sqlite3.connect(_db_path(store))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT event_id, target, status, deadline_ts, attempts "
            "FROM dispatches WHERE status='dispatching' "
            "ORDER BY event_id ASC, target ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def dispatch_row(store, event_id: int, target: str) -> dict | None:
    """按 (event_id, target) 读单个派发行；不存在返回 None。"""
    con = sqlite3.connect(_db_path(store))
    try:
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT event_id, target, status, deadline_ts, attempts "
            "FROM dispatches WHERE event_id=? AND target=?",
            (event_id, target),
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def has_matching_reply(events: list[dict], event_id: int, target: str) -> bool:
    """§9.1 a)：events 中是否存在 sender==target 且 event_id ∈ re 的回复（纵深防御）。

    events 为 store.events() 的返回（键 'from'/'re' 已由 store 映射，§0.1）。
    """
    for ev in events:
        if ev.get("from") == target and event_id in (ev.get("re") or []):
            return True
    return False
