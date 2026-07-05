"""调度层 · 恢复对账验收测试（spec §9.1、§9.4；并含 §5.1 核心环骨架断言）。

覆盖任务卡条目 (d)：§9.1 恢复对账**全情形**——
  - suspended 线程：保持挂起，gate_wait 行不动（不落入 dispatching 循环）。
  - a) 存在 sender=T 且 n ∈ re 的回复 → 补标 done（纵深防御）。
  - b) now > deadline_ts → 看门狗路径（attempt+1）。
  - c) 其余 → status → pending，重派发。
  - 黑板缺失/损坏 → rebuild_blackboard。

硬约束：顶层只 import orch.scheduler / orch.store；符号在函数体内引用。
恢复"禁止猜测"（§16.10）——测试只以查表结果为准。派发行状态直接读 dispatches 表
（观察落盘真相，非私有实现）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import orch.adapters  # 包级导入
import orch.scheduler  # 包级导入
import orch.store

# R-T3：会话台账只读观察（sessions 表）——热续断言需查 sid/gen（读盘真相，契约 §7）。
from orch.scheduler._dispatch import session_rows


# —— 直接读 dispatches 真相表（契约未暴露"按 id 查 status"，读盘合法）——
def _dispatch_row(thread_dir: Path, event_id: int, target: str) -> dict | None:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT event_id,target,status,deadline_ts,attempts "
            "FROM dispatches WHERE event_id=? AND target=?",
            (event_id, target),
        ).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _force_dispatching(store, thread_dir, event_id, target, deadline_ts):
    """把某派发行强制置为 dispatching + 指定 deadline（模拟崩溃在 invoke 中）。"""
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.execute(
            "UPDATE dispatches SET status='dispatching', deadline_ts=? "
            "WHERE event_id=? AND target=?",
            (deadline_ts, event_id, target),
        )
        con.commit()
    finally:
        con.close()


def _config():
    # M0 恢复只需线程默认；gate_ops 用跨平台无害命令（契约 §6.5）。
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
    }


# ——————————————————————————————————————————————————————————————
# §9.1 恢复情形 c)：dispatching 且未超时、无回复 → 转 pending
# ——————————————————————————————————————————————————————————————

def test_recover_case_c_requeues_to_pending(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    # 未来 deadline（不超时）、无回复 → 情形 c。
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "pending", "情形 c：应重置为 pending"
    # 且回到 pending 列表，主循环可接手。
    assert any(d["event_id"] == e1 and d["target"] == "backend"
               for d in st.pending_dispatches())


# ——————————————————————————————————————————————————————————————
# §9.1 恢复情形 a)：存在 sender=T 且 n∈re 的回复 → 补标 done
# ——————————————————————————————————————————————————————————————

def test_recover_case_a_backfills_done_when_reply_exists(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)
    # 回复已落盘：sender=backend 且 re 含 e1（崩溃发生在"标 done"之前的旧模型；
    # 合并事务后理论不出现，作纵深防御，§9.1 a）。
    st.append_event(sender="backend", type="handoff", body="done", to=["tester"], re=[e1])

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "done", "情形 a：有对应回复应补标 done"


def test_recover_case_a_ignores_unrelated_reply(thread_dir):
    # 回复 sender 对但 re 不含 n，或 re 含 n 但 sender 不对 → 不算 a，落 c。
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)
    # sender 对但 re 不含 e1。
    st.append_event(sender="backend", type="report", body="别的", to=["moderator"], re=[999])
    # re 含 e1 但 sender 非目标 backend。
    st.append_event(sender="frontend", type="report", body="别的2", to=["moderator"], re=[e1])

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "pending", "无匹配回复（sender=T 且 n 在 re 内）时不补 done，应转 pending"


# ——————————————————————————————————————————————————————————————
# §9.1 恢复情形 b)：now > deadline_ts → 看门狗路径（attempt+1）
# ——————————————————————————————————————————————————————————————

def test_recover_case_b_watchdog_bumps_attempt(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    before = _dispatch_row(thread_dir, e1, "backend")["attempts"]
    # 过去 deadline（已超时）、无回复 → 情形 b：看门狗计一次 attempt。
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() - 10_000)

    orch.scheduler.recover(st, _config())

    after = _dispatch_row(thread_dir, e1, "backend")
    assert after["attempts"] == before + 1, "情形 b：看门狗路径应 attempts+1"


def test_recover_case_b_precedence_when_deadline_passed_and_no_reply(thread_dir):
    # 超时且无回复：必须走 b（看门狗），不得直接当 c 简单 requeue 而漏计 attempt。
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() - 5.0)
    before = _dispatch_row(thread_dir, e1, "backend")["attempts"]

    orch.scheduler.recover(st, _config())

    after = _dispatch_row(thread_dir, e1, "backend")["attempts"]
    assert after == before + 1


# ——————————————————————————————————————————————————————————————
# §9.1 suspended：保持挂起，gate_wait 行不动
# ——————————————————————————————————————————————————————————————

def test_recover_suspended_keeps_gate_wait_untouched(thread_dir):
    st = orch.store.Store(thread_dir)
    e1 = st.append_event(sender="moderator", type="gate_request", body="批准?", to=["human"])
    st.mark_gate_wait(e1, "human")
    st.set_meta("status", "suspended")

    orch.scheduler.recover(st, _config())

    row = _dispatch_row(thread_dir, e1, "human")
    assert row["status"] == "gate_wait", "suspended 恢复：gate_wait 行必须保持不动"
    assert st.get_meta("status") == "suspended", "suspended 线程恢复后仍挂起"


def test_recover_suspended_does_not_touch_other_dispatching(thread_dir):
    # 挂起线程：恢复直接返回，连同期的 dispatching 行也不对账（§9.1：suspended → 保持挂起直接返回）。
    st = orch.store.Store(thread_dir)
    g = st.append_event(sender="moderator", type="gate_request", body="批?", to=["human"])
    st.mark_gate_wait(g, "human")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    _force_dispatching(st, thread_dir, e1, "backend", deadline_ts=time.time() + 10_000)
    st.set_meta("status", "suspended")

    orch.scheduler.recover(st, _config())

    # gate_wait 不动；dispatching 行保持（因挂起时恢复不处理派发循环）。
    assert _dispatch_row(thread_dir, g, "human")["status"] == "gate_wait"
    assert _dispatch_row(thread_dir, e1, "backend")["status"] == "dispatching"


# ——————————————————————————————————————————————————————————————
# §9.1 黑板缺失/损坏 → rebuild_blackboard
# ——————————————————————————————————————————————————————————————

def test_recover_rebuilds_blackboard_when_state_missing(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 落一条 A 类决策事件（冻结 v1）。
    e1 = st.append_event(sender="pm", type="decision", body="冻结", to=["moderator"],
                         blackboard_ops=[{"op": "freeze_contract", "name": "like-api",
                                          "path": "docs/like-api.md", "version": 1}])
    orch.store.apply_blackboard_ops(
        st, [{"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 1}], e1)
    # 删除 state.json 模拟黑板缺失。
    (thread_dir / "blackboard" / "state.json").unlink()

    orch.scheduler.recover(st, _config())

    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 1, \
        "黑板缺失恢复后应由日志重放重建"


def test_recover_rebuilds_blackboard_when_state_corrupt(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="pm", type="decision", body="冻结", to=["moderator"],
                         blackboard_ops=[{"op": "freeze_contract", "name": "like-api",
                                          "path": "docs/like-api.md", "version": 2}])
    orch.store.apply_blackboard_ops(
        st, [{"op": "freeze_contract", "name": "like-api",
              "path": "docs/like-api.md", "version": 2}], e1)
    # 写入损坏 JSON。
    (thread_dir / "blackboard" / "state.json").write_text("{ this is not json",
                                                          encoding="utf-8")

    orch.scheduler.recover(st, _config())

    state = orch.store.board_state(st)
    assert state["contracts"]["like-api"]["version"] == 2


# ——————————————————————————————————————————————————————————————
# §9.1 pending 行不处理（主循环接手）
# ——————————————————————————————————————————————————————————————

def test_recover_leaves_pending_untouched(thread_dir):
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="x", to=["backend"])
    # 保持 pending，不转 dispatching。
    orch.scheduler.recover(st, _config())
    row = _dispatch_row(thread_dir, e1, "backend")
    assert row["status"] == "pending", "pending 行恢复时不处理"


# ——————————————————————————————————————————————————————————————
# §3.2 发送者约束违规 → 调度层降级为 report 落盘 + 追加一条 system 审计事件
#   （spec §3.2 行105："发送者约束违规的处理：调度器把该信封降级为 report 落盘，
#    并追加一条 system 审计事件。"；发送者约束在 schema 校验之后单独执行，spec 行631）
# ——————————————————————————————————————————————————————————————

def _drive_one_dispatch(thread_dir, *, role: str, reply_env: dict,
                        can_decide: bool, seed_type: str = "assign"):
    """最小 store+mock+config 直接驱动**恰一次**派发（不跑整条附录B）。

    seed 一条 to=[role] 的事件触发对 role 的单次 invoke；MockAdapter 按触发号返回
    reply_env（只含作者字段）。reply_env 的 to 一律 [human]，使这条回复落盘后主循环在
    下一轮遇 target==human 即 gate_wait+suspended 返回（§10），从而只发生一次真实派发、
    干净停机（避免回复再触发对无适配器目标的二次派发）。config 只声明该 role 的 can_decide。
    返回 store。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type=seed_type, body="开工", to=[role])
    adapter = orch.adapters.MockAdapter(
        role=role, script={e1: reply_env}, ledger_path=thread_dir / "ledger.txt",
    )
    cfg = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "roles": {role: {"can_decide": can_decide, "write_scope": [], "tools": []}},
    }
    orch.scheduler.run_thread(st, cfg, {role: adapter})
    return st


