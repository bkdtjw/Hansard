"""系统执行器与门禁裁决入口（spec §5.5、§10、§8；契约 §4+§8）。

- append_system_event：编排器权威追加 system 事件（§3.1：sender='system'）。
- apply_gate_decision：§10 `orch approve|reject` 的编排器入口（契约 §8 冻结签名）。
- run_privileged_and_callbacks：approve 关联特权操作时的系统执行器（§5.5）。

铁律：
  §16.12 任何 agent 禁止直接执行 merge/部署；只能经门禁 + 系统执行器（本模块）。
  §16.11 system 事件的 from/re/id/ts 由编排器赋值，不信模型。
  凭据只存在于编排器环境（config），不进任何 agent 环境（§5.5）。
"""

from __future__ import annotations

import re
import subprocess

import orch.store


def append_system_event(
    store,
    *,
    body: str,
    to: list[str] | None = None,
    corr: str | None = None,
) -> int:
    """编排器权威追加一条 system 事件（sender='system'，§3.1/§3.2/§16.11）。

    返回事件 id。to 由调用方给定（回调/审计目标）；to 为空由 store 兜底 moderator（§5.2）。
    """
    return store.append_event(
        sender="system", type="system", body=body,
        to=list(to or []), corr=corr,
    )


def _find_gate_request(store, corr: str) -> dict | None:
    """按 corr 定位最近的 gate_request 事件（§10：corr 关联门禁）。"""
    match = None
    for ev in store.events():
        if ev.get("type") == "gate_request" and ev.get("corr") == corr:
            match = ev  # 取最后一个（同 corr 理论唯一）。
    return match


def _run_gate_op(op_conf: dict) -> dict:
    """执行单个 gate_op 命令模板（§5.5 系统执行器）。

    op_conf 形态（契约 §11.1/§6.5）：{'cmd': str, 'cwd'?: str, 'async'?: bool}。
    返回 {'exit_code': int, 'output': str, 'cmd': str}。
    """
    cmd = op_conf.get("cmd", "")
    cwd = op_conf.get("cwd") or "."
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=600,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"exit_code": int(proc.returncode), "output": out[:2000], "cmd": cmd}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"exit_code": 1, "output": f"gate_op 执行异常: {exc!r}"[:2000], "cmd": cmd}


def run_privileged_and_callbacks(store, config: dict, gate: dict, callback_to: str) -> None:
    """approve 后的系统执行器（§5.5）：执行 config['gate_ops'] 中的特权操作。

    M0 忠实复现控制流（契约 §6.5：用无害命令验证，不真 merge）：
      · 同步类特权操作（如 merge_main）→ 执行 → 结果作为 system 事件入队（to=callback_to）。
      · 长作业类（async: True，如 run_ci）→ 经 jobs 登记（M0 同步退化，契约 §6.2）→ 执行
        → 完成后回调 system 事件 to=[callback_to]、corr 回填。
    gate_ops 为空时（如 test_scheduler 的 _config）本函数为 no-op。
    """
    gate_ops = config.get("gate_ops") or {}
    corr = gate.get("corr")
    started_evt = gate.get("id")

    for op_name, op_conf in gate_ops.items():
        if not isinstance(op_conf, dict):
            # 简单字符串命令模板（如 "git -C ... merge ..."）。
            op_conf = {"cmd": str(op_conf)}
        is_async = bool(op_conf.get("async"))
        if is_async:
            # 长作业：先登记 jobs（§5.2），M0 同步执行后回调。
            job_corr = f"job-{op_name}"
            store.register_job(
                corr=job_corr, kind=op_name, cmd=op_conf.get("cmd", ""),
                callback_to=callback_to, started_evt=started_evt,
            )
            res = _run_gate_op(op_conf)
            store.set_job_status(job_corr, "done" if res["exit_code"] == 0 else "failed")
            append_system_event(
                store,
                body=f"CI 回调({op_name}) exit={res['exit_code']}: {res['output']}",
                to=[callback_to], corr=job_corr,
            )
        else:
            res = _run_gate_op(op_conf)
            append_system_event(
                store,
                body=f"系统执行器({op_name}) exit={res['exit_code']}: {res['output']}",
                to=[callback_to], corr=corr,
            )


# §10 corr 缺省生成形：`gate-{事件号}`（编排器生成，事件号即门禁挂起信封的 id）。
_GENERATED_CORR_RE = re.compile(r"^gate-(\d+)$")


def _find_informal_gate(store, corr: str) -> dict | None:
    """§10 corr 缺省生成条款：按生成形 corr 反解"非正式门禁"信封。

    spec §10 的挂起机制覆盖**一切** target=human 的 pending 行（§5.1 同判据），
    不限 type=gate_request；信封自身无 corr 时由编排器生成 `gate-{事件号}`。
    本函数只查表反解（§16.10 禁猜测）：corr 匹配生成形、事件存在、且其 to 含
    human 才构成门禁；任一不满足返回 None（调用方按"未找到"抛错）。
    —— 真实联跑发现的实现缺口：calc 线程 moderator 以 handoff→human 收尾，
    线程挂起后 approve/reject 因找不到 gate_request 而 KeyError，线程不可恢复。
    """
    m = _GENERATED_CORR_RE.match(str(corr))
    if not m:
        return None
    eid = int(m.group(1))
    for ev in store.events():
        if ev.get("id") == eid:
            return ev if "human" in (ev.get("to") or []) else None
    return None


