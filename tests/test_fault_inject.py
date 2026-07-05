"""M4-T1 · 故障注入钩子测试（先行见红）。

覆盖任务卡条目 (a)：
  - `orch.store.FaultInjector`：可设 (site, count) 触发 `SystemExit(137)`；
  - `orch.store` 三个关键落盘方法内嵌检查点：
      · append_event
      · mark_dispatching
      · reply_and_done
  - 全局注入器通过 `orch.store.set_fault_injector(...)` 注入 / 清除。

约束（CLAUDE.md / M4 契约 §1）：
  - 顶层只 `import orch.store`；被测符号在函数体内引用（未实现 → AttributeError）。
  - 断言 137 而非 -9：spec §9.4 "SIGKILL" 语义在 Windows/POSIX 下统一由 SystemExit(137)
    表达（M4 契约 §1 明列），harness 捕获后当作"kill -9"处理并触发下一轮 recover。
  - "特定 (site, count)"：同一 site 触发第 count 次时才抛，其余 pass；count=0 视为立即触发。
"""

from __future__ import annotations

import pytest

import orch.store


# ==================================================================
# (a-1) FaultInjector 存在 + check(site) 未命中时静默通过
# ==================================================================

def test_fault_injector_no_trigger_when_site_not_configured():
    """未针对某 site 配置的 check() 必须直接返回，不影响正常路径。"""
    fi = orch.store.FaultInjector({"append_event_post": 1})
    # 未配置的 site 名不应触发。
    fi.check("mark_dispatching_post")
    fi.check("reply_and_done_post")


# ==================================================================
# (a-2) FaultInjector 命中 (site, count) → SystemExit(137)
# ==================================================================

def test_fault_injector_triggers_systemexit_137_on_matched_site():
    """(site, count) 首次命中 → 抛 SystemExit(137) 模拟 kill -9。"""
    fi = orch.store.FaultInjector({"append_event_post": 1})
    # 首次调用命中 count=1；code 137 = 128+SIGKILL(9)，spec §9.4 语义。
    with pytest.raises(SystemExit) as ei:
        fi.check("append_event_post")
    assert ei.value.code == 137


def test_fault_injector_respects_count_semantics():
    """count=N 表示"第 N 次调用命中"；前 N-1 次不抛。"""
    fi = orch.store.FaultInjector({"append_event_post": 3})
    # 前两次 pass，第三次抛。
    fi.check("append_event_post")
    fi.check("append_event_post")
    with pytest.raises(SystemExit) as ei:
        fi.check("append_event_post")
    assert ei.value.code == 137


# ==================================================================
# (a-3) 全局注入 / 清除接口存在，且不注入时 store 行为无变化
# ==================================================================

def test_store_set_fault_injector_none_is_noop(thread_dir):
    """set_fault_injector(None) 应清除注入器；不注入时 Store 一切照常。"""
    st = orch.store.Store(thread_dir)
    # 清除（若之前被别的用例设过）。
    orch.store.set_fault_injector(None)
    eid = st.append_event(sender="human", type="assign", body="hi", to=["moderator"])
    assert eid >= 1
    assert st.pending_dispatches(), "无注入时 append_event 应正常生成派发行"


# ==================================================================
# (a-4) append_event 内嵌检查点：注入后事务 commit 后崩溃
# ==================================================================

def test_append_event_embeds_fault_check_post_commit(thread_dir):
    """append_event 事务提交后应调用一次 fi.check('append_event_post')。

    验证方式：装一个只在 site='append_event_post' 抛 137 的注入器；
    第一次 append_event 应因该检查点抛 SystemExit(137)。
    """
    st = orch.store.Store(thread_dir)
    orch.store.set_fault_injector(
        orch.store.FaultInjector({"append_event_post": 1})
    )
    try:
        with pytest.raises(SystemExit) as ei:
            st.append_event(sender="human", type="assign", body="x", to=["m"])
        assert ei.value.code == 137
    finally:
        orch.store.set_fault_injector(None)


# ==================================================================
# (a-5) mark_dispatching 内嵌检查点：注入后事务 commit 后崩溃
# ==================================================================

def test_mark_dispatching_embeds_fault_check_post_commit(thread_dir):
    """mark_dispatching 事务提交后应调用一次 fi.check('mark_dispatching_post')。"""
    st = orch.store.Store(thread_dir)
    # 先无注入正常 append 一条事件。
    eid = st.append_event(sender="human", type="assign", body="x", to=["m"])
    orch.store.set_fault_injector(
        orch.store.FaultInjector({"mark_dispatching_post": 1})
    )
    try:
        with pytest.raises(SystemExit) as ei:
            st.mark_dispatching(eid, "m", deadline_ts=99.0)
        assert ei.value.code == 137
    finally:
        orch.store.set_fault_injector(None)


# ==================================================================
# (a-6) reply_and_done 内嵌检查点：注入后事务 commit 后崩溃
# ==================================================================

def test_reply_and_done_embeds_fault_check_post_commit(thread_dir):
    """reply_and_done 事务提交后应调用一次 fi.check('reply_and_done_post')。"""
    st = orch.store.Store(thread_dir)
    eid = st.append_event(sender="human", type="assign", body="x", to=["m"])
    st.mark_dispatching(eid, "m", deadline_ts=99.0)
    reply = {
        "from": "m", "to": ["human"], "type": "chat", "body": "ack",
        "re": [eid],
    }
    orch.store.set_fault_injector(
        orch.store.FaultInjector({"reply_and_done_post": 1})
    )
    try:
        with pytest.raises(SystemExit) as ei:
            st.reply_and_done(
                done_event_id=eid, done_target="m", reply=reply, session=None,
            )
        assert ei.value.code == 137
    finally:
        orch.store.set_fault_injector(None)


# ==================================================================
# (a-7) 五个 §4.4 事务边界的 site 名齐备（M4 契约 §1）
# ==================================================================

def test_fault_injector_all_five_gap_sites_recognized(thread_dir):
    """M4 契约 §1：故障注入钩子必须覆盖 §4.4 五个事务边界的 site 名。

    仅验证 site 名可被 FaultInjector 识别（配置后命中即抛，未命中即通过），
    不校验它们是否已"落"到具体调用点（那属于 harness 综合覆盖，见 test_chaos.py）。
    """
    site_names = [
        "append_event_post",       # (1) 事件追加 + 派发行 单事务 提交后
        "mark_dispatching_post",   # (2) status→dispatching 提交后
        "invoke_post",             # (3) invoke 结束后 / autocommit 前
        "autocommit_post",         # (4) worktree autocommit 后
        "reply_and_done_post",     # (5) 回复落盘 + 标 done + 会话 upsert 提交后
    ]
    for site in site_names:
        fi = orch.store.FaultInjector({site: 1})
        with pytest.raises(SystemExit) as ei:
            fi.check(site)
        assert ei.value.code == 137, f"{site}: 应抛 SystemExit(137)"
