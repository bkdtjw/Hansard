"""§5.1 核心循环（单线程串行版）+ §8.3 验证钩子 + §5.4 终止清单。

控制流严格对齐 spec §5.1 伪代码与契约 §4 run_thread：
  每轮 pending 按 event_id 升序 → group_by(target) 聚合（同目标同批一次 invoke,
  re=全部 event_ids）→ 逐组串行（M0 不做写域并行，§5.1 并行是 M3）。

系统字段权威赋值（§3.1、§16.11）：from=target、re=event_ids、id/ts 由 store 落盘时赋，
一律不信 mock 输出的同名字段。
看门狗（§5.3/§16.2）：deadline_ts 为落盘绝对时间戳，禁止内存倒计时/sleep。
mock 角色无 worktree → 跳过 autocommit 与越权审计（§4.5，契约 §6.3）。
"""

from __future__ import annotations

import subprocess
import time
from itertools import groupby

import orch.protocol
import orch.store

from orch.scheduler.systemexec import (
    append_system_event,
    run_privileged_and_callbacks,
)

# 单次调用超时预算（秒）。M0 mock 同步返回，deadline 只用于崩溃后看门狗对账（§9.1 b）。
# spec §5.3 的"单次调用超时"级别在 M0 恢复算法中生效；主动触发是 M1（契约 §6.4）。
_DEFAULT_TIMEOUT_S = 600.0

# §5.1：schema 校验失败原地重调上限（两次仍败 → failed + 转 moderator）。
_MAX_SCHEMA_RETRY = 1


def _role_conf(config: dict, role: str) -> dict:
    return (config.get("roles") or {}).get(role, {}) or {}


def _timeout_for(config: dict, role: str) -> float:
    conf = _role_conf(config, role)
    caps = conf.get("caps") or {}
    t = caps.get("timeout_s")
    if isinstance(t, (int, float)) and t > 0:
        return float(t)
    return _DEFAULT_TIMEOUT_S


def _group_pending(pending: list[dict]) -> list[tuple[str, list[int]]]:
    """按 target 聚合 pending 派发行（§5.1：同目标同批 → 一次 invoke）。

    输入已按 event_id 升序（store.pending_dispatches 保证）。为聚合稳定，按 target
    分组并保留各组内 event_id 升序；组间按各组最小 event_id 升序（§5.1"组内最小 event_id
    串行"的确定性顺序）。返回 [(target, [event_ids...]), ...]。
    """
    by_target: dict[str, list[int]] = {}
    for row in pending:
        by_target.setdefault(row["target"], []).append(int(row["event_id"]))
    # 组间顺序：最小 event_id 升序，稳定确定。
    ordered = sorted(by_target.items(), key=lambda kv: min(kv[1]))
    return [(tgt, sorted(ids)) for tgt, ids in ordered]


def _assemble_view(store, role: str, event_ids: list[int]) -> dict:
    """M0 最小占位 view（契约 §3）：{role, event_ids, events, board}。

    四层结构/第三人称渲染/尾部重锚定是 M1（契约 §6.1）；mock 只用 role+event_ids。
    """
    board = ""
    board_path = store.thread_dir / "blackboard" / "board.md"
    try:
        if board_path.exists():
            board = board_path.read_text(encoding="utf-8")
    except OSError:
        board = ""
    return {
        "role": role,
        "event_ids": list(event_ids),
        "events": store.events(),
        "board": board,
    }


def _run_verify(config: dict, role: str) -> dict | None:
    """§8.3 验证钩子：回复为 acceptance 时编排器亲自执行 role.verify.cmd。

    返回 {'exit_code': int, 'output': str}；未配置 verify 时返回 None。
    cwd 占位（{worktree:role}/{target_repo}）在 M2 落地；M0 fixture 用无害命令 + cwd='.'。
    """
    verify = _role_conf(config, role).get("verify")
    if not verify:
        return None
    cmd = verify.get("cmd")
    if not cmd:
        return None
    cwd = verify.get("cwd") or "."
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True,
            timeout=float(verify.get("timeout_s", 120)),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"exit_code": int(proc.returncode), "output": out[:2000]}
    except (OSError, subprocess.SubprocessError) as exc:
        # 执行失败视为非 0（acceptance 不生效，§8.3）。
        return {"exit_code": 1, "output": f"verify 执行异常: {exc!r}"[:2000]}