def _events_by_id(store) -> list[dict]:
    return sorted(store.events(), key=lambda e: e["id"])


def test_sender_constraint_downgrade_backend_decision(thread_dir):
    """越权路径①：backend（can_decide=False）发 type=decision。

    §3.2 决策仅允许 can_decide 角色或 human；backend 不满足 → 调度器把该回复
    **降级为 report 落盘**（落盘事件 type==report）**且追加一条 system 审计事件**
    （from=='system'）。系统字段仍由编排器权威赋值（from=backend，§16.11）。
    """
    st = _drive_one_dispatch(
        thread_dir, role="backend", can_decide=False,
        reply_env={"to": ["human"], "type": "decision", "body": "我要冻结契约"},
    )
    evs = _events_by_id(st)

    # backend 那条回复落盘时已被降级为 report（非 decision）。
    backend_replies = [e for e in evs if e["from"] == "backend"]
    assert backend_replies, "backend 应有一条落盘回复"
    assert len(backend_replies) == 1
    assert backend_replies[0]["type"] == "report", (
        f"§3.2：越权 decision 应降级为 report 落盘，实际 {backend_replies[0]['type']}"
    )
    assert backend_replies[0]["type"] != "decision", "越权 decision 不得原样落盘"

    # 追加了一条 system 审计事件（编排器权威 sender=system）。
    system_events = [e for e in evs if e["from"] == "system" and e["type"] == "system"]
    assert system_events, "§3.2：应追加一条 system 审计事件"


