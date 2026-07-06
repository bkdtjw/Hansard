"""端到端验收测试（spec §5.1 核心环 + 附录B "点赞功能"）。

覆盖任务卡条目 (f)：
  - §5.1 核心环 + 附录B：事件类型序列一致。
  - 黑板终态：contracts 到 v2 冻结、tasks 全 done。

驱动方式（契约 §4.1 / §4 / §8）：
  · run_thread(store, config, adapters) 跑到 thread status ∈ {suspended, terminated} 返回。
  · human approve（E14）与 CI 回调（E15）经**契约 §8 冻结**的
    orch.scheduler.apply_gate_decision(store, config, adapters, corr=…, approve=True)
    产生：一处入口即完成 gate_decision(E14) + gate_wait 标 done + resume + 系统执行器 run_ci
    回调 system 事件(E15)。这是 spec §10/§5.5"人类批准 → 系统执行器 → system 事件入队"控制流
    在 M0 单线程串行下的忠实复现；E2E 不再直连 sqlite（契约 §8 裁决效果）。

硬约束：顶层只 import 包；具体符号在函数体内引用。断言只依赖 spec 保证的可观察终态，
不依赖未冻结的内部实现细节。§16.1 自查：路由只认 to 字段——E2E 不从 body 解析 @。

注：断言以**类型序列一致 + 黑板终态**为准（附录B 明文允许事件号偏移）。附录B 原始清单相对
spec §3.3/§5.1/§5.4 的两处对齐见 tests/fixtures/like_feature.yaml 抬头与 tests/helpers.py。
"""

from __future__ import annotations

import time
from pathlib import Path

import orch.adapters  # 包级导入
import orch.scheduler
import orch.store

from tests.helpers import EXPECTED_TYPE_SEQUENCE


def _build_adapters(like_feature_script, ledger_path):
    """按角色切片脚本，各建一个 MockAdapter，共享一个 ledger（§9.4）。"""
    return {
        role: orch.adapters.MockAdapter(
            role=role, script=table, ledger_path=ledger_path
        )
        for role, table in like_feature_script.items()
    }


def _config():
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        # gate_ops 用跨平台无害命令验证控制流（契约 §6.5），不真 merge。
        # 单一 run_ci（async）：approve → 系统执行器登记 jobs → 同步执行（M0 退化）→ 回调
        # 恰一条 system 事件（E15）。附录B 的 merge_main 在 M0 无害命令世界中并入 CI 校验，
        # 不再单列 system 事件（否则会多出一条 system、打断类型序列；见 helpers 抬头）。
        "gate_ops": {
            "run_ci": {"cmd": "python -c \"print('ci ok')\"", "cwd": ".", "async": True},
        },
        # 角色能力：写域用于并行判定（M0 串行，仅登记）；can_decide 决定 bb_ops 门槛与发送者约束。
        "roles": {
            "moderator": {"can_decide": True, "write_scope": [], "tools": []},
            "pm": {"can_decide": True, "write_scope": ["docs/"], "tools": ["Edit", "Write"]},
            "backend": {"can_decide": False, "write_scope": ["server/"],
                        "tools": ["Edit", "Write"]},
            "frontend": {"can_decide": False, "write_scope": ["web/"],
                         "tools": ["Edit", "Write"]},
            "tester": {"can_decide": False, "write_scope": ["tests/", "reports/"],
                       "tools": ["Edit", "Write"],
                       "verify": {"cmd": "python -c \"print('ok')\"", "cwd": "."}},
        },
    }


def _seed_e1(store):
    """E1：human → ∅ (assign)，兜底路由 → moderator。"""
    return store.append_event(
        sender="human", type="assign", body="帖子支持点赞/取消赞", to=[]
    )


def _types_in_order(store) -> list[str]:
    return [e["type"] for e in sorted(store.events(), key=lambda e: e["id"])]


# ——————————————————————————————————————————————————————————————
# 第一段：E1 → 跑到门禁（E13）挂起
# ——————————————————————————————————————————————————————————————