def _find_gate_decision(store, corr: str) -> dict | None:
    """按 corr 定位已存在的 gate_decision 事件（§9.1 恢复算法所需：只查表，禁猜测）。"""
    match = None
    for ev in store.events():
        if ev.get("type") == "gate_decision" and ev.get("corr") == corr:
            match = ev  # 取最后一个（同 corr 理论唯一）。
    return match


def _ci_callback_exists(store, gate_corr: str) -> bool:
    """§9.1：run_privileged_and_callbacks 的产物是否已落盘（防重复 CI 回调）。

    run_privileged_and_callbacks 会为每个 gate_op 追加 system 事件；对 run_ci 类
    会用 corr=`job-{op_name}` 关联 jobs 表 + system 回调事件。恢复时只要看到
    jobs 表里有非空行即视作"已跑过一次" —— jobs 表恰好是 §5.2 冻结的落盘真相，
    §9.1 允许"只查表"。
    """
    # 用 jobs 表判定（§5.2 落盘真相）：任一已登记的作业 = privileged callbacks 已启动过。
    con = store._con  # 已在 store 内的连接（读旁路合法：§7）
    row = con.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    if row is None:
        return False
    return int(row[0]) > 0


def apply_gate_decision(
    store: "orch.store.Store",
    config: dict,
    adapters: dict,
    *,
    corr: str,
    approve: bool,
    sender: str = "human",
) -> None:
    """§10 `orch approve|reject` 编排器入口（契约 §8 冻结签名）。

    ① 产生 gate_decision 事件（from=sender, corr 回填, to=[原 gate_request.sender]）；
    ② 对应 gate_wait 派发行标 done → thread status='running'（resume）；
    ③ approve 且 gate 关联特权操作 → 系统执行器按 config['gate_ops'] 执行，结果作为 system
       事件入队；run_ci 类经 jobs 登记（M0 同步退化）后回调 system 事件 to=[callback_to]、
       corr 回填（§5.2/§5.5）。reject：只入 gate_decision 并 resume，不执行特权操作。

    §9.1 R-a 幂等修复：apply_gate_decision 的四步（append_event / mark_done / set_meta /
    privileged callbacks）不是单事务；若在 append_event 与 mark_done 之间崩溃，落盘会留下
    一个 orphan gate_decision + 未 done 的 gate_wait + 未 running 的状态。恢复驱动再进入
    本函数就会**重复**追加同 corr 的 gate_decision，进而让 moderator 收到成对/成三份的
    pending 派发行、view.event_ids 里出现脚本外的最大触发号（KeyError）。
    修复：进入前**只查表**（§16.10 禁止猜测）看同 corr 是否已有 gate_decision；已有则复用
    该事件，只补做后续未完成步骤——严格按 §9.1"只查表数日志"的恢复语义。
    """
    gate = _find_gate_request(store, corr)
    if gate is None:
        # §10 corr 缺省生成：非 gate_request 信封发往 human 同样构成门禁，
        # corr 为编排器生成形 gate-{事件号}（测试：test_e2e informal gate 三连）。
        gate = _find_informal_gate(store, corr)
    if gate is None:
        raise KeyError(f"未找到 corr={corr} 的门禁信封（gate_request 或 to=human 挂起信封）")

    requester = gate.get("from")  # 原申请者（门禁挂起信封的 sender）。

    # §9.1 幂等：先查同 corr 是否已有 gate_decision（前一次崩溃前已 append）。
    existing = _find_gate_decision(store, corr)
    if existing is None:
        # ① gate_decision 事件：from=sender（human），corr 回填，to=[申请者]（§10）。
        store.append_event(
            sender=sender, type="gate_decision",
            body="approve" if approve else "reject",
            to=[requester], corr=corr, re=[gate["id"]],
        )
    # 若 existing 非空：复用之前那条，不再重复追加（§9.1 R-a 幂等）。
    # existing["type"]/["from"]/["corr"] 已落盘；本函数唯一"再修一次"的只有下游派发/状态。

    # ② gate_wait 行标 done + resume（§10）。gate_request 的 target 是 human。
    # 若之前已被标 done，mark_done 幂等（SET status='done' 是无副作用重复）。
    store.mark_done(gate["id"], "human")
    store.set_meta("status", "running")

    # ③ approve 且关联特权操作 → 系统执行器（§5.5）。reject 不执行。
    # §9.1 幂等：若 jobs 表已有登记（前次崩溃前 privileged callbacks 已启动），跳过重跑。
    if approve and not _ci_callback_exists(store, corr):
        run_privileged_and_callbacks(store, config, gate, callback_to=requester)