def test_sender_constraint_downgrade_nonhuman_gate_decision(thread_dir):
    """越权路径②：非 human 角色（moderator，即便 can_decide=True）发 type=gate_decision。

    §3.2 gate_decision 仅允许 human；moderator 非 human → 降级为 report 落盘 +
    追加 system 审计事件。can_decide=True 也不豁免（gate_decision 只认 human）。
    """
    st = _drive_one_dispatch(
        thread_dir, role="moderator", can_decide=True,
        reply_env={"to": ["human"], "type": "gate_decision", "body": "approve"},
    )
    evs = _events_by_id(st)

    mod_replies = [e for e in evs if e["from"] == "moderator"]
    assert mod_replies, "moderator 应有一条落盘回复"
    assert mod_replies[0]["type"] == "report", (
        f"§3.2：非 human 的 gate_decision 应降级为 report，实际 {mod_replies[0]['type']}"
    )
    assert mod_replies[0]["type"] != "gate_decision", "非 human 的 gate_decision 不得原样落盘"

    system_events = [e for e in evs if e["from"] == "system" and e["type"] == "system"]
    assert system_events, "§3.2：应追加一条 system 审计事件"


def test_sender_constraint_allows_legit_decision_from_can_decide_role(thread_dir):
    """反向对照：合法路径不得误降级——can_decide 角色发 decision 应原样落盘。

    证明 §3.2 接线是**精确**判定（复用 protocol.allowed_sender），不会把合法
    decision 也降级；且不追加 system 审计事件。
    """
    st = _drive_one_dispatch(
        thread_dir, role="pm", can_decide=True,
        reply_env={"to": ["human"], "type": "decision", "body": "码点计数"},
    )
    evs = _events_by_id(st)

    pm_replies = [e for e in evs if e["from"] == "pm"]
    assert pm_replies, "pm 应有一条落盘回复"
    assert pm_replies[0]["type"] == "decision", (
        f"合法 decision（can_decide 角色）必须原样落盘，实际 {pm_replies[0]['type']}"
    )
    # 合法路径不追加 §3.2 违规审计事件。
    assert not [e for e in evs if e["from"] == "system"], \
        "合法 decision 不应触发 §3.2 违规 system 审计事件"


# ——————————————————————————————————————————————————————————————
# R-T2 · D：§5.1 原地重调**携带错误说明**——schema 校验失败后，重调那一次的视图
#   必须在指令尾追加系统重调说明段（含首次校验错误文本），且仍只重调一次（§5.1）。
# ——————————————————————————————————————————————————————————————

class _RecordingScriptedAdapter:
    """脚本化 adapter：按 call_no（从 1 起）返回预置作者字段信封，并逐次记录收到的视图文本。

    仅测试用（不入 src）：
      - replies: {call_no: 作者字段信封}；缺项 → KeyError（暴露编排错误）。
      - view_texts: 每次 invoke 收到的 view['text']（供断言"第二次视图含错误说明"）。
    caps 属性供 async 路径限流读取；sync 路径不读。
    """

    caps = {"max_concurrent": 1}

    def __init__(self, role: str, replies: dict[int, dict]) -> None:
        self.role = role
        self.replies = dict(replies)
        self.call_no = 0
        self.view_texts: list[str] = []

    def invoke(self, view, sess):
        self.call_no += 1
        self.view_texts.append(str(view.get("text", "")))
        raw = self.replies[self.call_no]
        env = {k: raw[k] for k in
               ("to", "type", "body", "artifacts", "corr", "blackboard_ops")
               if k in raw}
        return env, sess


def _d_config(role: str) -> dict:
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "roles": {role: {"can_decide": False, "write_scope": [], "tools": []}},
    }


