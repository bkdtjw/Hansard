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
    """
    gate = _find_gate_request(store, corr)
    if gate is None:
        raise KeyError(f"未找到 corr={corr} 的 gate_request 事件")

    requester = gate.get("from")  # 原申请者（gate_request 的 sender）。

    # ① gate_decision 事件：from=sender（human），corr 回填，to=[申请者]（§10）。
    store.append_event(
        sender=sender, type="gate_decision",
        body="approve" if approve else "reject",
        to=[requester], corr=corr, re=[gate["id"]],
    )

    # ② gate_wait 行标 done + resume（§10）。gate_request 的 target 是 human。
    store.mark_done(gate["id"], "human")
    store.set_meta("status", "running")

    # ③ approve 且关联特权操作 → 系统执行器（§5.5）。reject 不执行。
    if approve:
        run_privileged_and_callbacks(store, config, gate, callback_to=requester)