def _finalize_envelope(store, config: dict, role: str, env: dict) -> dict:
    """把作者字段信封定稿为可落盘信封：赋系统字段 + §8.3 verify 钩子（含降级）。

    - 系统字段（§16.11）：from=role（不信 mock 自称）；re 由调用方在 reply 里赋 event_ids。
    - §8.3：type==acceptance → 执行 verify，写 meta.verify；exit_code!=0 → 降级为 report。
    """
    out = dict(env)
    out["from"] = role  # 权威赋值，覆盖 mock 任何自称（§3.1/§16.11）。
    meta = dict(out.get("meta") or {})

    if out.get("type") == "acceptance":
        vres = _run_verify(config, role)
        if vres is not None:
            meta["verify"] = vres
            if vres.get("exit_code") != 0:
                # §8.3：exit_code==0 是 acceptance 生效必要条件；否则降级 report。
                out["type"] = "report"
    out["meta"] = meta
    return out


def _enforce_sender_constraint(config: dict, reply: dict) -> str | None:
    """§3.2 发送者约束：越权则**就地把回复 type 改为 report**，返回被降级的原 type。

    判定精确复用 protocol.allowed_sender（§3.2：decision→can_decide 角色或 human；
    gate_decision→仅 human；system→仅编排器；terminate→moderator/tester/human；
    其余 type→任意）。发送者约束在 schema 校验之后单独执行（spec 行631）。

    sender=reply['from']（已由 _finalize_envelope 权威赋为 role，§16.11）；
    can_decide 取自 config 中该 role 的申报（§11.1）。允许则返回 None（不改 type）。
    降级后的 report 落盘 + system 审计事件由调用方按 §3.2 完成（参照 §3.3 bb_ops 越权处理）。
    """
    role = reply["from"]
    orig_type = reply.get("type")
    can_decide = bool(_role_conf(config, role).get("can_decide", False))
    if orch.protocol.allowed_sender(orig_type, role, can_decide=can_decide):
        return None
    reply["type"] = "report"  # §3.2：降级为 report 落盘。
    return orig_type


def _apply_bb_if_eligible(store, config: dict, reply: dict, reply_id: int) -> None:
    """§3.3 门槛判定后应用 blackboard_ops；不满足则忽略并追加 system 审计事件。

    门槛（§3.3）：final type ∈ {decision, acceptance, gate_decision} 且 from 角色 can_decide。
    降级发生在 _finalize_envelope 内，故此处按**定稿后**的 type 判定（acceptance 若被降级为
    report 则 bb_ops 不再适用）。
    """
    ops = reply.get("blackboard_ops")
    if not ops:
        return
    role = reply["from"]
    can_decide = bool(_role_conf(config, role).get("can_decide", False))
    if orch.protocol.can_apply_blackboard_ops(
        reply.get("type"), sender_can_decide=can_decide
    ):
        orch.store.apply_blackboard_ops(store, ops, reply_id)
    else:
        # §3.3：忽略 ops + 追加 system 审计事件（契约 §1 违规处理语义）。
        append_system_event(
            store,
            body=f"忽略越权/不合格 blackboard_ops：role={role} type={reply.get('type')}",
            to=["moderator"],
        )


def _handle_terminate(store, config: dict, term_event: dict) -> None:
    """§5.4 终止清单：汇总产物 → system 总结事件 → status=terminated → 拒绝新派发。

    terminate 信封落盘时不生成派发行（store.append_event 已保证，§5.4）；本函数在其后触发。
    汇总产物：黑板契约 + 全部 artifacts + 会话台账（M0 mock 无分支，分支列表退化为空）。
    """
    state = orch.store.board_state(store)
    contracts = state.get("contracts") or {}
    tasks = state.get("tasks") or {}
    artifacts: list[str] = []
    for ev in store.events():
        for a in ev.get("artifacts") or []:
            if a not in artifacts:
                artifacts.append(a)
    summary = (
        "线程终止清单：\n"
        f"- 冻结契约：{ {k: v.get('version') for k, v in contracts.items()} }\n"
        f"- 任务状态：{tasks}\n"
        f"- 产物：{artifacts}\n"
        f"- 触发终止事件：E{term_event.get('id')}"
    )
    # system 总结事件（§5.4）。to=[] → 兜底 moderator 落派发行；但线程即将 terminated，
    # 主循环随后不再派发（拒绝新派发）。为不制造悬挂待办，总结事件直接指向 moderator
    # 并在下一步终止后由"拒绝新派发"吞掉（terminated 线程 run_thread 不消费 pending）。
    append_system_event(store, body=summary, to=["moderator"])
    store.set_meta("status", "terminated")