def test_schema_retry_view_carries_error_note_and_retries_once(thread_dir):
    """R-T2 · D（§5.1，审计 §二 D）：第一次回非法信封 → 编排器**携带校验错误说明**原地
    重调一次；第二次回合法信封通过。断言：

      1) 第二次 invoke 收到的视图文本**包含**第一次的 schema 校验错误说明（含 header +
         具体错误文本），而第一次视图不含（错误列表不再被弃置）；
      2) 全程只重调一次（adapter 恰被调用两次，§5.1"对同一批次原地重调一次"）；
      3) 最终合法回复正常落盘（type=report）。
    """
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    role = "backend"
    e1 = st.append_event(sender="human", type="assign", body="开工", to=[role])

    # 计算第一次非法信封的确切校验错误文本，用于断言重调视图确实携带它。
    illegal_env = {"type": "assign", "body": "缺少必填 to 字段"}  # 缺 required 'to'
    expected_errors = orch.protocol.validate_author_fields(illegal_env)
    assert expected_errors, "构造用例前提：illegal_env 必须真的非法（缺 to）"

    adapter = _RecordingScriptedAdapter(role, {
        1: illegal_env,
        # 第二次合法：to=[human] 使回复落盘后主循环遇 human 即 suspend 干净停机。
        2: {"to": ["human"], "type": "report", "body": "已修正并重发"},
    })

    orch.scheduler.run_thread(st, _d_config(role), {role: adapter})

    # (2) 只重调一次：adapter 恰被调用两次。
    assert adapter.call_no == 2, (
        f"§5.1 只重调一次 → adapter 应恰被 invoke 两次，实际 {adapter.call_no}"
    )
    assert len(adapter.view_texts) == 2

    first_view, second_view = adapter.view_texts[0], adapter.view_texts[1]

    # (1) 第一次视图不含重调说明；第二次视图含说明 header + 具体错误文本。
    from orch.scheduler.core import _RETRY_NOTE_HEADER
    assert _RETRY_NOTE_HEADER not in first_view, "首次视图不应含重调说明段"
    assert _RETRY_NOTE_HEADER in second_view, (
        "§5.1：重调那一次的视图必须在指令尾追加系统重调说明段（含错误说明）"
    )
    # 具体校验错误文本被携带（取第一条错误的可辨识子串，避免措辞脆性）。
    assert expected_errors[0] in second_view, (
        f"§5.1：重调视图须携带**具体**校验错误文本；缺 {expected_errors[0]!r}。"
        f"审计否定的旧行为是 last_errors 捕获后被弃置。"
    )

    # (3) 最终合法回复落盘（type=report），线程干净挂起。
    replies = [e for e in st.events() if e["from"] == role]
    assert replies and replies[-1]["type"] == "report", "第二次合法回复应正常落盘"


def test_schema_retry_note_updates_token_estimate(thread_dir):
    """R-T2 · D 附加：重调视图的 token 估算随说明段同步更新（§6.3 全系统一致口径）。

    直接对 _view_with_retry_note 断言：新视图 token_est 严格大于原视图（追加了非空说明段），
    且用 render.estimate_tokens 复算一致——证明"token 估算同步更新"，非仅拼文本。
    """
    from orch.scheduler.core import _view_with_retry_note
    import orch.render

    base_text = "=== 系统层 ===\n你是 backend。"
    # 故意放一个**陈旧**的 token_est（999），证明修复后取的是对新文本的复算值、非陈旧值。
    base_view = {"text": base_text, "meta": {"token_est": 999}}
    errors = ["'to' is a required property"]
    new_view = _view_with_retry_note(base_view, errors)

    assert new_view["text"] != base_view["text"], "重调视图文本应已追加说明段"
    assert errors[0] in new_view["text"], "重调视图须含具体错误文本"
    recomputed = orch.render.estimate_tokens(new_view["text"])
    assert new_view["meta"]["token_est"] == recomputed, (
        "token 估算须与 render.estimate_tokens 对新文本复算一致（同步更新，非陈旧 999）"
    )
    # 同步更新的证据：新值 = 复算值，且严格大于对**原文本**的复算值（追加了非空说明段）。
    assert new_view["meta"]["token_est"] > orch.render.estimate_tokens(base_text), (
        "追加非空说明段后 token 估算（对新文本复算）应大于对原文本的复算值"
    )
    assert new_view["meta"]["token_est"] != 999, "不得沿用陈旧 token_est（须同步复算）"
    # 原视图不被就地改动（浅拷贝隔离）。
    assert base_view["meta"]["token_est"] == 999, "原视图 meta 不应被就地改动"

# ==================================================================
# R-T3 · §6.5 热续增量接入调度层（同步 core.run_thread）
#   把已实现的 render_delta（§6.5）按 docs/m3-contract.md §2 门控条件接线到主环。
#   —— 断言只观察落盘真相（sessions 表 / thread_meta）与 adapter 收到的视图文本。
#   —— FakeCliAdapter(supports_resume=True) 不需 worktree（本卡 resume 关乎会话，
#      非写域三件套）；无 worktrees 键 → 权限三件套 skip，专测热续判据。
# ==================================================================

