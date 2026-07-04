"""M1 · §5.4 终止完善 验收测试 —— 测试先行，见红。

覆盖任务卡条目 (f)：
  - 终止**总结 system 事件不生成 pending 派发行**（契约 §3 评审建议②：落盘时排除，或
    建后即 done；保持派发表整洁）。M0 遗留一行惰性 pending，M1 修正。
  - 终止清单含**契约 / artifacts / 分支列表 / 会话台账**（spec §5.4）。

区分（关键）：
  - "terminate 信号事件"不生成派发行 —— M0 已实现（store.append_event 对 terminate 型跳过派发行）。
  - "终止总结 system 事件"不生成 pending 派发行 —— **M1 新增**。二者是不同事件：总结 system 事件
    在 terminate 之后由编排器生成（id > terminate.id）。本文件断言的是**后者**。

驱动方式：最小化——seed 一条 to=[moderator] 触发事件，mock moderator 回 terminate（to=[]），
run_thread 处理 -> 触发终止清单 -> 生成总结 system 事件。不跑整条附录B，聚焦 §5.4。

硬约束：顶层只 import orch.scheduler / orch.store / orch.adapters（包级）；具体符号函数体内引用。
断言只依赖可观察落盘真相（events 表 / dispatches 表 / 总结事件正文），不改 M0 既有测试语义。
"""

from __future__ import annotations

from pathlib import Path

import orch.adapters  # 包级导入
import orch.scheduler
import orch.store

from tests.fixtures.m1_helpers import m1_config


# ——————————————————————————————————————————————————————————————
# 驱动辅助：最小化跑到 terminate
# ——————————————————————————————————————————————————————————————

def _term_config() -> dict:
    """moderator-only 的最小 config（m1_config 结构 + moderator 角色 can_decide）。"""
    cfg = m1_config()
    cfg["roles"] = {
        "moderator": {
            "adapter": "mock",
            "can_decide": True,
            "write_scope": [],
            "tools": [],
        },
    }
    return cfg


def _drive_to_terminate(thread_dir, tmp_dir):
    """铺契约(A)+artifact 事件，再由 moderator 发 terminate；跑到 terminated。返回 store。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")

    # A 类决策：冻结契约 v2（供终止清单"契约"段有内容）。
    st.append_event(
        sender="pm", type="decision", to=["moderator"], body="冻结契约到 v2",
        blackboard_ops=[{"op": "freeze_contract", "name": "like-api",
                         "path": "docs/like-api.md", "version": 2},
                        {"op": "set_task", "key": "backend.impl", "status": "done"}],
    )
    dec = st.events()[-1]
    orch.store.apply_blackboard_ops(st, dec["blackboard_ops"], dec["id"])

    # 携带 artifacts 的事件（供终止清单"artifacts"段有内容）。
    st.append_event(sender="backend", type="handoff", to=["tester"],
                    body="后端实现完成", artifacts=["server/like.py"])

    # 触发 moderator 的一次 invoke -> 返回 terminate（to=[]）。
    trig = st.append_event(sender="human", type="assign", to=["moderator"], body="收尾终止")
    mock = orch.adapters.MockAdapter(
        role="moderator",
        script={trig: {"to": [], "type": "terminate", "body": "全流程完成，终止线程"}},
        ledger_path=Path(tmp_dir) / "ledger.txt",
    )
    orch.scheduler.run_thread(st, _term_config(), {"moderator": mock})
    return st


def _terminate_event(store) -> dict | None:
    term = None
    for e in sorted(store.events(), key=lambda e: e["id"]):
        if e["type"] == "terminate":
            term = e
    return term


def _summary_system_event(store) -> dict | None:
    """终止总结 system 事件 = terminate 之后（id 更大）由编排器生成的 system 事件。"""
    term = _terminate_event(store)
    if term is None:
        return None
    summary = None
    for e in sorted(store.events(), key=lambda e: e["id"]):
        if e["type"] == "system" and e["id"] > term["id"]:
            summary = e  # 取 terminate 之后的（唯一）总结 system 事件
    return summary


# ==================================================================
# (f)-1：终止总结 system 事件不生成 pending 派发行
# ==================================================================

def test_terminate_summary_system_event_exists_and_not_dispatchable(thread_dir, tmp_dir):
    """§5.4：终止触发后生成一条总结 system 事件（id > terminate.id），且它**不可派发**。

    存在性是前置；M1 的可观察不变量是"该总结事件不产生 pending 派发行"（契约 §3 评审建议②）。
    合并断言 -> 该用例在 M0（总结事件带 pending moderator 派发）为红，M1 修正后转绿。
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    assert st.get_meta("status") == "terminated", "terminate 后线程应 terminated"
    summary = _summary_system_event(st)
    assert summary is not None, "§5.4：terminate 应触发一条总结 system 事件"
    assert summary["type"] == "system"
    # M1 不变量：总结事件不生成 pending 派发行。
    assert all(d["event_id"] != summary["id"] for d in st.pending_dispatches()), (
        "§5.4/契约§3：终止总结 system 事件不得可派发（无 pending 指向它）"
    )


