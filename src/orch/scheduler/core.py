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

import logging
import subprocess
import time
from itertools import groupby

import orch.protocol
import orch.render
import orch.store

from orch.scheduler._dispatch import session_rows
from orch.scheduler.availability import (
    TRANSPORT_FAILURE_ERRORS,
    AdapterAvailability,
    AdapterUnavailableError,
    adapter_instance,
    make_availability,
    note_blocked,
    note_fallback_switch,
    on_invoke_success,
    on_transport_failure,
    on_unavailable,
    primary_adapter_name,
    rebind_session_if_needed,
    resolve_binding,
)
from orch.scheduler.permissions import (
    audit_write_scope,
    autocommit,
    head_sha,
    reset_hard,
)
from orch.scheduler.systemexec import (
    append_system_event,
    run_privileged_and_callbacks,
)
from orch.scheduler.watchdog import check_watchdogs

# 审视快赢①（docs/usability-review-20260706.md P1）：核心环过程日志。
# 库层只发 logging 事件（零 print、零签名改动）；CLI 在 cmd_run 挂 stderr handler。
_RUN_LOG = logging.getLogger("orch.run")

# 单次调用超时预算（秒）。M0 mock 同步返回，deadline 只用于崩溃后看门狗对账（§9.1 b）。
# spec §5.3 的"单次调用超时"级别在 M0 恢复算法中生效；主动触发是 M1（契约 §6.4）。
_DEFAULT_TIMEOUT_S = 600.0

# §5.1：schema 校验失败原地重调上限（两次仍败 → failed + 转 moderator）。
_MAX_SCHEMA_RETRY = 1

# R-T2 · D：schema 校验失败原地重调时，视图指令尾追加的"系统重调说明段"标题（§5.1
# "携带错误说明对同一批次原地重调一次"）。放在视图文本最末（指令尾之后），保持 §6 五段
# 结构不破坏——它是编排器在重调这一次追加的系统侧提示，非新的一层。
_RETRY_NOTE_HEADER = "=== 系统重调说明（schema 校验失败，请修正后重发本次信封）==="


def _view_with_retry_note(view: dict, errors: list[str]) -> dict:
    """R-T2 · D：为"原地重调那一次"生成携带具体校验错误说明的视图（§5.1）。

    在原视图 text 尾部追加一段系统重调说明（含第一次的 schema 校验错误文本），并同步更新
    meta.token_est（§6.3 token 估算方法全系统一致，复用 render.estimate_tokens）。**不改**
    event_ids（同一批次原地重调，mock 仍按触发号查表 → 仍只重调一次，§5.1）。返回浅拷贝，
    不改动原 view（首次 invoke 用的是原 view，重调用带说明的新 view）。
    """
    new_view = dict(view)
    base_text = str(view.get("text", ""))
    err_lines = "\n".join(f"- {e}" for e in (errors or [])) or "- （无具体错误文本）"
    note = (
        f"{_RETRY_NOTE_HEADER}\n"
        f"上一次回复未通过信封 schema 校验，错误如下：\n{err_lines}\n"
        f"请据此修正，仅重发本次信封（字段 to / type / body / artifacts / corr / "
        f"blackboard_ops），其余系统字段由编排器赋值。"
    )
    new_text = f"{base_text}\n\n{note}" if base_text else note
    new_view["text"] = new_text
    # token 估算同步更新（§6.3 全系统一致口径）。
    meta = dict(view.get("meta") or {})
    meta["token_est"] = orch.render.estimate_tokens(new_text)
    meta["retry_note"] = True  # 观测点：本视图为携带错误说明的重调视图。
    new_view["meta"] = meta
    return new_view


# ——————————————————————————————————————————————————————————————
# R-T4 · §13 指标采集点（采集点随代码一起交付，禁止事后补测不可复算的数字）
#
# 三个采集点埋在 core / async_core 的**同一决策函数**里（Lead §17 裁决落实）：
#   1) 每次 invoke 记一条 `tokens` 行（tokens_in=派发视图 meta.token_est；
#      tokens_out=对回复 body 跑 estimate_tokens）。invoke 计数 = `tokens` 行数，
#      是 §13 首次合法率的分母（"总调用数"）。
#   2) 每次 schema 校验失败记一条 `schema_retry` 行（§13 首次合法率分子="退回次数"）。
#   3) 费用：**仅当** adapter 显式暴露真实用量/计费（属性 last_usage）时记 `cost` 行；
#      Mock/Fake 无 last_usage → 不记（orch metrics 成本字段显示 N/A，禁止编造 cost=0）。
# 这些 record_metric 都走 store 既有公开原语（不改 metrics DDL §4.3）。
# ——————————————————————————————————————————————————————————————

def _record_invoke_tokens(store, target: str, view_text_in: str, raw_env) -> None:
    """§13 采集点1：每次 invoke 记一条 tokens 行（可复算，随代码交付）。

    tokens_in = 本次实际送出的视图文本 estimate_tokens（= view.meta.token_est 同口径，
    §6.3 全系统一致）；tokens_out = 对回复 body 文本跑 estimate_tokens（非法回复无合法
    body → 0）。value 存 tokens_in；extra 记 'role:in=..:out=..' 供审计与复算对照。
    """
    tokens_in = orch.render.estimate_tokens(view_text_in or "")
    body = ""
    if isinstance(raw_env, dict):
        b = raw_env.get("body")
        if isinstance(b, str):
            body = b
    tokens_out = orch.render.estimate_tokens(body)
    store.record_metric(
        "tokens", float(tokens_in),
        extra=f"{target}:in={tokens_in}:out={tokens_out}",
    )


def _record_render_compression(store, target: str, view: dict) -> None:
    """§13 采集点3：渲染时记 背景层 原文/摘要 token（背景压缩比）。

    从 render_view 产出的 view.meta 读 bg_orig_tokens / bg_summarized_tokens（render 模块
    不持 store，故落盘在调度层，保持现分层 §2）。仅当**原文非 0**（确有背景层内容）时记录
    —— 无背景层内容压缩比无意义（分母为 0），不落 bg_* 行；orch metrics 无行显示 N/A。
    热续 render_delta 无背景层（不含该 meta 键）→ 自然跳过（get 兜 0）。压缩比由 orch
    metrics 对全部 bg_* 行求均值（Σ summarized ÷ Σ orig）。
    """
    if not isinstance(view, dict):
        return
    meta = view.get("meta") or {}
    orig = meta.get("bg_orig_tokens")
    summ = meta.get("bg_summarized_tokens")
    if not isinstance(orig, (int, float)) or orig <= 0:
        return
    summ_val = float(summ) if isinstance(summ, (int, float)) else 0.0
    store.record_metric("bg_orig_tokens", float(orig), extra=target)
    store.record_metric("bg_summarized_tokens", summ_val, extra=target)