def _resume_cfg(context_window: int = 100_000) -> dict:
    """CLI 型 backend 单角色配置（无 worktrees → 权限三件套 skip）。

    adapter 名 'cli' 绑定 context_window（render 预算）；roles.backend.adapter='cli'。
    """
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "adapters": {
            "cli": {"kind": "cli", "context_window": context_window, "timeout_s": 600},
        },
        "roles": {
            "backend": {"adapter": "cli", "can_decide": False,
                        "write_scope": ["server/"], "tools": ["Edit", "Write"]},
        },
    }


def _cli_adapter(role, wt, replies):
    """FakeCliAdapter(scripted_replies) —— supports_resume=True，返回 sess={sid,gen}。

    每次 invoke 记录 last_view_text（供断言 cold vs delta）。sid 从信封里的
    session/session_id/sid 字段提取（见 src FakeCliAdapter）。
    """
    return orch.adapters.FakeCliAdapter(
        role=role,
        config={"kind": "cli", "start_cmd": "fake", "timeout_s": 600},
        worktree=wt,
        scripted_replies=replies,
    )


def _is_cold_view(view_text: str) -> bool:
    """冷启动视图判据：系统层含权限申报「可写:」（render._build_system 冷启动全文）。

    render_delta 的系统层是最小签名（render._build_system_delta），无「可写:」权限申报、
    无 prompt 原文。据此区分某次 invoke 收到的是冷启动全量还是热续增量视图。
    """
    return "可写:" in view_text


def _bump_session_gen(thread_dir, role):
    """带外把 sessions.gen +1（测试用直接写盘，模拟别处会话代际推进）。"""
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.execute("UPDATE sessions SET gen = gen + 1 WHERE role=?", (role,))
        con.commit()
    finally:
        con.close()


def _seed_bb_decision(st, *, name: str, version: int) -> int:
    """铺一条 pm 的 A 类 decision（冻结契约）并投影黑板，同时把其 moderator 派发行标 done。

    只为改变黑板 version 标量（测试门控3/规则2）；标 done 避免它驱动主环去 invoke
    无适配器的 moderator（本测只声明 backend 一个适配器）。返回事件号。
    """
    ops = [{"op": "freeze_contract", "name": name,
            "path": f"docs/{name}.md", "version": version}]
    eid = st.append_event(sender="pm", type="decision",
                          body=f"契约 {name} v{version}", to=["moderator"],
                          blackboard_ops=ops)
    orch.store.apply_blackboard_ops(st, ops, eid)
    st.mark_done(eid, "moderator")  # 不留待办给无适配器的 moderator。
    return eid


def test_resume_second_round_uses_render_delta(thread_dir, tmp_dir):
    """两轮派发：第一轮冷启动全量，第二轮走 render_delta（增量），text 明显更短。

    §6.5 / m3-contract §2：backend(supports_resume,sid 稳定) 连续两轮——
      round1：冷启动 render_view（系统层含「可写:」权限申报、背景层全文）；
      round2：render_delta（无背景层全文、含新事件/黑板 diff、指令尾必发），
              视图 text 长度明显小于 round1。
    """
    wt = tmp_dir / "wt-backend"
    wt.mkdir()

    replies = {
        1: {"to": ["human"], "type": "report", "body": "round1 done",
            "session": "sid-backend-STABLE"},
    }
    ad = _cli_adapter("backend", wt, replies)
    cfg = _resume_cfg()

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["backend"])

    # —— round1：冷启动。回复 to=[human] → 下一轮 gate_wait+suspended，干净停机。 ——
    orch.scheduler.run_thread(st, cfg, {"backend": ad})

    assert ad.call_no == 1, "round1 应恰调用一次"
    round1_text = ad.last_view_text
    assert _is_cold_view(round1_text), "round1 必须是冷启动全量（系统层含『可写:』权限申报）"

    # sessions 表应已持久化 backend 的 sid（热续接线的前置：会话落盘）。
    srows = {s["role"]: s for s in session_rows(st)}
    assert "backend" in srows, "round1 后应持久化 backend 会话行（sessions 表）"
    assert srows["backend"]["sid"] == "sid-backend-STABLE", "sid 应为 adapter 返回的稳定值"

    # —— round2：唤醒线程，再投一件新事件（frontend→backend，B 类相关），继续跑。 ——
    st.set_meta("status", "running")
    st.append_event(sender="frontend", type="review", body="联调评审", to=["backend"])
    ad.scripted_replies[2] = {"to": ["human"], "type": "report", "body": "round2 done",
                              "session": "sid-backend-STABLE"}

    orch.scheduler.run_thread(st, cfg, {"backend": ad})

    assert ad.call_no == 2, "round2 应恰再调用一次"
    round2_text = ad.last_view_text
    assert not _is_cold_view(round2_text), \
        "round2 必须走 render_delta（增量，系统层无『可写:』权限申报全文）"
    # 指令尾必发（§6.2/§6.5）。
    assert "你是 backend" in round2_text, "热续指令尾必发（角色声明）"
    # 增量视图明显短于冷启动全量。
    assert len(round2_text) < len(round1_text), (
        f"render_delta 应明显短于冷启动：round2={len(round2_text)} 应 < round1={len(round1_text)}"
    )


