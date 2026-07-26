"""§5.1 异步版核心环 + §5.2 长作业真异步 + §9.3 多线程 workspace 运行（M3-T3）。

本模块与 core.py 同步版**并存**（不删除、不改语义）；测试新用例走 async 路径，
既有 M0/M1/M2 测试仍走 sync 路径（run_thread）。

关键设计（严格对齐 M3 契约 §3）：
  1. 并行判定（§5.1）：一轮 pending groups 按 target 聚合后，用 config[roles][r].write_scope
     两两求交；写域**不相交**的组同批 asyncio.TaskGroup 并行 invoke；写域**相交**的组落
     入串行队列。写域为空（moderator 等 API 型角色）视为不相交（与任何组均可并行）。
  2. SQLite 单连接线程不安全：全模块用一把 asyncio.Lock 保护所有 store 写入
     （events.db 单线程语义）。读取（events(), pending_dispatches, dispatching_rows）
     也在锁内做，避免读写竞争。
  3. adapter 调用：
     - 有 ainvoke（异步 adapter，如 _ParallelBarrierAdapter/_TrackAd）→ 直接 await。
     - 只有 invoke（同步 adapter，如 FakeApiAdapter）→ 用 asyncio.to_thread 包裹。
     两条路径都在 caps.max_concurrent Semaphore 下限流。
  4. 组内单卡失败（schema 校验两次败、audit 越权）不拖累其他卡：TaskGroup 内每卡任务
     捕获自身异常落盘（mark_failed + system event），不再向 TaskGroup 抛出。
  5. 长作业异步（§5.2）：subprocess.Popen 立即返回；asyncio.create_task 轮询 process.poll，
     完成后在锁内 append system event + set_job_status。
  6. 多线程 workspace（§9.3）：为 workspace_dir 下每个 t-* 子目录 create_task 跑
     run_thread_async；per-adapter caps.max_concurrent 用 asyncio.Semaphore 全局限流。

铁律：
  - 系统字段仍编排器权威赋值（§16.11：from/re 一律不信 mock）。
  - 落盘顺序不变：mark_dispatching → invoke → schema 校验 → reply_and_done → bb_ops。
  - 不改 sync 版 run_thread 语义（保 M0/M1/M2 测试绿）。
  - 不引入 aiohttp/aiofiles 等第三方库（asyncio + subprocess 足够）。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any

import orch.protocol
import orch.render
import orch.store

from orch.scheduler._dispatch import session_rows
from orch.scheduler.availability import (
    TRANSPORT_FAILURE_ERRORS,
    AdapterAvailability,
    AdapterUnavailableError,
    adapter_instance,
    apply_rebinding,
    make_availability,
    note_blocked,
    note_fallback_switch,
    note_recovered,
    on_invoke_success,
    on_transport_failure,
    on_unavailable,
    prev_binding,
    primary_adapter_name,
    resolve_binding,
)
from orch.scheduler.core import (
    _MAX_SCHEMA_RETRY,
    _TRIP_RETRY,
    _apply_bb_if_eligible,
    _assemble_view,
    _enforce_sender_constraint,
    _ensure_audit_baseline,
    _finalize_envelope,
    _finish_interrupted_terminate,
    _group_pending,
    _handle_terminate,
    _is_cold_start,
    _last_ok_commit,
    _persist_resume_state,
    _record_invoke_cost,
    _record_invoke_tokens,
    _record_render_compression,
    _render_for_dispatch,
    _role_conf,
    _role_worktree,
    _session_for_upsert,
    _session_row,
    _timeout_for,
    _transport_failure_fallout,
    _verify_downgrade_note,
    _view_with_retry_note,
    _write_scope,
)
from orch.scheduler.permissions import (
    audit_write_scope,
    autocommit,
    reset_hard,
)
from orch.scheduler.systemexec import append_system_event
from orch.scheduler.watchdog import check_watchdogs


# ——————————————————————————————————————————————————————————————
# 每个 Store 一把锁：多协程写同一 sqlite 连接必须串行化
# ——————————————————————————————————————————————————————————————

_STORE_LOCKS: dict[int, asyncio.Lock] = {}


def _store_lock(store) -> asyncio.Lock:
    """按 store 对象身份取 asyncio.Lock（SQLite 单连接不允许多线程并发写）。

    §9.3 多线程场景下每个 Store 各自一把锁：不同线程 store 互不阻塞（各自独立 db）。
    同一 store 内多协程写入必须锁串行化（sqlite 单连接线程不安全）。
    """
    key = id(store)
    lock = _STORE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _STORE_LOCKS[key] = lock
    return lock


# ——————————————————————————————————————————————————————————————
# 并行判定：写域两两相交？
# ——————————————————————————————————————————————————————————————

def _scopes_intersect(a: list[str], b: list[str]) -> bool:
    """两 write_scope 前缀集合是否相交（§8.2/§5.1）。空集视为不相交（API 型无写盘）。

    前缀相交：任一 a 项以 b 项为前缀（或反之，含 '/' 边界）。
    """
    if not a or not b:
        return False
    a_norm = [s.rstrip("/") for s in a if s]
    b_norm = [s.rstrip("/") for s in b if s]
    for x in a_norm:
        for y in b_norm:
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def _partition_parallel_groups(
    config: dict, groups: list[tuple[str, list[int]]]
) -> list[list[tuple[str, list[int]]]]:
    """把 groups 按写域相交关系分成"批"——批内两两写域不相交（可并行），批间串行。

    §5.1：不相交 → asyncio.TaskGroup 并行；相交 → 串行。
    贪心算法：遍历 groups，每个组尝试加入现有批（要求与批内所有组写域不相交）；
    不能加入则新开一批。这保证 O(n^2) 且与 spec §5.1 语义一致（不追求最优装箱）。
    human 派发行由主循环单独处理（此处 groups 不含 target='human'）。
    """
    batches: list[list[tuple[str, list[int]]]] = []
    scopes: list[list[str]] = []  # 每批已并入的组的写域并集（同 batches 索引）

    for tgt, eids in groups:
        my_scope = _write_scope(config, tgt)
        placed = False
        for i, batch in enumerate(batches):
            # 与该批中所有组写域两两不相交才能并入
            ok = True
            for other_tgt, _ in batch:
                other_scope = _write_scope(config, other_tgt)
                if _scopes_intersect(my_scope, other_scope):
                    ok = False
                    break
            if ok:
                batch.append((tgt, eids))
                scopes[i].extend(my_scope)
                placed = True
                break
        if not placed:
            batches.append([(tgt, eids)])
            scopes.append(list(my_scope))
    return batches


# ——————————————————————————————————————————————————————————————
# adapter 调用统一入口：async or sync + Semaphore 限流
# ——————————————————————————————————————————————————————————————

def _adapter_max_concurrent(adapter) -> int:
    """从 adapter.caps.max_concurrent 读并发上限；缺省 1（保守）。"""
    caps = getattr(adapter, "caps", None) or {}
    try:
        n = int(caps.get("max_concurrent", 1))
    except (TypeError, ValueError):
        n = 1
    return max(1, n)


_ADAPTER_SEMAS: dict[int, asyncio.Semaphore] = {}


def _adapter_semaphore(adapter) -> asyncio.Semaphore:
    """按 adapter 对象身份取 Semaphore（§9.3 per-adapter caps.max_concurrent 全局限流）。"""
    key = id(adapter)
    sem = _ADAPTER_SEMAS.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_adapter_max_concurrent(adapter))
        _ADAPTER_SEMAS[key] = sem
    return sem


async def _invoke_adapter(adapter, view: dict, sess: Any) -> tuple[dict, Any]:
    """统一 invoke 入口：async ainvoke 直调；sync invoke 走 asyncio.to_thread。

    加 per-adapter Semaphore 限流（§9.3 caps.max_concurrent）。
    """
    sem = _adapter_semaphore(adapter)
    async with sem:
        if hasattr(adapter, "ainvoke") and callable(adapter.ainvoke):
            # 异步 adapter（如测试中的 _ParallelBarrierAdapter）
            return await adapter.ainvoke(view, sess)
        # 同步 adapter：包 to_thread（不阻塞事件循环）
        return await asyncio.to_thread(adapter.invoke, view, sess)


# ——————————————————————————————————————————————————————————————
# 单组处理（一次 target+event_ids 的 invoke + 落盘）
# ——————————————————————————————————————————————————————————————

async def _dispatch_group_async(
    store,
    config: dict,
    adapters: dict,
    target: str,
    event_ids: list[int],
    lock: asyncio.Lock,
    *,
    availability: AdapterAvailability | None = None,
    effective: str | None = None,
) -> None:
    """§5.6.3 第 1 条的"跳闸→立即重解析→当场重试"外层（与 core._dispatch_group 同源同修）。

    重试在**本协程内串行**完成：不引入任何跨组屏障，并行判定仍只看 §5.1 的写域两两不相交
    （同一 target 的组本就串行，重试不改变任何组间关系）。终止性同同步环：每次重试必伴随
    一次 disable，绑定链有限且跳闸单向，无需计数器。
    """
    while True:
        outcome = await _dispatch_group_async_once(
            store, config, adapters, target, event_ids, lock,
            availability=availability, effective=effective,
        )
        if outcome is not _TRIP_RETRY:
            return
        effective = resolve_binding(config, target, availability)
        if effective is None:
            async with lock:
                note_blocked(store, target, primary_adapter_name(config, target))
            return
        # 有新绑定 → 同一轮内当场以新绑定重跑本组（切换审计 / 换绑冷启动照旧）。


async def _dispatch_group_async_once(
    store,
    config: dict,
    adapters: dict,
    target: str,
    event_ids: list[int],
    lock: asyncio.Lock,
    *,
    availability: AdapterAvailability | None = None,
    effective: str | None = None,
) -> object:
    """处理单个 (target, event_ids) 组的异步版本（**一次**尝试）。

    返回 ``_TRIP_RETRY`` 表示特征命中跳闸、需由 ``_dispatch_group_async`` 同轮换绑重试；
    其余情况返回 None（成功或已落盘的失败）。

    落盘顺序严格对齐 §5.1 与 sync 版 _dispatch_group：
      mark_dispatching → invoke → schema 校验 → autocommit → audit → reply_and_done → bb_ops。
    所有 store 写入都在 lock 内；invoke 本身在锁外（真正的并发发生在 invoke 阶段）。

    组内单卡失败（schema/audit 越权）不抛出到 TaskGroup（避免拖累其他并行组）：
    异常路径通过 mark_failed + system event 落盘表达。

    M5 §5.6（契约 §3 "两条环对等"）：与 core._dispatch_group **复用同一批辅助函数**
    （orch.scheduler.availability）——实例按生效绑定名取、切换审计与换绑冷启动、跳闸
    与 streak 记账全部同源。availability 为 None（config 无 adapter_state_path）时
    以下全部退化为不存在，异常兜底路径逐字保持既有。
    """
    if availability is None:
        adapter = adapters[target]      # 老路径逐字不变（角色名键）。
        primary = ""
    else:
        primary = primary_adapter_name(config, target)
        adapter = adapter_instance(adapters, target, str(effective), primary)

    # § 4.4 事务(2)：标 dispatching + 落 deadline_ts。
    # §9.1 R-a：崩溃恢复兼容——进入 invoke 前再查一次同 target 的 pending 派发行，
    # 把新出现的合并入本批 event_ids（§5.1"同目标同批一次 invoke, re=全部 event_ids"）。
    # 详见 core._dispatch_group 同名注释；异步版同源同修。
    deadline_ts = time.time() + _timeout_for(config, target)
    async with lock:
        fresh_ids = {int(r["event_id"]) for r in store.pending_dispatches()
                     if r["target"] == target}
        merged_ids = sorted(set(event_ids) | fresh_ids)
        if merged_ids != list(event_ids):
            event_ids = merged_ids
        # M5 §5.6.2 换绑前置（**在标 dispatching 之前**，与 core._dispatch_group 同源同修）：
        # prev 先推导（读序）→ **先**换绑落实（attempts 归零 / 会话作废）→ **后**落通告
        # （写序，R7 N3：两步非单事务，先归零才自愈）。全部在锁内（sqlite 单连接串行化）。
        if availability is not None:
            eff = str(effective)
            prev = prev_binding(store, target, primary)
            apply_rebinding(
                store, _session_row(store, target), target, eff, event_ids, prev,
            )
            if eff != primary:
                note_fallback_switch(store, availability, target, primary, eff)
            else:
                note_recovered(store, target, primary)
        for eid in event_ids:
            store.mark_dispatching(eid, target, deadline_ts)
        # R-T2 · E（§8.2 首轮审计兜底，与 core._dispatch_group 同源同修）：worktree 存在但
        # 无 last_ok_commit 时，本轮 invoke 前取 HEAD 落盘为对齐点，使审计恒执行、拦首轮越权。
        _ensure_audit_baseline(store, config, target)
        # R-T3（§6.5 热续接入，与 core._dispatch_group 同源同修）：复用同一决策函数
        # _render_for_dispatch——门控(1)(2)(3) + §6.5 规则2（契约 version 变更作废 sid）决定
        # 走 render_delta 还是冷启动 render_view。决策读/写盘（sessions/thread_meta/黑板）均在
        # 锁内（sqlite 单连接串行化）。resume_sess = 热续时传给 invoke 的既有会话（None=冷启动）。
        view, resume_sess = _render_for_dispatch(store, config, target, event_ids, adapter)
        # §13 采集点3：背景层压缩比随派发落盘（与 core 同源同修；仍在锁内写 store）。
        _record_render_compression(store, target, view)

    view_text = view.get("text", "") if isinstance(view, dict) else str(view)

    # invoke + schema 校验（原地重调一次；两次败 → failed + 转 moderator）
    # R-T2 · D：与 core._dispatch_group 同源同修——重调那一次携带错误说明（_view_with_retry_note
    # 在指令尾追加系统重调说明段，含首次校验错误文本，token 估算同步更新），event_ids 不变。
    attempt = 0
    env: dict | None = None
    sess = resume_sess       # 首次 invoke 携带既有会话（热续）或 None（冷启动）。
    last_errors: list[str] = []
    cur_view = view          # 首次原视图；失败后切换为携带错误说明的重调视图。
    cur_view_text = view_text
    while attempt <= _MAX_SCHEMA_RETRY:
        try:
            raw_env, sess = await _invoke_adapter(adapter, cur_view, sess)
        except Exception as exc:
            # M5 §5.6.3（仅在启用时）：先按分类结果消费，与同步环同源。
            if availability is not None and isinstance(exc, AdapterUnavailableError):
                # 特征命中 → 跳闸 + 审计 + 指标；该次失败不计 attempts，行回 pending，
                # 由外层 _dispatch_group_async **同一轮内立即**重解析（§5.6.3 第 1 条）。
                async with lock:
                    on_unavailable(store, availability, str(effective), exc)
                    for eid in event_ids:
                        store.set_pending(eid, target)
                return _TRIP_RETRY
            if availability is not None and isinstance(exc, TRANSPORT_FAILURE_ERRORS):
                # 其他传输级失败 → 既有 attempts 语义（同源 _transport_failure_fallout）
                # 叠加 fail_streak 记账（达阈值则跳闸 + 审计 + 指标）。
                async with lock:
                    on_transport_failure(
                        store, config, availability, str(effective), exc,
                    )
                    _transport_failure_fallout(store, target, event_ids, exc)
                return
            # invoke 异常视为一次失败（不抛出，落盘）——未启用 M5 或非传输级异常时
            # 走既有兜底路径，逐字不变。
            async with lock:
                for eid in event_ids:
                    store.mark_failed(eid, target)
                append_system_event(
                    store,
                    body=f"角色 {target} 对 E{event_ids} invoke 异常：{exc!r}",
                    to=["moderator"],
                )
            return
        if availability is not None:
            # §5.6.3：传输层成功 → fail_streak 归零（schema 成败与可用性无关）。
            on_invoke_success(availability, str(effective))
        # 审计原文（本次实际送出的视图文本）+ §13 采集点1 tokens/cost（与 core 同源同修）。
        async with lock:
            store.write_invoke_log(
                event_ids=event_ids, role=target,
                view_text=cur_view_text, output_text=str(raw_env),
            )
            _record_invoke_tokens(store, target, cur_view_text, raw_env)
            _record_invoke_cost(store, target, adapter)
        errors = orch.protocol.validate_author_fields(raw_env)
        if not errors:
            env = raw_env
            break
        # §13 采集点2：本次 schema 校验失败 → 记一条 schema_retry（与 core 同源同修）。
        async with lock:
            store.record_metric("schema_retry", 1.0, extra=target)
        last_errors = errors
        attempt += 1
        # §5.1：下一次（原地重调）视图携带本次校验错误说明。
        cur_view = _view_with_retry_note(view, last_errors)
        cur_view_text = str(cur_view.get("text", ""))

    if env is None:
        async with lock:
            for eid in event_ids:
                store.mark_failed(eid, target)
            append_system_event(
                store,
                body=f"角色 {target} 对 E{event_ids} 的回复两次 schema 校验失败：{last_errors}",
                to=["moderator"],
            )
        return

    # §4.4 间隙(3) invoke_post：与 core._dispatch_group 同源同修——invoke 已返回、
    # reply_and_done 尚未落盘时崩溃（"崩溃高发区"）。按控制流位置触发（R-T1 Lead §17）。
    orch.store.fault_check("invoke_post")

    # 权限三件套（M2）：仅当有 worktree 时启用（M0/M1/M3 mock skip）。
    worktree_path = _role_worktree(config, target)
    if worktree_path is not None:
        async with lock:
            last_ok = _last_ok_commit(store, config, target)
        commit_evt = max(event_ids)
        # git 操作走 to_thread（阻塞子进程）
        new_sha = await asyncio.to_thread(
            autocommit, worktree_path, target, commit_evt
        )
        if last_ok:
            ok, violations = await asyncio.to_thread(
                audit_write_scope, worktree_path, _write_scope(config, target), last_ok,
            )
            if not ok:
                await asyncio.to_thread(reset_hard, worktree_path, last_ok)
                async with lock:
                    for eid in event_ids:
                        store.mark_failed(eid, target)
                    append_system_event(
                        store,
                        body=(
                            f"§8.2 write_scope 越权：role={target} "
                            f"E{event_ids} audit rejected，越权路径={violations}；"
                            f"已 git reset --hard 到 {last_ok}。"
                        ),
                        to=["moderator"],
                    )
                return
        if new_sha:
            async with lock:
                store.set_meta(f"last_ok_commit:{target}", new_sha)

    # §4.4 间隙(4) autocommit_post：与 core._dispatch_group 同源同修——autocommit + 审计
    # 已完成、reply_and_done 尚未落盘时崩溃。按控制流位置触发（R-T1 Lead §17）：mock 无
    # worktree、autocommit 为 no-op 时位置依然存在，照样触发。
    orch.store.fault_check("autocommit_post")

    # 定稿信封 + 系统字段 + verify 钩子。
    # **必须走 to_thread**：_finalize_envelope 内的 §8.3 verify 是阻塞 subprocess.run
    # （上限 verify.timeout_s，缺省 120s）。裸调会把整个事件循环焊死那么久——§5.1 写域
    # 并行批内的其它角色协程、§9.3 run_workspace 里 gather 的其余线程全部陪跑。本文件
    # 其余阻塞调用（invoke / autocommit / audit_write_scope / reset_hard）已一律 to_thread，
    # 此处同源。前提见 core._finalize_envelope 文档：它不碰 store（否则跨线程用同一
    # sqlite 连接），改那边要同步改这里。
    reply = await asyncio.to_thread(_finalize_envelope, store, config, target, env)
    # §8.3 行452 后半句（与 core._dispatch_group 同源同修）：紧接定稿推导降级提示正文，
    # 此时 reply['type'] 尚未被 §3.2 改动，判据不会与发送者约束降级串味。
    verify_note = _verify_downgrade_note(target, env, reply)
    reply["re"] = list(event_ids)

    # §3.2 发送者约束
    downgraded_from = _enforce_sender_constraint(config, reply)

    async with lock:
        # §13 batch_size 埋点（M3 契约 §3）
        store.record_metric("batch_size", float(len(event_ids)), extra=target)

        # R-T3（§6.5 热续接线，与 core._dispatch_group 同源同修）：把 adapter 返回的会话
        # （{sid,gen}）在回复落盘同一事务内 upsert 到 sessions 表；sess=None（mock）→ 不 upsert。
        # M5 §5.6.2：启用时 backend 列记**生效绑定**（与换绑判据自洽）。
        session_upsert = _session_for_upsert(
            store, config, target, event_ids, sess,
            backend=str(effective) if availability is not None else None,
        )

        # 回复落盘 + 标 done
        reply_id = store.reply_and_done(
            done_event_id=event_ids[0], done_target=target, reply=reply,
            session=session_upsert,
        )
        for eid in event_ids[1:]:
            store.mark_done(eid, target)

        # R-T3（§16.9）：会话 upsert 后持久化本轮热续判据基线（last_evt/bb_version/gen）。
        if session_upsert is not None:
            _persist_resume_state(store, config, target, event_ids, sess)

        # §8.3 行452：verify 降级 → 追加 system 提示（与 core._dispatch_group 同源同修；
        # 两条环缺一边 = 并发路径上静默失明）。
        if verify_note is not None:
            append_system_event(store, body=verify_note, to=["moderator"])

        if downgraded_from is not None:
            append_system_event(
                store,
                body=(f"发送者约束违规降级为 report：role={target} "
                      f"越权 type={downgraded_from}（§3.2）"),
                to=["moderator"],
            )

        # §3.3 bb_ops
        _apply_bb_if_eligible(store, config, reply, reply_id)

        # 终止检查（§5.4）
        if reply.get("type") == "terminate":
            term_ev = None
            for ev in store.events():
                if ev["id"] == reply_id:
                    term_ev = ev
                    break
            _handle_terminate(store, config, term_ev or {"id": reply_id})


# ——————————————————————————————————————————————————————————————
# 核心环异步版
# ——————————————————————————————————————————————————————————————

async def run_thread_async(
    store: "orch.store.Store",
    config: dict,
    adapters: dict,
) -> None:
    """§5.1 核心循环异步版（M3 契约 §3）。

    每轮 pending groups：
      - human 目标 → gate_wait + suspended，整体返回（§10 挂起停机）。
      - 其余按写域两两相交关系分批：批内并行（asyncio.TaskGroup），批间串行。
    组间/批间的线程 status 变化每次都检查（terminate/suspend 立即回到外层）。
    """
    lock = _store_lock(store)

    # §9.3 多线程生产路径同源崩溃恢复：进入主循环前先补完可能被 kill 打断的终止清算。
    # 与 sync 版 run_thread 顶部逐字同源——复用 core._finish_interrupted_terminate（不复制逻辑）。
    # 若盘上已有 terminate 事件但 status != terminated（在 _handle_terminate 三步中途被 kill），
    # 主循环若直接跑会把终止总结事件的残留 pending 当普通派发去 invoke → 脚本无该事件号 →
    # KeyError（§9.1）。只查表幂等重入（§16.10），须在 store 锁内（多协程串行化写）。
    async with lock:
        _finish_interrupted_terminate(store, config)

    # M5 §5.6：可用性视图（config['adapter_state_path'] 缺失 → None = 整体不启用，
    # 与同步环同一开关）。只造不读，首读在每轮的 reload()。
    availability = make_availability(config)

    while True:
        async with lock:
            status = store.get_meta("status")
        if status in ("suspended", "terminated"):
            return

        # §5.3 看门狗每轮主动调用
        async with lock:
            check_watchdogs(store, config)
            status = store.get_meta("status")
        if status in ("suspended", "terminated"):
            return

        async with lock:
            pending = store.pending_dispatches()
        if not pending:
            return

        # human 目标 → gate_wait + suspended（§10）
        human_ids = [int(r["event_id"]) for r in pending if r["target"] == "human"]
        if human_ids:
            async with lock:
                for eid in human_ids:
                    store.mark_gate_wait(eid, "human")
                store.set_meta("status", "suspended")
            return

        groups = _group_pending(pending)

        # §5.6.1：每轮调度前重读状态文件（与同步环同源；禁止只在启动时读一次）。
        # §5.6.2：解析发生在标 dispatching 之前；聚合与并行判定不变（角色级概念，与绑定无关）。
        effective_by_target: dict[str, str | None] = {}
        schedulable: list[tuple[str, list[int]]] = []
        if availability is not None:
            availability.reload()
            for tgt, eids in groups:
                eff = resolve_binding(config, tgt, availability)
                if eff is None:
                    # 全部不可用 → 本组保持 pending、不 invoke、不消耗 attempts；
                    # 首次进入阻塞态追加通告事件；其余角色照常调度，线程不挂起。
                    async with lock:
                        note_blocked(store, tgt, primary_adapter_name(config, tgt))
                    continue
                effective_by_target[tgt] = eff
                schedulable.append((tgt, eids))
            if not schedulable:
                # 本轮无可调度组 → 返回（"等待"退化为返回，禁止忙等；与同步环同义）。
                return
        else:
            schedulable = list(groups)

        # 按写域相交关系分批：批内并行，批间串行
        batches = _partition_parallel_groups(config, schedulable)

        for batch in batches:
            if len(batch) == 1:
                # 单组批：串行调用
                tgt, eids = batch[0]
                await _dispatch_group_async(
                    store, config, adapters, tgt, eids, lock,
                    availability=availability,
                    effective=effective_by_target.get(tgt),
                )
            else:
                # 多组批：TaskGroup 并行调用（每卡异常已在 _dispatch_group_async 内落盘吸收）
                async with asyncio.TaskGroup() as tg:
                    for tgt, eids in batch:
                        tg.create_task(
                            _dispatch_group_async(
                                store, config, adapters, tgt, eids, lock,
                                availability=availability,
                                effective=effective_by_target.get(tgt),
                            )
                        )

            # 一批处理后检查是否终止/挂起
            async with lock:
                st = store.get_meta("status")
            if st in ("suspended", "terminated"):
                return

        # 回到 while 顶部（新回复入队后可能有新 pending）


# ——————————————————————————————————————————————————————————————
# 长作业真异步（§5.2 / M3 契约 §3）
# ——————————————————————————————————————————————————————————————

def register_async_job(
    store,
    corr: str,
    cmd,
    callback_to: str,
    *,
    started_evt: int | None = None,
    kind: str | None = None,
) -> None:
    """§5.2：非阻塞启动 subprocess，完成后 append system 事件回调。

    - 立即用 subprocess.Popen 启动进程（不 wait/不 communicate 阻塞）；
    - 用 asyncio.create_task 起一个后台协程轮询 process.poll()，完成后：
        1) 更新 jobs 表 status='done'/'failed'（§5.2）；
        2) append_system_event(sender='system', to=[callback_to], corr=corr)（§16.11）。
    - 立即返回，不阻塞调用者（run_thread_async 主流可继续推进）。

    注入本模块的 jobs 表状态语义：register 时插入 'running'（M0 同步版是 'running'；
    见 store.register_job 的 SQL）；完成后由后台任务翻成 'done'/'failed'。

    kind 缺省 = corr（长作业无独立分类时 corr 即语义标签）；started_evt 缺省 0。
    cmd 可以是 str（shell=True）或 list（shell=False）。
    """
    if kind is None:
        kind = corr
    if started_evt is None:
        started_evt = 0
    # cmd 序列化到 jobs.cmd（str）以便日志溯源
    cmd_repr = cmd if isinstance(cmd, str) else " ".join(str(x) for x in cmd)

    # 登记 jobs 表（sync，走 store 原语；此调用在当前事件循环内触发，无锁——注意
    # register_async_job 目前只从 async 环境或测试同步路径外部调用；如需强并发保护，
    # 由外层协程用 _store_lock 序列化）。
    lock = _store_lock(store)

    async def _wait_and_callback(proc: subprocess.Popen) -> None:
        """后台协程：轮询进程结束，落盘回调。"""
        try:
            # 非阻塞轮询（每 50ms 一次），保留事件循环响应性
            while proc.poll() is None:
                await asyncio.sleep(0.05)
            exit_code = int(proc.returncode)
            # 读取一次输出（进程已结束，communicate 立即返回）
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            output = ((stdout or "") + (stderr or ""))[:2000]
            status = "done" if exit_code == 0 else "failed"
            async with lock:
                store.set_job_status(corr, status)
                append_system_event(
                    store,
                    body=f"异步作业({kind}) exit={exit_code}: {output}",
                    to=[callback_to],
                    corr=corr,
                )
        except Exception as exc:
            async with lock:
                try:
                    store.set_job_status(corr, "failed")
                except Exception:
                    pass
                append_system_event(
                    store,
                    body=f"异步作业({kind}) 回调异常：{exc!r}",
                    to=[callback_to],
                    corr=corr,
                )

    # 立即启动子进程（非阻塞）
    is_str = isinstance(cmd, str)
    proc = subprocess.Popen(  # noqa: S603 — §5.2 系统执行器职责
        cmd,
        shell=is_str,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # 登记 jobs 表（M0 同步版直接 sync 调用；此处也保持同步，因为 store 单连接）。
    # 若在异步环境外调用（无运行中 loop），仍能登记。
    store.register_job(
        corr=corr, kind=kind, cmd=cmd_repr,
        callback_to=callback_to, started_evt=started_evt,
    )

    # 起后台协程监视进程结束（要求当前有运行中的 asyncio loop）
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_wait_and_callback(proc))
    except RuntimeError:
        # 无运行中 loop（同步调用场景）：退化为直接等待（保守；测试均在 asyncio.run 内调用）。
        exit_code = proc.wait()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        output = ((stdout or "") + (stderr or ""))[:2000]
        status = "done" if exit_code == 0 else "failed"
        store.set_job_status(corr, status)
        append_system_event(
            store,
            body=f"异步作业({kind}) exit={exit_code}: {output}",
            to=[callback_to],
            corr=corr,
        )


# ——————————————————————————————————————————————————————————————
# 多线程 workspace 运行（§9.3 / M3 契约 §3）
# ——————————————————————————————————————————————————————————————

async def run_workspace(
    workspace_dir,
    config: dict,
    adapters_factory,
) -> None:
    """§9.3：workspace_dir 下每个 t-* 子目录为一个线程，各自跑 run_thread_async。

    - workspace_dir：包含若干 t-* 子目录（一线程一目录一 db，§4.1）。
    - adapters_factory(thread_dir) → adapters dict：为每个线程构造独立 adapters（可复用）。
    - per-adapter caps.max_concurrent 用 Semaphore 全局限流（同一 adapter 对象在跨线程复用
      时通过 _adapter_semaphore 的 id 键实现全局限流；不同 adapter 对象各自独立）。

    实现：为每个 t-* 目录起一个 asyncio.create_task 跑 run_thread_async；用 gather 汇总。
    """
    workspace = Path(workspace_dir)
    thread_dirs = sorted(
        [p for p in workspace.iterdir() if p.is_dir() and p.name.startswith("t-")]
    )
    if not thread_dirs:
        return

    async def _run_one(td: Path) -> None:
        store = orch.store.Store(td)
        adapters = adapters_factory(td)
        await run_thread_async(store, config, adapters)

    await asyncio.gather(*(_run_one(td) for td in thread_dirs))