def _record_invoke_cost(store, target: str, adapter) -> None:
    """§13 采集点1（费用侧）：仅当 adapter 暴露真实用量 last_usage 时记 cost 行。

    协议（Lead §17 裁决）：adapter 有 `last_usage` 属性且其含数值 'cost'（或可换算的
    真实计费字段）→ 记一条 cost 行；否则**不记**（Mock/Fake 无 → orch metrics 成本 N/A）。
    禁止编造 cost=0：无真实计费信息就不落任何 cost 行。真实后端 Q1/Q2 陪跑接入后自然充值。
    """
    usage = getattr(adapter, "last_usage", None)
    if not isinstance(usage, dict):
        return
    cost = usage.get("cost")
    if not isinstance(cost, (int, float)):
        return
    store.record_metric("cost", float(cost), extra=str(target))


def _role_conf(config: dict, role: str) -> dict:
    return (config.get("roles") or {}).get(role, {}) or {}


def _role_worktree(config: dict, role: str):
    """M2：从 config['worktrees'][role] 读该角色的 worktree 路径（M2 契约 §3）。

    M0/M1 mock 语境不设 worktrees → 返回 None → 三件套整体 skip（保 127 绿）。
    """
    from pathlib import Path
    wts = (config or {}).get("worktrees") or {}
    p = wts.get(role)
    if not p:
        return None
    return Path(p)


def _write_scope(config: dict, role: str) -> list[str]:
    """§8.2 审计所需的写域申报（M2 契约 §5，roles[role].write_scope）。"""
    return list(_role_conf(config, role).get("write_scope") or [])


def _last_ok_commit(store, config: dict, role: str) -> str | None:
    """§8.2 审计所需的对齐点：上个合法 commit（生产回路，C-4 修复）。

    读取顺序：
      1) store.get_meta(f"last_ok_commit:{role}") —— 前一轮 autocommit 成功后回写的 sha
         （见 _dispatch_group 中的 `store.set_meta`；避免生产路径依赖外部补丁 config[…]）；
      2) config['last_ok_commit'] —— 兜底，供测试/首轮无 store 记录时使用。

    未提供则返回 None → 审计跳过（无对齐点无法执行 diff）；autocommit 仍会执行。
    """
    if store is not None:
        try:
            v = store.get_meta(f"last_ok_commit:{role}")
        except Exception:
            v = None
        if v:
            return str(v)
    v = (config or {}).get("last_ok_commit")
    return str(v) if v else None


def _ensure_audit_baseline(store, config: dict, role: str) -> None:
    """R-T2 · E（§8.2 首轮审计兜底）：确保该角色审计恒有对齐点，消除首轮 fail-open。

    仅当该角色**有 worktree**（CLI 型）且 store/config **均无** last_ok_commit 时生效：
    在本轮 invoke 之前，取该 worktree 当前 HEAD sha（只查 git，§16.10 不猜测）落盘为
    thread_meta 键 last_ok_commit:{role}。此后 _dispatch_group 的审计分支 `if last_ok_commit:`
    恒为真、恒执行 → 首轮越权写入也被 diff 拦截（审计报告指出此前会漏网的场景）。

    无 worktree（mock/API 型）→ no-op（保持 M0/M1 mock 绿）。HEAD 取不到（异常仓库）→
    不落盘（审计仍 skip，但不引入错误对齐点）。已有 last_ok_commit → no-op（不覆盖既有对齐点）。
    """
    worktree_path = _role_worktree(config, role)
    if worktree_path is None:
        return
    if _last_ok_commit(store, config, role):
        return
    sha = head_sha(worktree_path)
    if sha:
        store.set_meta(f"last_ok_commit:{role}", sha)


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


def _is_cold_start(store, role: str) -> bool:
    """§6.2/契约 §4：该 role 是否冷启动（sessions.gen==0 或无 sid，即尚未热续）。

    读 sessions 台账；无该角色行 → 冷启动。M1 mock 不 upsert 会话 → 恒冷启动
    （render 恒走冷启动全量，热续增量 §6.5 是 M3）。
    """
    for s in session_rows(store):
        if s.get("role") == role:
            return int(s.get("gen") or 0) == 0 or not s.get("sid")
    return True


def _assemble_view(store, config: dict, role: str, event_ids: list[int]) -> dict:
    """M1：调用 render.render_view 组装完整不对称四层视图（契约 §4 / §6）。

    替换 M0 最小占位 view：五段（system/blackboard/background/focus/instruction）完整渲染、
    第三人称焦点窗、预算裁剪。mock 仍按 view['event_ids'] 查表（不依赖 text），但落 invoke
    log 时用 view['text'] 完整渲染原文（§14）。cold_start 依 sessions 台账推导（契约 §4）。
    视图组装属调度层职责、与厂商无关（§2）。
    """
    return orch.render.render_view(
        store, config,
        role=role,
        event_ids=list(event_ids),
        cold_start=_is_cold_start(store, role),
    )


# ——————————————————————————————————————————————————————————————
# R-T3 · §6.5 热续增量接入调度层（docs/m3-contract.md §2 门控条件逐句落实）
# ——————————————————————————————————————————————————————————————
#
# 已实现但从未被调度层调用的 render_delta（§6.5）在此接线到同步/异步两条核心环。
# core.py 与 async_core.py **复用同一决策函数**（_render_for_dispatch），不写两份逻辑。
#
# 门控条件（m3-contract §2；全部满足才走 render_delta，否则冷启动 render_view）：
#   (1) 该角色 adapter 声明 supports_resume 且 sessions 表中该角色 sid 非空；
#   (2) sessions.gen 自上次渲染未变（持久化的 resume_gen == 当前 sessions.gen）；
#   (3) 黑板 version 未大改（<1 step，即自上次渲染以来黑板 version 未推进）。
#
# 持久化（§16.9 禁内存态；键名自定但稳定可从盘重建）：每次成功渲染派发后把该角色的
# last_evt（本批最大事件号）、当时黑板 version、当时 sessions.gen 落到 thread_meta。
#
# §6.5 规则2：render_delta 返回 meta.needs_cold_start=True（新事件含契约 version 变更
# ≥1）→ 主动作废该角色 sid（sessions.sid 置空 + gen 递增，走 store 既有 reply_and_done
# 的 session upsert 通道）→ 本轮回退冷启动 render_view（不得把 delta 视图发出去）。