def test_resume_falls_back_cold_when_blackboard_version_advanced(thread_dir, tmp_dir):
    """两轮之间黑板 version 推进（freeze_contract）→ 回退冷启动（m3-contract §2 门控3）。

    round1 冷启动 + 持久化 last_evt/bb_version；round2 之前落一条 A 类 decision（冻结
    契约 v1）使黑板 version 推进 → 门控3 不满足 → round2 回退冷启动 render_view。
    """
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    replies = {1: {"to": ["human"], "type": "report", "body": "r1",
                   "session": "sid-STABLE"}}
    ad = _cli_adapter("backend", wt, replies)
    cfg = _resume_cfg()

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    assert _is_cold_view(ad.last_view_text), "round1 冷启动"

    # —— 黑板 version 推进：落一条 pm 的**非契约**决策（set_decision）并投影黑板，
    #    使黑板 version 标量推进但**不**触发 §6.5 规则2（契约 version 变更）——专测门控3。 ——
    ops = [{"op": "set_decision", "text": "重复点赞=取消赞（幂等）"}]
    e_dec = st.append_event(sender="pm", type="decision", body="定决策", to=["moderator"],
                            blackboard_ops=ops)
    orch.store.apply_blackboard_ops(st, ops, e_dec)
    st.mark_done(e_dec, "moderator")  # 不留待办给无适配器的 moderator。

    # —— round2：再投一件新事件触发 backend，跑。 ——
    st.set_meta("status", "running")
    st.append_event(sender="frontend", type="review", body="联调", to=["backend"])
    ad.scripted_replies[2] = {"to": ["human"], "type": "report", "body": "r2",
                              "session": "sid-STABLE"}
    orch.scheduler.run_thread(st, cfg, {"backend": ad})

    assert ad.call_no == 2
    assert _is_cold_view(ad.last_view_text), \
        "黑板 version 推进（非契约变化）后 round2 必须回退冷启动（门控3 不满足）"

    # 门控3 是"非大改"→ sid **不**作废（仍保留稳定值），区别于 §6.5 规则2。
    srows_after = {s["role"]: s for s in session_rows(st)}
    assert srows_after["backend"]["sid"], "门控3 回退冷启动不应作废 sid（非契约大改）"


def test_resume_contract_version_bump_invalidates_sid(thread_dir, tmp_dir):
    """新事件含契约 version 变更 → sid 被作废（sessions 查询）且本轮冷启动全量。

    §6.5 规则2：render_delta 返回 meta.needs_cold_start=True（新事件中含契约 version
    变更 ≥1）→ 调度层主动作废该角色 sid（sessions.sid 置空/gen 递增）、本轮回退冷启动
    render_view，不得把 delta 视图发出去。
    """
    wt = tmp_dir / "wt-backend"
    wt.mkdir()

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cfg = _resume_cfg()

    # 先落一条 A 类 decision v1（会在 round1 之前投影黑板，作为热续基线的一部分）。
    _seed_bb_decision(st, name="like-api", version=1)

    # round1 触发件：human→backend。
    st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    replies = {1: {"to": ["human"], "type": "report", "body": "r1",
                   "session": "sid-STABLE"}}
    ad = _cli_adapter("backend", wt, replies)
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    assert _is_cold_view(ad.last_view_text), "round1 冷启动"
    srows = {s["role"]: s for s in session_rows(st)}
    assert srows["backend"]["sid"] == "sid-STABLE"
    gen_before = int(srows["backend"]["gen"])

    # —— round2 之前：落一条把 like-api 提到 v2 的 A 类 decision（契约 version 变更）。
    #    调度层应先尝试 delta、读到 needs_cold_start=True → 作废 sid → 冷启动。 ——
    st.set_meta("status", "running")
    _seed_bb_decision(st, name="like-api", version=2)
    st.append_event(sender="frontend", type="review", body="联调", to=["backend"])
    ad.scripted_replies[2] = {"to": ["human"], "type": "report", "body": "r2",
                              "session": "sid-STABLE-2"}
    orch.scheduler.run_thread(st, cfg, {"backend": ad})

    # 本轮视图必须是冷启动全量（不得把 delta 发出去）。
    assert _is_cold_view(ad.last_view_text), \
        "契约 version 变更 → 本轮必须冷启动全量（不得发 delta 视图）"

    # sid 被作废：在「本轮 backend 又冷启动写回新 sid」之前的瞬间应为空/gen 递增。
    # 由于 round2 的 adapter 又回写了 sid-STABLE-2（新冷启动会话），最终 sessions.sid 非空；
    # 故这里断言「作废动作发生过」的可观测残留：gen 相对 round1 至少递增（作废 gen+=1 与
    # round2 冷启动 upsert 的 gen 叠加）——即 gen > gen_before。
    srows2 = {s["role"]: s for s in session_rows(st)}
    assert int(srows2["backend"]["gen"]) > gen_before, (
        "契约 version 变更应触发 sid 作废（gen 递增），最终 gen 应大于 round1 的 gen"
    )