def test_e2e_runs_until_gate_suspends(thread_dir, like_feature_script, tmp_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    adapters = _build_adapters(like_feature_script, tmp_dir / "ledger.txt")
    _seed_e1(st)

    orch.scheduler.run_thread(st, _config(), adapters)

    # 到 gate_request(to=[human]) 处线程挂起（§10）。
    assert st.get_meta("status") == "suspended", "遇 gate_request 应挂起"
    types = _types_in_order(st)
    assert "gate_request" in types, "应产生 gate_request 事件"
    # 挂起点之前已冻结契约 v2（E6）。
    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 2, "挂起前契约应到 v2"


# ——————————————————————————————————————————————————————————————
# 完整流程：E1 → 门禁挂起 → approve+CI回调 → 续跑到 terminate
# ——————————————————————————————————————————————————————————————

def _find_gate_request(store):
    for e in sorted(store.events(), key=lambda e: e["id"]):
        if e["type"] == "gate_request":
            return e
    return None


def _human_approve(store, config, adapters):
    """§10 `orch approve` 的编排器入口 —— 经**契约 §8 冻结**的
    `orch.scheduler.apply_gate_decision`（替代早期裸 sqlite / store 原语注入）。

    该入口在一处忠实复现 spec §10/§5.5 控制流：
      ① 产生 gate_decision 事件（from=human, corr 回填, to=[原 gate_request.sender]）；
      ② gate_wait 行标 done → thread status='running'（resume）；
      ③ approve 关联特权操作 → 系统执行器按 config['gate_ops'] 执行 run_ci（M0 同步退化，
         经 jobs 登记）→ 回调 system 事件 to=[callback_to]、corr 回填（= 附录B E15）。
    E2E 不再直连 sqlite；gate_decision(E14) 与 CI 回调(E15) 均由本入口产生。
    """
    gate = _find_gate_request(store)
    assert gate is not None, "应已产生 gate_request(E13)"
    corr = gate.get("corr") or "gate-01"
    orch.scheduler.apply_gate_decision(
        store, config, adapters, corr=corr, approve=True, sender="human",
    )


def test_e2e_full_sequence_and_blackboard_terminal(thread_dir, like_feature_script, tmp_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = _config()
    adapters = _build_adapters(like_feature_script, tmp_dir / "ledger.txt")
    _seed_e1(st)

    # 段1：跑到门禁挂起。
    orch.scheduler.run_thread(st, cfg, adapters)
    assert st.get_meta("status") == "suspended"

    # 段2：人类 approve + 系统执行器 CI 回调（契约 §8 冻结的 apply_gate_decision）。
    _human_approve(st, cfg, adapters)

    # 段3：续跑到 terminate。
    orch.scheduler.run_thread(st, cfg, adapters)
    assert st.get_meta("status") == "terminated", "terminate 后线程应终止"

    # —— 断言 (f)-1：事件类型序列一致（附录B 允许事件号偏移，故比类型序列）——
    types = _types_in_order(st)
    assert types == EXPECTED_TYPE_SEQUENCE, (
        f"事件类型序列不符附录B\n实际: {types}\n期望: {EXPECTED_TYPE_SEQUENCE}"
    )

    # —— 断言 (f)-2：黑板终态——contracts 到 v2 冻结、tasks 全 done ——
    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 2, "终态契约必须冻结在 v2"
    assert state["tasks"], "终态应有任务状态投影"
    assert all(v == "done" for v in state["tasks"].values()), \
        f"终态所有任务必须 done: {state['tasks']}"


def test_e2e_terminate_has_no_dispatch_row(thread_dir, like_feature_script, tmp_dir):
    # §5.4：terminate 落盘不生成派发行。
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = _config()
    adapters = _build_adapters(like_feature_script, tmp_dir / "ledger.txt")
    _seed_e1(st)
    orch.scheduler.run_thread(st, cfg, adapters)
    _human_approve(st, cfg, adapters)
    orch.scheduler.run_thread(st, cfg, adapters)

    term = None
    for e in sorted(st.events(), key=lambda e: e["id"]):
        if e["type"] == "terminate":
            term = e
    assert term is not None, "应有 terminate 事件"
    assert all(d["event_id"] != term["id"] for d in st.pending_dispatches()), \
        "terminate 不应生成待办派发行"


def test_e2e_acceptance_requires_verify_exit_code(thread_dir, like_feature_script, tmp_dir):
    """§8.3：acceptance 生效需 meta.verify.exit_code == 0（编排器亲自执行 verify）。

    tester 的两次 acceptance（E12/E18）落盘后，其 meta.verify.exit_code 必须存在且为 0。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = _config()
    adapters = _build_adapters(like_feature_script, tmp_dir / "ledger.txt")
    _seed_e1(st)
    orch.scheduler.run_thread(st, cfg, adapters)
    _human_approve(st, cfg, adapters)
    orch.scheduler.run_thread(st, cfg, adapters)

    acc = [e for e in st.events() if e["type"] == "acceptance"]
    assert acc, "应有 acceptance 事件"
    for e in acc:
        verify = (e.get("meta") or {}).get("verify") or {}
        assert verify.get("exit_code") == 0, \
            f"acceptance(#{e['id']}) 需 meta.verify.exit_code==0，实际 {verify}"


def test_e2e_ledger_no_duplicate_event_ids(thread_dir, like_feature_script, tmp_dir):
    """§9.4：整条 E2E 跑通后，mock ledger 无重复事件号（exactly-once）。"""
    ledger = tmp_dir / "ledger.txt"
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = _config()
    adapters = _build_adapters(like_feature_script, ledger)
    _seed_e1(st)
    orch.scheduler.run_thread(st, cfg, adapters)
    _human_approve(st, cfg, adapters)
    orch.scheduler.run_thread(st, cfg, adapters)

    lines = [ln for ln in Path(ledger).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == len(set(lines)), f"ledger 出现重复事件号: {lines}"


# ——————————————————————————————————————————————————————————————
# §10 corr 缺省生成（"非正式门禁"）：任意 to=[human] 信封挂起后也可 approve 恢复
# —— 真实联跑发现（calc 线程：moderator 发 handoff→human 后线程无法恢复）。
# spec §10："调度器遇到 target=human 的 pending 行时置 gate_wait…corr 缺省时
# 由编排器生成 `gate-{事件号}`"；§5.1 把**所有** target=human 行送入该机制，
# 故 approve 的可恢复性不得只限 type=gate_request。
# ——————————————————————————————————————————————————————————————

import pytest
import sqlite3


def _informal_gate_setup(thread_dir, tmp_dir):
    """E1 human assign→[pm]；pm(mock) 回 handoff→[human]（无 corr）→ 线程挂起。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="做完通知我", to=["pm"])
    adapters = {
        "pm": orch.adapters.MockAdapter(
            role="pm",
            script={1: {"to": ["human"], "type": "handoff",
                        "body": "已完成，请人工确认收尾。"}},
            ledger_path=tmp_dir / "ledger.txt",
        ),
    }
    cfg = {**_config(), "gate_ops": {}}   # 无特权操作：只验恢复语义
    orch.scheduler.run_thread(st, cfg, adapters)
    return st, cfg


def _dispatch_status(thread_dir, event_id, target):
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    row = con.execute(
        "select status from dispatches where event_id=? and target=?",
        (event_id, target),
    ).fetchone()
    con.close()
    return row[0] if row else None


def test_informal_gate_generated_corr_approve_resumes(thread_dir, tmp_dir):
    """§10 corr 缺省生成：approve gate-{事件号} 应恢复非正式门禁挂起的线程。"""
    st, cfg = _informal_gate_setup(thread_dir, tmp_dir)
    assert st.get_meta("status") == "suspended", "handoff→human 应挂起（§5.1）"
    assert _dispatch_status(thread_dir, 2, "human") == "gate_wait"

    orch.scheduler.apply_gate_decision(st, cfg, {}, corr="gate-2", approve=True)

    assert st.get_meta("status") == "running", "§10：approve 后线程应 resume"
    assert _dispatch_status(thread_dir, 2, "human") == "done"
    gd = [e for e in st.events() if e.get("type") == "gate_decision"]
    assert len(gd) == 1
    assert gd[0].get("corr") == "gate-2"
    assert gd[0].get("to") == ["pm"], "gate_decision 应回给原申请者（§10）"


def test_informal_gate_approve_idempotent(thread_dir, tmp_dir):
    """§9.1 幂等：同 corr 重复 approve 不得追加第二条 gate_decision。"""
    st, cfg = _informal_gate_setup(thread_dir, tmp_dir)
    orch.scheduler.apply_gate_decision(st, cfg, {}, corr="gate-2", approve=True)
    orch.scheduler.apply_gate_decision(st, cfg, {}, corr="gate-2", approve=True)
    gd = [e for e in st.events() if e.get("type") == "gate_decision"]
    assert len(gd) == 1


def test_informal_gate_rejects_bogus_generated_corr(thread_dir, tmp_dir):
    """生成形 corr 只查表反解（§16.10 禁猜测）：事件不存在或 to 不含 human → KeyError。"""
    st, cfg = _informal_gate_setup(thread_dir, tmp_dir)
    with pytest.raises(KeyError):
        orch.scheduler.apply_gate_decision(st, cfg, {}, corr="gate-99", approve=True)
    with pytest.raises(KeyError):   # E1 to=["pm"]，非发往 human：不构成门禁
        orch.scheduler.apply_gate_decision(st, cfg, {}, corr="gate-1", approve=True)
    assert st.get_meta("status") == "suspended", "非法 corr 不得触碰线程状态"