# thread_meta 键前缀（§16.9 稳定键名）：
_RESUME_LAST_EVT_KEY = "resume_last_evt:{role}"      # 上次渲染的本批最大事件号
_RESUME_BB_VERSION_KEY = "resume_bb_version:{role}"  # 上次渲染时的黑板 version 标量
_RESUME_GEN_KEY = "resume_gen:{role}"                # 上次渲染时的 sessions.gen


def _blackboard_version(store) -> int:
    """黑板 version 标量（§6.5 门控3 / 规则2 的"版本推进"判据，调度层自定，§17 归档）。

    = Σ(各冻结契约 version) + 决策条数。契约只增版本、决策只追加 → 单调不减；任一
    材料级黑板变化（新契约/契约升版/新决策）都令其 ≥1 步推进，正好对应 m3-contract §2
    门控3"黑板 version 未推进（<1 step）"与 §6.5 规则2"契约版本变更"的宏观信号。

    只读 board_state（§4.6 从盘 state.json 重建），不持有任何内存态（§16.9）。
    """
    state = orch.store.board_state(store)
    contracts = state.get("contracts") or {}
    total = 0
    for c in contracts.values():
        v = (c or {}).get("version")
        try:
            total += int(v)
        except (TypeError, ValueError):
            total += 1  # 非数值 version：计 1（保守，仍单调）。
    total += len(state.get("decisions") or [])
    return total


def _adapter_name(config: dict, role: str) -> str:
    """该角色绑定的 adapter 名（config.roles[role].adapter）；缺省 role 名兜底（会话 backend 列）。"""
    return str(_role_conf(config, role).get("adapter") or role)


def _session_row(store, role: str) -> dict | None:
    """读该角色的 sessions 行（读盘观察落盘真相，§9.1/§16.9）；无则 None。"""
    for s in session_rows(store):
        if s.get("role") == role:
            return s
    return None


def _resume_session_ok(store, config: dict, role: str, adapter) -> dict | None:
    """m3-contract §2 门控 (1)(2)：会话侧热续前置——adapter 支持 resume + sid 非空 + gen 未变。

    满足则返回该角色 sessions 行（供 render_delta 取 last_evt / 传 sess）；否则 None。
    门控(3)（黑板 version）**不在此判**：它与 §6.5 规则2（契约 version 变更→作废 sid）有
    先后语义——规则2 需先跑 render_delta 读 needs_cold_start，故 (3) 的判定挪到
    _render_for_dispatch 内、在 render_delta 之后（见该函数）。全部只读盘（§16.9）。
    """
    caps = getattr(adapter, "caps", None) or {}
    if not caps.get("supports_resume"):
        return None  # 门控(1)前半：adapter 不支持 resume。
    srow = _session_row(store, role)
    if srow is None or not srow.get("sid"):
        return None  # 门控(1)后半：sid 非空。
    # 门控(2)：sessions.gen 自上次渲染未变。
    persisted_gen = store.get_meta(_RESUME_GEN_KEY.format(role=role))
    if not persisted_gen or int(persisted_gen) != int(srow.get("gen") or 0):
        return None
    # last_evt 必须已持久化（否则无从算增量）。
    if store.get_meta(_RESUME_LAST_EVT_KEY.format(role=role)) is None:
        return None
    return srow


def _blackboard_version_advanced(store, role: str) -> bool:
    """m3-contract §2 门控(3)：黑板 version 是否自上次渲染推进（≥1 step）。

    比较持久化基线 resume_bb_version 与当前黑板 version 标量。基线缺失（从未渲染过）视为
    「已推进」→ 不满足门控(3)。此判据只覆盖**非契约**的黑板变化（新决策/任务）——契约
    version 变更由 §6.5 规则2（render_delta.needs_cold_start）单独处理并作废 sid。
    """
    persisted_bb = store.get_meta(_RESUME_BB_VERSION_KEY.format(role=role))
    if persisted_bb is None:
        return True
    return int(persisted_bb) != _blackboard_version(store)


def _invalidate_sid(store, config: dict, role: str, srow: dict) -> None:
    """§6.5 规则2：作废该角色 sid（sessions.sid 置空 + gen 递增）——纯会话簿记，不经事件日志。

    sessions 表是**工作状态**而非事件真相（§4.2 事件日志=发生过什么）：sid 作废、代际递增
    属会话簿记，不该借合成 type=system 事件承载（§3.2 对 type=system 的语义枚举是看门狗/
    回调/审计，不含"会话作废"，且合成事件会永久污染事件日志）。故走 store 的 upsert_session
    直写 sessions 表（单事务，语义与 reply_and_done 内会话 upsert 完全一致、复用同一内部实现）。

    作废后同时清掉该角色 resume_* 持久化键，确保后续轮不再误判可热续。
    """
    old_gen = int(srow.get("gen") or 0)
    # sid 置空 = 作废；gen 在原值上 +1（§6.5 规则2）。backend/last_evt 由 upsert_session
    # 保留既有行，无需在此重传（§4.3 DDL 列冻结）。
    store.upsert_session(role=role, sid=None, gen=old_gen + 1)
    # 清掉该角色的热续持久化基线（作废后重新冷启动会写新基线）。
    role_gen_key = _RESUME_GEN_KEY.format(role=role)
    store.set_meta(role_gen_key, "")   # 空串 = 无有效基线（_resume_eligible 视为不满足）。


def _session_for_upsert(store, config: dict, role: str, event_ids: list[int],
                        sess: dict | None, backend: str | None = None) -> dict | None:
    """把 adapter 返回的会话（{sid,gen}）规范化为 reply_and_done 的 session upsert 入参。

    §7.5 sessions 列：role / backend / sid / last_evt / gen。sess 为 None（mock 无会话）→
    返回 None（不 upsert，与接线前逐字一致）。backend 缺省取角色绑定的 adapter 名（config）；
    M5 §5.6.2 降级派发时由调用方显式传入**生效绑定名**（否则会话表会被写回主绑定，
    与"effective ≠ sessions.backend 视为会话死亡"的判据自相矛盾）。
    last_evt = 本批最大事件号（§6.5 下轮增量起点）。

    gen 单调不减（spec §7.2"gen += 1"语义：会话代际计数只增）：取 adapter 返回 gen 与盘上
    既有 gen 的较大者。这保证 §6.5 规则2 作废 sid 时 gen 递增（_invalidate_sid 把 gen 顶到
    old+1）后，同轮回退冷启动的会话 upsert 不会把 gen 回退——作废痕迹（gen 递增）得以留存。
    """
    if sess is None:
        return None
    prev = _session_row(store, role) or {}
    adapter_gen = int(sess.get("gen", 0) or 0)
    prev_gen = int(prev.get("gen") or 0)
    return {
        "role": role,
        "backend": backend if backend else _adapter_name(config, role),
        "sid": sess.get("sid"),
        "last_evt": int(max(event_ids)),
        "gen": max(adapter_gen, prev_gen),
    }