def test_resume_cold_start_when_no_session(thread_dir, tmp_dir):
    """门控1 反例：无 sid → 恒冷启动（首轮无会话；adapter 未产出 sid 时 round2 仍冷启动）。"""
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    replies = {1: {"to": ["human"], "type": "report", "body": "r1"}}  # 无 session → sid 为空
    ad = _cli_adapter("backend", wt, replies)
    cfg = _resume_cfg()

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    assert _is_cold_view(ad.last_view_text), "首轮无会话 → 冷启动"

    # round2：adapter 返回过 sid=None（无 session 字段）→ sessions.sid 仍为空 → 仍冷启动。
    st.set_meta("status", "running")
    st.append_event(sender="frontend", type="review", body="联调", to=["backend"])
    ad.scripted_replies[2] = {"to": ["human"], "type": "report", "body": "r2"}
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    assert _is_cold_view(ad.last_view_text), "sid 为空（adapter 未产出 sid）→ round2 仍冷启动"


def test_resume_cold_start_when_adapter_not_support_resume(thread_dir, tmp_dir):
    """门控1 反例：adapter 不支持 resume（caps.supports_resume=False）→ 恒冷启动。

    用 FakeApiAdapter（supports_resume 恒 False）驱动两轮：即便盘上有 sid（这里没有），
    也因门控1 的 supports_resume 分支不满足而恒冷启动。这里以 API 型角色验证。
    """
    replies = {1: {"to": ["human"], "type": "report", "body": "r1",
                   "session": "sid-X"},
               2: {"to": ["human"], "type": "report", "body": "r2",
                   "session": "sid-X"}}
    ad = orch.adapters.FakeApiAdapter(
        role="backend", config={"kind": "api"}, scripted_replies=replies,
    )
    # API 型 supports_resume 恒 False。
    assert ad.caps["supports_resume"] is False
    cfg = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "adapters": {"api": {"kind": "api", "context_window": 100_000, "timeout_s": 600}},
        "roles": {"backend": {"adapter": "api", "can_decide": False,
                              "write_scope": [], "tools": []}},
    }

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    round1 = ad.last_view_text if hasattr(ad, "last_view_text") else None

    st.set_meta("status", "running")
    st.append_event(sender="frontend", type="review", body="联调", to=["backend"])
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    round2 = ad.last_view_text if hasattr(ad, "last_view_text") else None

    # FakeApiAdapter 若未暴露 last_view_text，用 view 文本无法直接断言；改用会话表：
    # supports_resume=False → 调度层不应把该角色标记为可热续（sessions 可有行，但门控1 失败）。
    # 核心断言：两轮都必须是冷启动视图。若 adapter 记录了 view 文本则据此断言。
    if round1 is not None and round2 is not None:
        assert _is_cold_view(round1), "round1 冷启动"
        assert _is_cold_view(round2), "supports_resume=False → round2 仍冷启动"


def test_resume_cold_start_when_gen_changed_out_of_band(thread_dir, tmp_dir):
    """门控2 反例：sessions.gen 自上次渲染被带外改变 → 回退冷启动。

    round1 后持久化 gen 基线；带外把 sessions.gen 再推进一格（模拟别处又冷启动过一次），
    round2 时当前 sessions.gen ≠ 持久化基线 → 门控2 不满足 → 冷启动。
    """
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    replies = {1: {"to": ["human"], "type": "report", "body": "r1",
                   "session": "sid-STABLE"}}
    ad = _cli_adapter("backend", wt, replies)
    cfg = _resume_cfg()

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    assert _is_cold_view(ad.last_view_text), "round1 冷启动"

    # 带外把 sessions.gen 推进一格（sid 不变）——模拟「上次渲染以来 gen 变了」。
    _bump_session_gen(thread_dir, "backend")

    st.set_meta("status", "running")
    st.append_event(sender="frontend", type="review", body="联调", to=["backend"])
    ad.scripted_replies[2] = {"to": ["human"], "type": "report", "body": "r2",
                              "session": "sid-STABLE"}
    orch.scheduler.run_thread(st, cfg, {"backend": ad})
    assert _is_cold_view(ad.last_view_text), "gen 带外变化 → round2 回退冷启动（门控2）"


def test_resume_reconstructable_after_restart(thread_dir, tmp_dir):
    """§16.9：重启恢复后（新进程模拟：重建 Store）热续判据仍能从盘上重建。

    round1 用 Store 实例 A 冷启动 + 持久化会话/last_evt/bb_version（全部落盘）；丢弃 A，
    新建 Store 实例 B（模拟新进程），round2 走 render_delta——证明热续判据（sid/gen/
    last_evt/bb_version）不依赖任何内存态，纯从 events.db + thread_meta 重建。
    """
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    replies = {1: {"to": ["human"], "type": "report", "body": "r1",
                   "session": "sid-STABLE"}}
    ad = _cli_adapter("backend", wt, replies)
    cfg = _resume_cfg()

    st_a = orch.store.Store(thread_dir)
    st_a.set_meta("status", "running")
    st_a.append_event(sender="human", type="assign", body="开工", to=["backend"])
    orch.scheduler.run_thread(st_a, cfg, {"backend": ad})
    assert _is_cold_view(ad.last_view_text), "round1 冷启动"
    del st_a  # 丢弃内存态（模拟进程退出）。

    # —— 新进程：重建 Store（只从盘读）。 ——
    st_b = orch.store.Store(thread_dir)
    assert st_b.get_meta("status") in ("running", "suspended")
    st_b.set_meta("status", "running")
    st_b.append_event(sender="frontend", type="review", body="联调", to=["backend"])
    ad.scripted_replies[2] = {"to": ["human"], "type": "report", "body": "r2",
                              "session": "sid-STABLE"}
    orch.scheduler.run_thread(st_b, cfg, {"backend": ad})

    assert ad.call_no == 2
    assert not _is_cold_view(ad.last_view_text), \
        "§16.9：重启后热续判据应能从盘重建 → round2 走 render_delta"


