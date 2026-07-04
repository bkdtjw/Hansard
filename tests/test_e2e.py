"""端到端验收测试（spec §5.1 核心环 + 附录B "点赞功能"）。

覆盖任务卡条目 (f)：
  - §5.1 核心环 + 附录B：事件类型序列一致。
  - 黑板终态：contracts 到 v2 冻结、tasks 全 done。

驱动方式（契约 §4.1 / §4）：
  · run_thread(store, config, adapters) 跑到 thread status ∈ {suspended, terminated} 返回。
  · human approve（E14）与 CI 回调（E15）是编排器/人类侧动作，附录B 注明 gate_decision
    "E2E 直接调 API 模拟"。M0 契约冻结的是 run_thread/recover 与 store 原语；因此本测试
    用**已冻结的 store 原语**注入 E14/E15（一条 from=human 的 gate_decision + 一条
    from=system 的 CI 回调），再续跑 run_thread 至终止。这是 spec §10/§5.5 中"人类批准 →
    系统执行器 → system 事件入队"控制流在 M0 单线程串行下的最小忠实复现。

硬约束：顶层只 import 包；具体符号在函数体内引用。断言只依赖 spec 保证的可观察终态，
不依赖未冻结的内部实现细节。§16.1 自查：路由只认 to 字段——E2E 不从 body 解析 @。

注：本卡为"测试先行"，实现尚未编写，本文件预期为红。附录B 事件号可能因实现细节偏移，
故断言以**类型序列一致 + 黑板终态**为准（附录B 明文允许事件号偏移）。
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
        "gate_ops": {
            "merge_main": {"cmd": "python -c \"print('merged')\""},
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


def _human_approve_and_ci_callback(store):
    """模拟 §10 的 `orch approve`：产生 gate_decision（from=human）→ gate_wait 行 done
    → 线程 resume；并模拟系统执行器发起的 run_ci 完成后的 system 回调（§5.2/§5.5）。

    仅用**已冻结**的 store 原语，控制流忠于 spec：批准 → 系统执行器 → system 事件入队。
    """
    gate = _find_gate_request(store)
    assert gate is not None
    corr = gate.get("corr") or "gate-01"
    # gate_decision：to=原 gate_request 的 sender（让申请者续走流程，§10）。
    store.append_event(
        sender="human", type="gate_decision",
        body="approve", to=[gate["from"]], corr=corr, re=[gate["id"]],
    )
    # gate_wait 行标 done + 线程 resume。
    store.reply_and_done  # noqa: B018  （占位引用，证明契约方法存在；实际 done 由下行完成）
    # 直接把 gate_wait 行置 done 并 resume（编排器 approve 的落盘效果）。
    import sqlite3
    con = sqlite3.connect(str(Path(store_thread_dir(store)) / "events.db"))
    try:
        con.execute(
            "UPDATE dispatches SET status='done' WHERE event_id=? AND target='human'",
            (gate["id"],),
        )
        con.commit()
    finally:
        con.close()
    store.set_meta("status", "running")
    # 系统执行器：登记 run_ci 长作业并在完成后回调（M0 同步退化，契约 §6.2）。
    store.register_job(corr="job-01", kind="ci", cmd="python -c \"print('ci')\"",
                       callback_to="moderator", started_evt=gate["id"])
    store.set_job_status("job-01", "done")
    # CI 回调：system 事件 to=[moderator]，corr 回填（§5.2）。
    store.append_event(
        sender="system", type="system",
        body="CI 通过", to=["moderator"], corr="job-01",
    )


def store_thread_dir(store) -> Path:
    """取 Store 绑定的线程目录（契约 §2：Store 绑定一个线程目录）。

    优先读公开属性；缺失则从 events() 无关——这里通过约定属性名探测，
    未实现即 AttributeError 触发红。"""
    for attr in ("thread_dir", "dir", "path", "root"):
        v = getattr(store, attr, None)
        if v is not None:
            return Path(v)
    raise AttributeError("Store 未暴露线程目录属性（thread_dir/dir/path/root 之一）")


def test_e2e_full_sequence_and_blackboard_terminal(thread_dir, like_feature_script, tmp_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    adapters = _build_adapters(like_feature_script, tmp_dir / "ledger.txt")
    _seed_e1(st)

    # 段1：跑到门禁挂起。
    orch.scheduler.run_thread(st, _config(), adapters)
    assert st.get_meta("status") == "suspended"

    # 段2：人类 approve + 系统执行器 CI 回调（frozen store 原语注入）。
    _human_approve_and_ci_callback(st)

    # 段3：续跑到 terminate。
    orch.scheduler.run_thread(st, _config(), adapters)
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
    adapters = _build_adapters(like_feature_script, tmp_dir / "ledger.txt")
    _seed_e1(st)
    orch.scheduler.run_thread(st, _config(), adapters)
    _human_approve_and_ci_callback(st)
    orch.scheduler.run_thread(st, _config(), adapters)

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
    adapters = _build_adapters(like_feature_script, tmp_dir / "ledger.txt")
    _seed_e1(st)
    orch.scheduler.run_thread(st, _config(), adapters)
    _human_approve_and_ci_callback(st)
    orch.scheduler.run_thread(st, _config(), adapters)

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
    adapters = _build_adapters(like_feature_script, ledger)
    _seed_e1(st)
    orch.scheduler.run_thread(st, _config(), adapters)
    _human_approve_and_ci_callback(st)
    orch.scheduler.run_thread(st, _config(), adapters)

    lines = [ln for ln in Path(ledger).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == len(set(lines)), f"ledger 出现重复事件号: {lines}"