def _persist_resume_state(store, config: dict, role: str, event_ids: list[int],
                          sess: dict | None) -> None:
    """成功渲染派发后持久化热续判据（§16.9 全部落盘，键名稳定可从盘重建）。

    - last_evt = 本批最大事件号（下轮 render_delta 的增量起点）；
    - bb_version = 当前黑板 version 标量（下轮门控3 比对基线）；
    - resume_gen = 当前 sessions.gen（下轮门控2 比对基线）——从 sess 或 sessions 行取。

    sess 为 adapter 返回的会话（{sid, gen}）；会话本身的 upsert 已在 reply_and_done 内完成
    （见 _dispatch_group 调 reply_and_done 时传入 session）。此处只落"渲染判据基线"。
    """
    store.set_meta(_RESUME_LAST_EVT_KEY.format(role=role), str(max(event_ids)))
    store.set_meta(_RESUME_BB_VERSION_KEY.format(role=role), str(_blackboard_version(store)))
    srow = _session_row(store, role)
    gen_val = int((srow or {}).get("gen") or 0)
    store.set_meta(_RESUME_GEN_KEY.format(role=role), str(gen_val))


def _render_for_dispatch(store, config: dict, role: str, event_ids: list[int],
                         adapter) -> tuple[dict, dict | None]:
    """R-T3 热续决策（同步/异步核心环复用同一函数）：决定本轮走 render_delta 还是 render_view。

    返回 (view, resume_sess)：
      - view：本轮要送 adapter.invoke 的视图（冷启动全量或热续增量）。
      - resume_sess：热续时传给 adapter.invoke 的既有会话（{sid,gen}，供真实 CLI resume_cmd）；
        冷启动为 None（start_cmd 全量）。

    决策顺序（m3-contract §2 + §6.5 规则2 的先后语义）：
      A) 门控(1)(2) 会话侧前置（supports_resume + sid 非空 + gen 未变）不满足 → 冷启动。
      B) 满足 (1)(2) → 先 render_delta，**据其 meta.needs_cold_start 判 §6.5 规则2**：
         needs_cold_start=True（新事件含契约 version 变更 ≥1，"需求被推翻级别的大改"）→
         主动作废 sid（_invalidate_sid）+ 回退冷启动 render_view（不得发 delta 视图）。
      C) needs_cold_start=False，再判门控(3)：黑板 version 若因**非契约**变化（新决策/任务）
         推进（≥1 step）→ 回退冷启动（但**不作废 sid**，因非"大改"级别）；未推进 → 采用
         delta 视图热续。

    为何 (3) 挪到 render_delta 之后：契约 version 变更同时会推进黑板 version 标量，若把 (3)
    放在 render_delta 之前，契约变更会被 (3) 先行拦成"普通冷启动"、永不作废 sid，规则2 落空。
    先跑 render_delta 读 needs_cold_start，能把"契约大改（作废 sid）"与"非契约小改（保 sid
    仅本轮冷启动）"精确分流——这正是 §6.5"小改增量、大改弃会话"的分野。

    不满足门控 → 直接 render_view（冷启动）。mock/Fake 不支持 resume 时只走 (A) 冷启动分支，
    与接线前行为逐字一致（既有 219+ 测试与混沌 MockAdapter 路径零扰动）。
    """
    srow = _resume_session_ok(store, config, role, adapter)
    if srow is None:
        return _assemble_view(store, config, role, event_ids), None

    last_evt = int(store.get_meta(_RESUME_LAST_EVT_KEY.format(role=role)) or 0)
    delta = orch.render.render_delta(
        store, config, role=role, event_ids=list(event_ids),
        last_evt=last_evt,
    )
    if delta.get("meta", {}).get("needs_cold_start"):
        # §6.5 规则2：契约 version 变更（大改）→ 作废 sid，本轮回退冷启动全量。
        _invalidate_sid(store, config, role, srow)
        return _assemble_view(store, config, role, event_ids), None
    # 门控(3)：黑板 version 因非契约变化推进 → 回退冷启动（保 sid，不作废）。
    if _blackboard_version_advanced(store, role):
        return _assemble_view(store, config, role, event_ids), None
    # 热续：把既有会话传给 invoke（真实 CLI 据此 resume_cmd(sid)）。
    resume_sess = {"sid": srow.get("sid"), "gen": int(srow.get("gen") or 0)}
    return delta, resume_sess


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


def _collect_branches(store, config: dict) -> list[str]:
    """§5.4 终止清单"分支列表"段（M1 mock 退化为空）。

    真实 CLI（M2）每角色一个 worktree/分支（../wt-*, m{n}-*），此处从 config.roles 的
    worktree/branch 申报汇总。M1 mock 角色无 worktree → 返回空列表；但终止清单仍列"分支"
    段目（体现四项俱全，§5.4）。此函数保留分支、对 mock no-op（无凭空构造分支名）。
    """
    branches: list[str] = []
    roles = (config.get("roles") or {}) if config else {}
    for _role, rc in roles.items():
        if not isinstance(rc, dict):
            continue
        br = rc.get("branch")
        if br and br not in branches:
            branches.append(br)
    return branches


def _find_terminate_summary(store, term_event_id: int) -> dict | None:
    """按 re=[term_event_id] + sender=system 定位已落盘的终止总结事件（§9.1 只查表）。

    终止总结事件用 re=[触发终止事件号] 标识其血缘（见 _handle_terminate）。恢复/重入时
    据此判断"总结事件是否已落盘"，避免重复追加（幂等）。同一 term 理论唯一，取最后一条。
    """
    match = None
    for ev in store.events():
        if (ev.get("from") == "system"
                and ev.get("type") == "system"
                and term_event_id in (ev.get("re") or [])):
            match = ev
    return match