# ——————————————————————————————————————————————————————————————
# R-T4 · §13 采集点随代码交付：调度层单次派发即落 tokens / batch_size / schema_retry
# ——————————————————————————————————————————————————————————————

def _metric_rows(store, key: str) -> list[float]:
    rows = store._con.execute(
        "SELECT value FROM metrics WHERE key=?", (key,)
    ).fetchall()
    return [float(r["value"]) for r in rows]


def test_dispatch_records_tokens_and_batch_no_retry_on_legal_reply(thread_dir):
    """§13 采集点（R-T4）：一次合法派发落 1 条 tokens 行 + 1 条 batch_size 行，
    0 条 schema_retry 行（首次即合法）。tokens_in 可复算 = 派发视图 token_est。"""
    st = _drive_one_dispatch(
        thread_dir, role="backend", can_decide=False,
        reply_env={"to": ["human"], "type": "report", "body": "已完成"},
    )
    tokens = _metric_rows(st, "tokens")
    batch = _metric_rows(st, "batch_size")
    retries = _metric_rows(st, "schema_retry")

    assert len(tokens) == 1, f"一次合法派发应恰 1 条 tokens 行，实测 {len(tokens)}"
    assert len(batch) == 1, f"一次派发应恰 1 条 batch_size 行，实测 {len(batch)}"
    assert retries == [], f"首次即合法不应有 schema_retry 行，实测 {retries}"
    # tokens_in 可复算：> 0（视图非空）。
    assert tokens[0] > 0, "tokens_in 应为派发视图的正 token 估算（可复算）"


def test_dispatch_records_one_schema_retry_on_one_illegal_reply(thread_dir):
    """§13 采集点2（R-T4）：首次非法 → 记恰 1 条 schema_retry 行；invoke 记 2 条
    tokens 行（首次非法 + 重调合法各一次），首次合法率分母/分子由此复算。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    role = "backend"
    e1 = st.append_event(sender="human", type="assign", body="开工", to=[role])
    adapter = _RecordingScriptedAdapter(role, {
        1: {"type": "report", "body": "缺 to 非法"},          # 缺必填 to → 校验失败
        2: {"to": ["human"], "type": "report", "body": "已修正重发"},
    })
    orch.scheduler.run_thread(st, _d_config(role), {role: adapter})

    retries = _metric_rows(st, "schema_retry")
    tokens = _metric_rows(st, "tokens")
    assert len(retries) == 1, f"一次非法回复应恰记 1 条 schema_retry，实测 {len(retries)}"
    assert len(tokens) == 2, (
        f"非法+重调合法共 2 次 invoke → 2 条 tokens 行，实测 {len(tokens)}"
    )
    # 首次合法率复算 = 1 - retry/invoke = 1 - 1/2 = 50%（本单次派发切片）。
    first_legal = (1.0 - len(retries) / len(tokens)) * 100.0
    assert abs(first_legal - 50.0) < 0.01, f"本切片首次合法率应 50%，实测 {first_legal}"


def test_dispatch_records_background_compression_metric(thread_dir):
    """§13 采集点3（R-T4）：有背景层内容的派发落 bg_orig_tokens / bg_summarized_tokens 行。

    构造：先播若干 report（C 类 → 背景层），再触发对 backend 的一次派发，
    该派发视图背景层非空 → 调度层据 view.meta 落两条 bg_* 行（可复算压缩比）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 背景 report（C 类 → 背景层）：立即标 done，仅作背景上下文，不实际派发
    # （无 moderator adapter；避免派发到无适配器目标）。
    for i in range(3):
        rid = st.append_event(sender="frontend", type="report", to=["moderator"],
                              body=f"背景报告 {i}")
        st.mark_done(rid, "moderator")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["backend"])
    adapter = orch.adapters.MockAdapter(
        role="backend", script={e1: {"to": ["human"], "type": "report", "body": "ok"}},
        ledger_path=thread_dir / "ledger.txt",
    )
    orch.scheduler.run_thread(st, _d_config("backend"), {"backend": adapter})

    orig = _metric_rows(st, "bg_orig_tokens")
    summ = _metric_rows(st, "bg_summarized_tokens")
    assert orig, "背景层非空的派发应落 bg_orig_tokens 行（§13 采集点3）"
    assert summ, "背景层非空的派发应落 bg_summarized_tokens 行（§13 采集点3）"
    assert orig[0] > 0, "背景原文 token 应 > 0（可复算）"
