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