def _handle_terminate(store, config: dict, term_event: dict) -> None:
    """§5.4 终止清单：汇总产物 → system 总结事件 → status=terminated → 拒绝新派发。

    terminate 信封落盘时不生成派发行（store.append_event 已保证，§5.4）；本函数在其后触发。
    汇总产物四项（§5.4）：黑板契约 + 全部 artifacts + 分支列表 + 会话台账（mock 无分支/无
    会话时退化为空列表，但四段段目俱全）。

    评审建议②（契约 §3）：终止**总结 system 事件不生成 pending 派发行**——本函数落盘该
    system 事件后立即把其派发行标 done（"建后即 done"），保持派发表整洁；不残留任何指向
    总结事件的 pending 行。

    R-T2 · H（§5.4 忠实语义修复，审计 §二 H）：spec §5.4 只说终止"此后**拒绝新派发**"，
    并未说要作废终止**前**既有的 pending 待办。旧实现把终止前所有 pending 一并 mark_done
    （静默作废既有待办）超出字面语义（审计否定）。本函数据此改为：**只处理终止清算自身**——
    落盘/复用总结事件、把总结事件**其自身**的派发行标 done、置 status=terminated；**不触碰**
    终止前既有的其它 pending 派发行。terminated 线程凭 run_thread 顶部/组间的 status 判定
    拒绝新派发（既有行为）；恢复算法（§9.1）对 terminated 线程不处理其 pending 的既有行为
    亦保持不变。

    R-T1 崩溃安全 + 幂等（§4.4 间隙(1)/§9.1）：本函数的三步（append 总结 / mark_done /
    set status=terminated）不是单事务。若在 append 总结事件（其 append_event_post 钩子）
    与后续两步之间崩溃，盘上会留下：总结事件已落盘、其 moderator 派发行仍 pending、status
    仍 running。恢复主循环会把该 pending 行当普通派发去 invoke moderator → 脚本无该事件号
    → KeyError。修复：
      1) 总结事件带 re=[term_event.id] 标识血缘，可被 _find_terminate_summary 只查表定位；
      2) 本函数进入先查是否已存在总结事件——已存在则**复用**（不重复 append），只补做
         其自身派发行 mark_done + set status（幂等重入）。
    这是通用规则（任何在此边界崩溃的轮都靠"查表复用总结事件"闭合），非对特定 seed 特判。
    幂等重入只重标总结事件自身的派发行 done，仍不触碰终止前既有 pending（H 语义）。
    """
    term_id = int(term_event.get("id")) if term_event.get("id") is not None else None

    # §9.1 幂等：先查同一 term 的总结事件是否已落盘（前次崩溃前已 append）。
    existing = _find_terminate_summary(store, term_id) if term_id is not None else None

    if existing is None:
        state = orch.store.board_state(store)
        contracts = state.get("contracts") or {}
        tasks = state.get("tasks") or {}

        # 全部 artifacts（去重、保序，§5.4）。
        artifacts: list[str] = []
        for ev in store.events():
            for a in ev.get("artifacts") or []:
                if a not in artifacts:
                    artifacts.append(a)

        # 分支列表（§5.4；mock 退化为空）。
        branches = _collect_branches(store, config)

        # 会话台账（§5.4；读 sessions 表，mock 常为空）。
        sessions = session_rows(store)
        session_lines = [
            f"{s.get('role')}@{s.get('backend')}"
            f"(sid={s.get('sid')}, gen={s.get('gen')}, last_evt={s.get('last_evt')})"
            for s in sessions
        ]

        summary = (
            "线程终止清单：\n"
            f"- 冻结契约：{ {k: v.get('version') for k, v in contracts.items()} }\n"
            f"- 任务状态：{tasks}\n"
            f"- 产物 artifacts：{artifacts}\n"
            f"- 分支列表：{branches}\n"
            f"- 会话台账：{session_lines}\n"
            f"- 触发终止事件：E{term_id}"
        )

        # system 总结事件（§5.4）。to=[moderator] 落一行 pending 派发（§4.4(1)）；
        # 系统字段编排器权威赋值（sender='system'，§16.11）。带 re=[term_id] 标识血缘，
        # 供恢复/重入只查表定位（§9.1）。
        summary_id = store.append_event(
            sender="system", type="system", body=summary,
            to=["moderator"], re=[term_id] if term_id is not None else [],
        )
    else:
        summary_id = int(existing["id"])

    # 总结事件不留待办：其**自身**派发行立即标 done（"建后即 done"，契约 §3；幂等重复
    # SET 无副作用）。总结事件 to=[moderator] → 其派发行 (summary_id, 'moderator')。
    store.mark_done(summary_id, "moderator")

    # R-T2 · H（§5.4 忠实语义）：终止**只**清算自身（总结事件其自身派发行 done + 状态置
    # terminated），**不触碰**终止前既有的其它 pending 派发行（旧实现一并 mark_done 是审计
    # 否定的"静默作废既有待办"，超出 spec"此后拒绝新派发"字面语义）。terminated 线程凭
    # run_thread 的 status 判定拒绝消费任何 pending（既有行为）；§9.1 恢复对 terminated
    # 线程亦不处理其 pending。故此处不再遍历清扫其余 pending。
    store.set_meta("status", "terminated")


def _finish_interrupted_terminate(store, config: dict) -> None:
    """§9.1 恢复补完：若盘上有 terminate 事件但线程未 terminated，幂等重入终止清算。

    只查表数日志（§16.10 禁猜测）：
      - 无 terminate 事件 → no-op（正常运行中的线程）；
      - status 已 terminated → no-op（已闭合）；
      - status == suspended → 不动（挂起线程等 gate_decision，§9.1；terminate 与 suspend
        互斥，理论不共存，防御性跳过）；
      - 否则（有 terminate 且未 terminated 且非挂起）→ 对最后一条 terminate 事件调用幂等
        _handle_terminate 补完（复用已存在的总结事件、清扫 pending、置 terminated）。
    """
    status = store.get_meta("status")
    if status in ("terminated", "suspended"):
        return
    term_ev = None
    for ev in store.events():
        if ev.get("type") == "terminate":
            term_ev = ev  # 取最后一条（理论唯一）。
    if term_ev is None:
        return
    _handle_terminate(store, config, term_ev)