def test_terminate_summary_system_event_has_no_pending_dispatch(thread_dir, tmp_dir):
    """(f)-1 核心：终止总结 system 事件**不生成 pending 派发行**（契约 §3 评审建议②）。

    M0 现状：总结事件以 to=[moderator] 落盘 -> 生成一行 pending 派发（惰性遗留）。
    M1 要求：落盘时排除该派发行，或建后即 done -> **不得存在指向总结事件的 pending 行**。
    断言对两种实现都成立（只看"无 pending 指向它"）。此用例在 M0 为红、M1 修正后转绿。
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    summary = _summary_system_event(st)
    assert summary is not None, "应有终止总结 system 事件"

    pending = st.pending_dispatches()
    offending = [d for d in pending if d["event_id"] == summary["id"]]
    assert not offending, (
        f"§5.4/契约§3：终止总结 system 事件(#{summary['id']}) 不得生成 pending 派发行，"
        f"实际存在 {offending}"
    )


def test_terminate_leaves_no_pending_dispatches_at_all(thread_dir, tmp_dir):
    """终止后派发表应整洁：不残留任何 pending 行（含总结事件与 terminate 本身）。

    §5.4：terminate 拒绝新派发、总结事件不建待办 -> 终态无 pending 悬挂。
    （terminate 信号本身不生成派发行 M0 已实现；本用例合并断言终态整洁。）
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    pending = st.pending_dispatches()
    assert pending == [], f"终止后不应残留任何 pending 派发行，实际 {pending}"


# ==================================================================
# (f)-2：终止清单含 契约 / artifacts / 分支列表 / 会话台账
#   注：契约/artifacts 段 M0 已有内容；为满足"M1 用例见红"，这些用例同时断言 M1 不变量
#   （总结事件不可派发），使其在 M0 为红、M1 补齐后转绿——覆盖内容与整洁两个维度。
# ==================================================================

def test_terminate_checklist_contains_contracts(thread_dir, tmp_dir):
    """§5.4 终止清单含**黑板契约**：总结正文应体现已冻结契约（like-api / v2）。

    并断言 M1 不变量（总结事件不可派发），使内容检查与 §5.4 整洁要求一并见红/转绿。
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    summary = _summary_system_event(st)
    assert summary is not None
    body = summary["body"]
    assert "like-api" in body or "契约" in body, "终止清单须汇总黑板契约（§5.4）"
    assert "2" in body, "终止清单契约段须反映版本 v2（§5.4）"
    assert all(d["event_id"] != summary["id"] for d in st.pending_dispatches()), (
        "§5.4/契约§3：终止总结 system 事件不得可派发"
    )


def test_terminate_checklist_contains_artifacts(thread_dir, tmp_dir):
    """§5.4 终止清单含**全部 artifacts**：总结正文应列出 server/like.py。

    并断言 M1 不变量（总结事件不可派发）。
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    summary = _summary_system_event(st)
    assert summary is not None
    assert "server/like.py" in summary["body"], "终止清单须汇总全部 artifacts（§5.4）"
    assert all(d["event_id"] != summary["id"] for d in st.pending_dispatches()), (
        "§5.4/契约§3：终止总结 system 事件不得可派发"
    )


def test_terminate_checklist_contains_branch_list(thread_dir, tmp_dir):
    """§5.4 终止清单含**分支列表**：总结正文须有分支段（M1 新增；mock 无 worktree 时可为空列表，
    但清单**段目**必须在，体现四项俱全）。此用例在 M0（无分支段）为红、M1 补齐后转绿。
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    summary = _summary_system_event(st)
    assert summary is not None
    assert "分支" in summary["body"], "终止清单须含分支列表段（§5.4 四项之一）"


def test_terminate_checklist_contains_session_ledger(thread_dir, tmp_dir):
    """§5.4 终止清单含**会话台账**：总结正文须有会话段。此用例在 M0（无会话段）为红、
    M1 补齐后转绿。
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    summary = _summary_system_event(st)
    assert summary is not None
    assert "会话" in summary["body"], "终止清单须含会话台账段（§5.4 四项之一）"


def test_terminate_checklist_has_all_four_sections(thread_dir, tmp_dir):
    """§5.4 综合：终止清单四项俱全（契约 / artifacts / 分支 / 会话台账）。

    以四个可辨识标记同时存在为判据（契约名 + artifact 路径 + 分支段 + 会话段）。
    """
    st = _drive_to_terminate(thread_dir, tmp_dir)
    summary = _summary_system_event(st)
    assert summary is not None
    body = summary["body"]
    missing = []
    if not ("like-api" in body or "契约" in body):
        missing.append("契约")
    if "server/like.py" not in body:
        missing.append("artifacts")
    if "分支" not in body:
        missing.append("分支列表")
    if "会话" not in body:
        missing.append("会话台账")
    assert not missing, f"§5.4 终止清单缺少段目：{missing}"