def run_thread(
    store: "orch.store.Store",
    config: dict,
    adapters: dict,
) -> None:
    """§5.1 核心循环单线程串行版。跑到 thread status ∈ {suspended, terminated} 返回。"""
    while True:
        status = store.get_meta("status")
        if status in ("suspended", "terminated"):
            return

        pending = store.pending_dispatches()
        if not pending:
            # 无待办：M0 单线程无外部事件源，直接返回（§5.1 的"等待"在 M0 退化为返回）。
            return

        groups = _group_pending(pending)

        for target, event_ids in groups:
            # target==human → gate_wait + 挂起（§10）。
            if target == "human":
                for eid in event_ids:
                    store.mark_gate_wait(eid, target)
                store.set_meta("status", "suspended")
                return  # §10：整体停机，挂起不消耗资源。

            if not _dispatch_group(store, config, adapters, target, event_ids):
                # 组内失败已落盘（failed + system 转 moderator）；继续下一组。
                continue

            # 组处理后若线程状态改变（terminate/suspend），立即回到外层判定。
            st = store.get_meta("status")
            if st in ("suspended", "terminated"):
                return

        # 一轮 groups 处理完，回到 while 顶部重取 pending（新回复已入队）。


def _dispatch_group(
    store,
    config: dict,
    adapters: dict,
    target: str,
    event_ids: list[int],
) -> bool:
    """处理单个 (target, event_ids) 组。成功返回 True，失败（两次 schema 败）返回 False。

    落盘顺序严格对齐 §5.1：mark_dispatching(+deadline) → invoke → schema 校验（原地重调一次）
    → reply_and_done（系统字段 from/re） → apply bb_ops → verify 钩子已并入定稿 → 终止检查。
    """
    adapter = adapters[target]

    # 标 dispatching + 落盘绝对截止时间戳（§4.4 事务(2)、§16.2）。
    deadline_ts = time.time() + _timeout_for(config, target)
    for eid in event_ids:
        store.mark_dispatching(eid, target, deadline_ts)

    view = _assemble_view(store, target, event_ids)

    # invoke + schema 校验（失败原地重调一次；两次败 → failed + 转 moderator，§5.1）。
    attempt = 0
    env: dict | None = None
    sess = None
    last_errors: list[str] = []
    while attempt <= _MAX_SCHEMA_RETRY:
        raw_env, sess = adapter.invoke(view, sess)
        # 审计原文（§14 一等公民）。
        store.write_invoke_log(
            event_ids=event_ids, role=target,
            view_text=str(view), output_text=str(raw_env),
        )
        errors = orch.protocol.validate_author_fields(raw_env)
        if not errors:
            env = raw_env
            break
        last_errors = errors
        attempt += 1

    if env is None:
        # 两次仍非法 → failed + system 事件转 moderator（§5.1）。
        for eid in event_ids:
            store.mark_failed(eid, target)
        append_system_event(
            store,
            body=f"角色 {target} 对 E{event_ids} 的回复两次 schema 校验失败：{last_errors}",
            to=["moderator"],
        )
        return False

    # 定稿信封：系统字段 from + §8.3 verify 钩子（含 acceptance 降级）。
    reply = _finalize_envelope(store, config, target, env)
    # 权威赋值 re = 本批全部 event_ids（§3.1/§16.11）。
    reply["re"] = list(event_ids)

    # §3.2 发送者约束：越权则**就地降级为 report** 后再落盘（spec 行105/行631；schema 校验之后
    # 单独执行）。降级在 reply_and_done 之前完成 → report 落盘、且 _apply_bb_if_eligible 按定稿
    # type（report）自然判定 bb_ops 不适用。审计事件在回复落盘后追加（同 §3.3 bb_ops 越权处理）。
    downgraded_from = _enforce_sender_constraint(config, reply)

    # 记录聚合埋点 batch_size（§13）。
    store.record_metric("batch_size", float(len(event_ids)), extra=target)

    # [事务(5)] 回复落盘 + 标 done（对本组每一行都标 done）。
    # reply_and_done 只标一行 done；组内其余行单独标 done（同批聚合，一次回复覆盖全组）。
    reply_id = store.reply_and_done(
        done_event_id=event_ids[0], done_target=target, reply=reply, session=None
    )
    for eid in event_ids[1:]:
        store.mark_done(eid, target)

    # §3.2：越权已降级 report 落盘 → 追加一条 system 审计事件（编排器权威 from=system，§16.11）。
    if downgraded_from is not None:
        append_system_event(
            store,
            body=(f"发送者约束违规降级为 report：role={target} "
                  f"越权 type={downgraded_from}（§3.2）"),
            to=["moderator"],
        )

    # 应用 blackboard_ops（§3.3 门槛，定稿 type 判定）。
    _apply_bb_if_eligible(store, config, reply, reply_id)

    # 终止检查（§5.4）：定稿 type==terminate → 触发终止清单。
    if reply.get("type") == "terminate":
        term_ev = None
        for ev in store.events():
            if ev["id"] == reply_id:
                term_ev = ev
                break
        _handle_terminate(store, config, term_ev or {"id": reply_id})

    return True