def run_thread(
    store: "orch.store.Store",
    config: dict,
    adapters: dict,
) -> None:
    """§5.1 核心循环单线程串行版。跑到 thread status ∈ {suspended, terminated} 返回。"""
    # R-T1 崩溃恢复：进入前先补完可能被 kill 打断的终止清算（§9.1/§5.4）。
    # 若盘上已有 terminate 事件但 status != terminated（在 _handle_terminate 三步中途被
    # kill），主循环若直接跑会把总结事件的残留 pending 当普通派发去 invoke → 脚本无该事件号
    # → KeyError。此处只查表（§16.10）：发现未闭合的 terminate 即幂等重入 _handle_terminate
    # 补完（复用已存在的总结事件、清扫 pending、置 terminated），再进主循环。
    _finish_interrupted_terminate(store, config)

    # M5 §5.6：可用性视图（config['adapter_state_path'] 缺失 → None = 降级路由整体不启用，
    # 两条环走与 M0–M4 逐字相同的老路径）。此处只造不读，首读在每轮的 reload()。
    availability = make_availability(config)

    while True:
        status = store.get_meta("status")
        if status in ("suspended", "terminated"):
            return

        # §5.3 看门狗三级：核心环**每轮**主动调用（契约 §2/§4）。触发 level2/level3 会产生
        # gate_request(to=[human]) 并复用 M0 门禁机制置 suspended；level1 计 attempt。
        # 触发挂起后由下一步状态判定接手返回（§10）。时间取样一次注入，禁内存倒计时（§16.2）。
        check_watchdogs(store, config)
        if store.get_meta("status") in ("suspended", "terminated"):
            return

        pending = store.pending_dispatches()
        if not pending:
            # 无待办：M0 单线程无外部事件源，直接返回（§5.1 的"等待"在 M0 退化为返回）。
            return

        groups = _group_pending(pending)

        # §5.6.1：**每轮调度前**重读适配器状态文件（禁止只在启动时读一次）——外部写者
        # （CLI / 控制台）在两轮之间的 enable/disable 必须立刻对本轮解析生效。
        if availability is not None:
            availability.reload()

        # 本轮是否有任何组真正进入派发（§5.6.2 阻塞态判定用）。
        dispatched_any = False

        for target, event_ids in groups:
            # target==human → gate_wait + 挂起（§10）。
            if target == "human":
                for eid in event_ids:
                    store.mark_gate_wait(eid, target)
                store.set_meta("status", "suspended")
                _RUN_LOG.info(
                    "%s E%s → human 挂起等待人工裁决（orch approve gate-{事件号} 可恢复）",
                    store.thread_dir.name, event_ids,
                )
                return  # §10：整体停机，挂起不消耗资源。

            # §5.6.2 生效绑定解析：**在标 dispatching 之前**现算（聚合与并行判定不变）。
            effective: str | None = None
            if availability is not None:
                effective = resolve_binding(config, target, availability)
                if effective is None:
                    # 全部不可用 → 该组保持 pending、不 invoke、不消耗 attempts；
                    # 首次进入阻塞态追加一条通告事件；其余角色照常调度，线程不挂起。
                    note_blocked(store, target, primary_adapter_name(config, target))
                    _RUN_LOG.info(
                        "%s E%s → %s 无可用适配器，保持 pending 等待人工 enable（§5.6.2）",
                        store.thread_dir.name, event_ids, target,
                    )
                    continue

            dispatched_any = True
            if not _dispatch_group(store, config, adapters, target, event_ids,
                                   availability=availability, effective=effective):
                # 组内失败已落盘（failed + system 转 moderator，或跳闸后回 pending）；
                # 继续下一组。
                continue

            # 组处理后若线程状态改变（terminate/suspend），立即回到外层判定。
            st = store.get_meta("status")
            if st in ("suspended", "terminated"):
                return

        if availability is not None and not dispatched_any:
            # §5.6.2：本轮**无可调度组**（全部角色阻塞）→ 立即返回。同步环的"等待"退化为
            # 返回，与 M0"无待办即返回"同一机制；**禁止**库内 sleep 忙等（轮询归 orch run）。
            _RUN_LOG.info("%s 本轮无可调度组（全部角色阻塞），返回等待外层轮询",
                          store.thread_dir.name)
            return

        # 一轮 groups 处理完，回到 while 顶部重取 pending（新回复已入队）。


def _transport_failure_fallout(store, target: str, event_ids: list[int],
                               exc: BaseException) -> bool:
    """§5.1 传输级失败（超时 / 进程失败 / 无法解析出信封）的既有 attempts 语义。

        attempts += 1；attempts ≤ 1 → 回到 pending 重派发；否则 failed + system 事件
        to=[moderator] 报告。

    M5 只在其上**叠加** availability.record_failure（见调用点），本函数不碰可用性。
    组是一次 invoke（§5.1 聚合），故按组统一裁决：任一行预算耗尽 → 整组 failed。
    恒返回 False（调用方按"本组未产出回复"继续下一组）。
    """
    new_attempts = [store.bump_attempt(eid, target) for eid in event_ids]
    if max(new_attempts) <= 1:
        for eid in event_ids:
            store.set_pending(eid, target)
        _RUN_LOG.info("%s E%s → %s 传输级失败（attempts=%d），回 pending 重派发：%r",
                      store.thread_dir.name, event_ids, target, max(new_attempts), exc)
        return False
    for eid in event_ids:
        store.mark_failed(eid, target)
    append_system_event(
        store,
        body=(f"角色 {target} 对 E{event_ids} 的调用传输级失败且重试预算耗尽"
              f"（attempts={max(new_attempts)}）：{exc!r}"),
        to=["moderator"],
    )
    return False


def _dispatch_group(
    store,
    config: dict,
    adapters: dict,
    target: str,
    event_ids: list[int],
    *,
    availability: AdapterAvailability | None = None,
    effective: str | None = None,
) -> bool:
    """处理单个 (target, event_ids) 组。成功返回 True，失败（两次 schema 败）返回 False。

    M5 §5.6（availability 非 None 时才启用；为 None 时以下全部退化为不存在）：
      · adapter 实例按**生效绑定名**取（契约 §3）；
      · effective ≠ 主绑定 → 首次追加切换审计事件 + 指标；
      · effective ≠ sessions.backend → 会话作废（sid 空 / gen+1 / backend 更新）+ 本组
        attempts 归零，随后按冷启动路径 invoke；
      · invoke 抛 AdapterUnavailableError → 跳闸（by=auto）+ 审计 + 指标，行回 pending、
        attempts 不变，本轮跳过（下轮重解析由备胎接手）；
      · 其他传输级失败 → 既有 attempts 语义（_transport_failure_fallout）+ record_failure；
      · 成功 invoke → record_success。schema 校验失败**不触碰**可用性（§5.6.3）。

    落盘顺序严格对齐 §5.1：mark_dispatching(+deadline) → invoke → schema 校验（原地重调一次）
    → reply_and_done（系统字段 from/re） → apply bb_ops → verify 钩子已并入定稿 → 终止检查。

    §9.1 崩溃恢复兼容（R-a 修复）：本组进入 invoke 前**再查一次**当前所有 pending 派发行，
    把 target 相同的新行合并入本批 event_ids（§5.1"同目标同批一次 invoke, re=全部 event_ids"）。
    背景：崩溃恢复后可能同时留下上游 pending（recover set_pending）与下游 pending（上一轮已
    落盘），两者在同一 groups 里被分成两组；处理上游组时会立即产出新的下游 pending 派发行，
    与已排在后面的下游组同 target。若不合批就会用**过时的** event_ids 触发号查表，命中 mock
    脚本以外的键（比如 backend.script 无 E4 因为 E4 应聚合到 E5 一起触发 pm）。合批后 max
    (event_ids) 与"若无崩溃则正常聚合"一致，脚本命中恢复正常。合并仅新增 event_ids（不覆盖
    已排入的），也不改变派发行 (event_id, target) 唯一约束。
    """
    if availability is None:
        adapter = adapters[target]      # 老路径逐字不变（角色名键）。
        primary = ""
    else:
        primary = primary_adapter_name(config, target)
        adapter = adapter_instance(adapters, target, str(effective), primary)

    # §9.1 R-a：崩溃恢复后再查一次同 target 的 pending 行，合并入本批。
    # `pending_dispatches()` 只返回 status='pending' 的行；本组在这一步尚未 mark_dispatching。
    # 用 set 去重后按升序落回，保持确定性（§5.1）。
    fresh_ids = {int(r["event_id"]) for r in store.pending_dispatches()
                 if r["target"] == target}
    merged_ids = sorted(set(event_ids) | fresh_ids)
    if merged_ids != list(event_ids):
        event_ids = merged_ids

    _RUN_LOG.info("%s E%s → %s 派发…", store.thread_dir.name, event_ids, target)

    # ————————————————————————————————————————————————————————
    # M5 §5.6.2 换绑前置（**在标 dispatching 之前**，聚合与并行判定不变）：
    #   ① effective ≠ 主绑定 → 首次追加切换审计事件（不生成派发行）+ §13 埋点；
    #   ② effective ≠ sessions.backend → 视为会话死亡：sid 置空 / gen+1 / backend 更新，
    #      并对本组各行 attempts 归零，随后自然走冷启动全量组装（§6.1–6.4）。
    # ————————————————————————————————————————————————————————
    if availability is not None:
        eff = str(effective)
        if eff != primary:
            note_fallback_switch(store, availability, target, primary, eff)
        rebind_session_if_needed(
            store, _session_row(store, target), target, eff, event_ids,
        )

    # 标 dispatching + 落盘绝对截止时间戳（§4.4 事务(2)、§16.2）。
    deadline_ts = time.time() + _timeout_for(config, target)
    for eid in event_ids:
        store.mark_dispatching(eid, target, deadline_ts)

    # R-T2 · E（§8.2 首轮审计兜底，审计 §二 E）：worktree 存在但 store/config 均无
    # last_ok_commit（该角色首轮、config 无兜底）时，旧实现 `if last_ok_commit:` 整段审计
    # 跳过 → 首轮越权写入漏网（fail-open）。修复：在**本轮 invoke 之前**（agent 尚未写入）
    # 取该 worktree 当前 HEAD sha 落盘为 last_ok_commit:{role}——只查 git、不猜测（§16.10）。
    # 必须在 invoke 前捕获：invoke/autocommit 之后 HEAD 会含 agent 写入，届时再取会把越权
    # 提交当成对齐点自 diff 自身（永远合规）。捕获后审计分支恒有对齐点、恒执行。
    _ensure_audit_baseline(store, config, target)

    # R-T3（§6.5 热续增量接入）：由 _render_for_dispatch 统一决策本轮走 render_delta 还是
    # render_view——门控(1)(2)(3) 全满足且 needs_cold_start=False 才发增量视图，否则冷启动
    # 全量；契约 version 变更时先作废 sid 再回退冷启动（不发 delta 视图）。resume_sess 为热续
    # 时传给 invoke 的既有会话（真实 CLI 据此 resume_cmd(sid)）；冷启动为 None。mock/Fake
    # 不支持 resume 时只走冷启动分支，与接线前逐字一致（既有测试与混沌零扰动）。
    view, resume_sess = _render_for_dispatch(store, config, target, event_ids, adapter)
    # §13 采集点3：渲染背景层压缩比（原文/摘要 token）随派发落盘（render 不持 store，§2）。
    _record_render_compression(store, target, view)
    # 落 invoke log 用完整渲染视图文本（§14 / 契约 §4）；mock 仍按 view['event_ids'] 查表。
    view_text = view.get("text", "") if isinstance(view, dict) else str(view)

    # invoke + schema 校验（失败原地重调一次；两次败 → failed + 转 moderator，§5.1）。
    # R-T2 · D：重调那一次**携带错误说明**——用 _view_with_retry_note 在指令尾追加系统
    # 重调说明段（含首次校验错误文本），token 估算同步更新；event_ids 不变（仍只重调一次）。
    attempt = 0
    env: dict | None = None
    sess = resume_sess       # 首次 invoke 携带既有会话（热续）或 None（冷启动）。
    last_errors: list[str] = []
    cur_view = view          # 首次用原视图；失败后切换为携带错误说明的重调视图。
    cur_view_text = view_text
    while attempt <= _MAX_SCHEMA_RETRY:
        try:
            raw_env, sess = adapter.invoke(cur_view, sess)
        except AdapterUnavailableError as exc:
            if availability is None:
                raise           # 未启用 → 既有路径逐字不变（异常照旧上抛）。
            # §5.6.3 第 1 条：立即跳闸（记在**自己解析出的**生效绑定名上）+ 审计 + 指标；
            # 该次失败**不计** attempts，派发行回 pending，本轮跳过 → 下轮重解析接手。
            on_unavailable(store, availability, str(effective), exc)
            for eid in event_ids:
                store.set_pending(eid, target)
            return False
        except TRANSPORT_FAILURE_ERRORS as exc:
            if availability is None:
                raise           # 未启用 → 既有路径逐字不变（异常照旧上抛）。
            # §5.6.3 第 2 条：既有 attempts / 重试语义不变，**叠加** fail_streak 记账。
            on_transport_failure(store, config, availability, str(effective), exc)
            return _transport_failure_fallout(store, target, event_ids, exc)
        if availability is not None:
            # §5.6.3：传输层成功（拿到了输出）→ fail_streak 归零。后续 schema 校验的
            # 成败与可用性无关（输出质量问题不是可用性问题）。
            on_invoke_success(availability, str(effective))
        # 审计原文（§14 一等公民）：本次实际送出的视图文本 + 输出原文。
        store.write_invoke_log(
            event_ids=event_ids, role=target,
            view_text=cur_view_text, output_text=str(raw_env),
        )
        # §13 采集点1：每次 invoke 记 tokens（可复算）+ 可选 cost（有真实用量才记）。
        _record_invoke_tokens(store, target, cur_view_text, raw_env)
        _record_invoke_cost(store, target, adapter)
        errors = orch.protocol.validate_author_fields(raw_env)
        if not errors:
            env = raw_env
            break
        # §13 采集点2：本次 schema 校验失败 → 记一条 schema_retry（首次合法率分子）。
        store.record_metric("schema_retry", 1.0, extra=target)
        last_errors = errors
        attempt += 1
        # §5.1：下一次（原地重调）视图携带本次校验错误说明。
        cur_view = _view_with_retry_note(view, last_errors)
        cur_view_text = str(cur_view.get("text", ""))

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

    # §4.4 间隙(3) invoke_post：adapter.invoke 已返回、reply_and_done 尚未落盘时崩溃
    # （spec 亲标"崩溃高发区，盘上无痕迹"）。此刻盘上：本批派发行仍是 dispatching、
    # 无回复事件——恢复走 §9.1 b)（超时→attempt）或 c)（重派发）。按控制流位置触发
    # （R-T1 Lead §17 裁决）。
    orch.store.fault_check("invoke_post")

    # ————————————————————————————————————————————————————————
    # §4.5 + §8.2 权限三件套接入：仅当该角色有 worktree 时启用（M0/M1 mock skip）。
    # 顺序（spec §5.1 伪代码 (4)）：autocommit → audit_write_scope → 违规拒收+reset+审计。
    # ————————————————————————————————————————————————————————
    worktree_path = _role_worktree(config, target)
    if worktree_path is not None:
        # 生产回路（C-4）：先读 store 里的 last_ok_commit:{role}（前一轮 autocommit 回写），
        # 再兜底 config。审计与 reset 都用同一对齐点（读→审计→reset 前不重取，避免刚
        # 落盘的新 sha 被当成对齐点自 diff 自身，永远合规）。
        last_ok_commit = _last_ok_commit(store, config, target)
        # 用本批最大 event_id 命名 commit（同批一次 invoke，一次 wip 提交，§4.5）。
        commit_evt = max(event_ids)
        new_sha = autocommit(worktree_path, role=target, event_id=commit_evt)

        if last_ok_commit:
            ok, violations = audit_write_scope(
                worktree_path, _write_scope(config, target), last_ok_commit,
            )
            if not ok:
                # §8.2 违规处理：整体拒收该信封 + git reset --hard {last_ok_commit} +
                # 追加 system 审计事件转 moderator（简化决策，不做部分裁剪）。
                # last_ok_commit 是 store/config 里已经确定合法的对齐点，与刚才 autocommit
                # 的 new_sha 无关；不把 new_sha 回写 store（越权提交作废）。
                reset_hard(worktree_path, last_ok_commit)
                for eid in event_ids:
                    store.mark_failed(eid, target)
                append_system_event(
                    store,
                    body=(
                        f"§8.2 write_scope 越权：role={target} "
                        f"E{event_ids} audit rejected，越权路径={violations}；"
                        f"已 git reset --hard 到 {last_ok_commit}。"
                    ),
                    to=["moderator"],
                )
                return False

        # C-4 生产回路：越权拒收路径已 return False；能走到这里 = 审计合规（或对齐点缺失
        # 跳过审计的骨架状态）。若本轮 autocommit 产出了新 sha，把它回写 store，作为下一轮
        # audit 的对齐点（不改 spec DDL，用现有 thread_meta 表，键名 last_ok_commit:{role}）。
        if new_sha:
            store.set_meta(f"last_ok_commit:{target}", new_sha)

    # §4.4 间隙(4) autocommit_post：autocommit + 越权审计（§8.2 权限层）已完成、
    # reply_and_done 尚未落盘时崩溃。按**控制流位置**触发（R-T1 Lead §17 裁决）：
    # mock 无 worktree、autocommit 为 no-op 时该位置依然存在，照样触发；不依赖是否
    # 真的产生 commit。此刻盘上态与 invoke_post 同（回复未落盘），恢复走 §9.1 b)/c)。
    orch.store.fault_check("autocommit_post")

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

    # R-T3（§6.5 热续接线）：把 adapter 返回的会话（{sid,gen}）在**回复落盘同一事务**内
    # upsert 到 sessions 表（§7.5，走 store 既有 reply_and_done 的 session 通道，不新增 store
    # 方法、不改 sessions DDL）。sess 为 None（mock 无会话）或 {sid,gen}（CLI/Fake）——None
    # 时不 upsert，与接线前逐字一致（mock 恒冷启动、混沌零扰动）。
    # M5 §5.6.2：会话表的 backend 列记**生效绑定**（降级期间落主绑定会与"effective ≠
    # sessions.backend 视为会话死亡"的判据自相矛盾）；未启用时仍取角色绑定名（逐字不变）。
    session_upsert = _session_for_upsert(
        store, config, target, event_ids, sess,
        backend=str(effective) if availability is not None else None,
    )

    # [事务(5)] 回复落盘 + 标 done（对本组每一行都标 done）。
    # reply_and_done 只标一行 done；组内其余行单独标 done（同批聚合，一次回复覆盖全组）。
    reply_id = store.reply_and_done(
        done_event_id=event_ids[0], done_target=target, reply=reply,
        session=session_upsert,
    )
    for eid in event_ids[1:]:
        store.mark_done(eid, target)

    _RUN_LOG.info("%s E%d %s 回复落盘（type=%s，re=E%s）",
                  store.thread_dir.name, reply_id, target,
                  reply.get("type"), event_ids)

    # R-T3（§16.9）：会话 upsert 后持久化本轮热续判据基线（last_evt / bb_version / gen）到
    # thread_meta，供下轮 _resume_eligible 从盘重建判断。仅当本轮确实产出会话（sess 非空）时
    # 持久化——mock 无会话 → 不落基线 → 下轮仍冷启动（零扰动）。
    if session_upsert is not None:
        _persist_resume_state(store, config, target, event_ids, sess)

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
