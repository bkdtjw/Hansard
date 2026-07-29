"""M5-T1 · 适配器可用性与降级路由 验收测试（测试先行，见红）。

只读依据（顺序即阅读顺序）：
  spec §5.6（5.6.1 状态与存储 / 5.6.2 生效绑定解析 / 5.6.3 自动跳闸 / 5.6.4 边界）、
  §5.1 伪代码、§7.6 末段（错误分类责任在适配层）、§11.1 可用性与降级字段段、
  §12 可用性呈现段、§13 新两行（降级切换次数 / 自动跳闸次数）、§15 M5 行、§9.4。
接口冻结：docs/m5-contract.md（签名 / 键名 / 端点一字为准）。

覆盖分区（每条至少一个用例，按契约符号写；缺失实现自然见红）：
  A  状态模块      orch.adapters.state（load/reload/disable/enable/record_*/snapshot/
                   state_path_for/常量）
  B  生效绑定解析  resolve_effective_adapter
  C  配置校验      validate_availability_config
  D  调度接线      core.run_thread（同步环必测）+ async_core.run_thread_async（冒烟）
  E  CLI           orch adapters / orch adapter disable|enable / orch status
  F  Web           GET /api/adapters、POST /api/adapters/{enable,disable}
  G  E2E           附录B fixture：主绑定 disable → 备胎接手跑到 terminated，终态与基准一致

硬约束（CLAUDE.md / 既有测试惯例）：
  - 顶层只 import 包（orch.adapters / orch.scheduler / orch.store / orch.cli）；
    M5 新符号（orch.adapters.state.* 等）一律在**函数体内**引用，未实现表现为
    运行时红（ImportError/AttributeError），而非 collection 中断。
  - 不 mock 被测对象：状态文件真落盘、Store 真开 sqlite、web 真起 make_server + urllib、
    CLI 真走 typer CliRunner。
  - 无恒真断言、无 try/except 吞错、无 skip/xfail 掩盖（本文件零 skip）。
  - 临时目录用仓库既有 `tmp_dir` fixture（tests/conftest.py 的项目本地 .pytmp/）。

【T1 自决 + 需 Lead 裁决的两处契约缺口（已在汇报 ④ 列出，测试按此写死）】
  (1) 调度层如何拿到状态文件路径：契约 §1 只给 `state_path_for(config_path)`，而
      `run_thread(store, config, adapters)` 只拿到 config **字典**。本测试沿用仓库既有
      "CLI 把派生路径写回 config 字典"惯例（参见 cli/main.py 的 config['worktrees'][role]），
      约定键名 **config['adapter_state_path']**（绝对路径字符串）；同时一并提供
      config['config_path'] 便于实现方改用 state_path_for 派生。
  (2) adapters 映射键：契约 §3 "invoke 用 'adapter 名 → 实例' 映射按 effective 取实例"。
      本测试按**适配器名**作键。既有 302 用例的角色名键因 core._adapter_name 的
      "缺省用角色名兜底"而与之天然兼容（role 无 adapter 声明 → effective == role 名）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

import orch.adapters
import orch.cli
import orch.scheduler
import orch.store

from tests.helpers import EXPECTED_TYPE_SEQUENCE


# ======================================================================
# 公共小工具（只读盘 / 只查表；不 mock 任何被测对象）
# ======================================================================

# 契约 §4 冻结的 M5 审计事件 meta.kind（R3/评审 major-4 追加第四种 adapter_recovered）。
# 凡"与不中断基准比较终态"的口径都按本清单剔除审计事件——漏一种就会在"跑中人工
# enable 回归"的场景里把 recovered 事件算进事件号名次，导致比较口径失真。
_M5_AUDIT_KINDS = (
    "fallback_switch", "adapter_blocked", "adapter_trip", "adapter_recovered",
)


def _dispatch_row(thread_dir, event_id: int, target: str) -> dict | None:
    """直读 dispatches 真相表（既有测试同一姿势，见 test_e2e/_dispatch_status）。"""
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT event_id, target, status, attempts FROM dispatches"
            " WHERE event_id=? AND target=?",
            (event_id, target),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def _dispatch_count_for_event(thread_dir, event_id: int) -> int:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM dispatches WHERE event_id=?", (event_id,)
        ).fetchone()
        return int(row[0])
    finally:
        con.close()


def _session_row(thread_dir, role: str) -> dict | None:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT role, backend, sid, last_evt, gen FROM sessions WHERE role=?",
            (role,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def _metric_rows(thread_dir, key: str) -> list[dict]:
    con = sqlite3.connect(str(Path(thread_dir) / "events.db"))
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT ts, key, value, extra FROM metrics WHERE key=? ORDER BY ts ASC",
            (key,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _audit_events(store, kind: str) -> list[dict]:
    """按 meta_json.kind 取 M5 审计事件（契约 §4 冻结机器字段在 meta 里）。"""
    return [
        ev for ev in store.events()
        if (ev.get("meta") or {}).get("kind") == kind
    ]


def _types_in_order(store) -> list[str]:
    return [e["type"] for e in sorted(store.events(), key=lambda e: e["id"])]


class _CountingMock(orch.adapters.MockAdapter):
    """MockAdapter 的**只加计数**变体：记录每次 invoke 的触发号后原样委托父类。

    不改变任何被测行为（ledger 副作用与返回信封全由父类产生），只为断言
    "到底是主绑定还是备胎接的活"提供可观察点。
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.calls: list[int] = []

    def invoke(self, view: dict, sess: dict | None):
        self.calls.append(max(view["event_ids"]))
        return super().invoke(view, sess)


class _SeqScript(dict):
    """附录B 脚本表的**按调用序**取用替身（只换查表键，不碰适配器行为）。

    为什么需要它（T1 发现的 fixture 耦合，已在汇报 ④ 升级）：
      tests/fixtures/like_feature.yaml 的脚本表以**事件号**为键，而 M5 降级跑会
      合法地在事件流中插入若干条审计事件（§5.6.2 "落盘但不生成派发行"），使其后
      所有事件号整体偏移 → 按事件号查表必然落空（KeyError），与被测逻辑无关。
      附录B 明文允许"事件号偏移"，故这里让脚本表按**取用次序**返回第 i 项
      （i = 该角色第 i 次 invoke），与 M2 契约 §2 已冻结的
      FakeCliAdapter/FakeApiAdapter `scripted_replies[call_no]` 同一惯例。

    只影响"取哪一条预置信封"，不影响 MockAdapter 的任何其它语义：ledger 仍由
    MockAdapter 按**真实事件号**追加（exactly-once 校验依据不被稀释）。
    """

    def __init__(self, table: dict) -> None:
        super().__init__(table)
        self._order = sorted(table)
        self._taken = 0

    def __getitem__(self, _event_id):
        if self._taken >= len(self._order):
            raise KeyError(
                f"脚本已耗尽：第 {self._taken + 1} 次取用，表内只有 {len(self._order)} 项"
            )
        key = self._order[self._taken]
        self._taken += 1
        return super().__getitem__(key)


class _TransportFailAdapter:
    """传输级失败桩（§5.6.3 第 2 条 "连续失败" 路径用）。

    每次 invoke 抛 TimeoutError（spec §5.3/§5.1 的"超时/进程失败"类），
    不含任何 unavailable_patterns 特征词——确保走 streak 路径而非 pattern 路径。
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.calls = 0
        self.caps = {
            "context_window": 0, "tools": [], "write_scope": [],
            "cost_tier": "cheap", "supports_resume": False,
            "timeout_s": 0, "max_concurrent": 1,
        }

    def invoke(self, view: dict, sess: dict | None):
        self.calls += 1
        raise TimeoutError(f"{self.role} transport failure #{self.calls}")


def _fake(role: str, replies: dict[int, dict]):
    """按 call_no（从 1 起）分派的脚本化适配器——与事件号解耦。

    用既有 FakeApiAdapter（M2 契约 §2 冻结：scripted_replies 按 call_no 查表、
    supports_resume 恒 False、返回 sess=None）。因为 M5 会在事件流中插入审计事件、
    事件号会偏移，按 call_no 分派可让脚本不受偏移影响。
    """
    return orch.adapters.FakeApiAdapter(
        role=role, config={}, scripted_replies=replies,
    )


def _handoff(to: list[str], body: str = "交接") -> dict:
    return {"to": list(to), "type": "handoff", "body": body}


def _terminate(body: str = "收工") -> dict:
    return {"to": [], "type": "terminate", "body": body}


# ======================================================================
# A. 状态模块（orch.adapters.state）—— 契约 §1
# ======================================================================

def _state_mod():
    """在函数体内 import，未实现 → ImportError（红），不打断 collection。"""
    import orch.adapters.state as m
    return m


def test_state_load_missing_file_all_enabled(tmp_dir):
    """§5.6.1：文件缺失 → 视为全部 enabled（冷启动默认）。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"
    assert not path.exists()

    av = m.AdapterAvailability.load(path)
    assert av.is_enabled("kimi_cli") is True
    assert av.is_enabled("claude_cli") is True
    # 缺失文件不得被"顺手创建"（§5.6.1 只在写入时落盘）。
    assert isinstance(av.snapshot(), dict)


def test_state_load_corrupt_json_raises(tmp_dir):
    """§5.6.1：文件损坏 → 启动报错，禁止猜测（契约 §1：AdapterStateError）。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"
    path.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(m.AdapterStateError):
        m.AdapterAvailability.load(path)


def test_state_disable_then_reload_reads_back(tmp_dir):
    """disable 落盘后**重新 load** 可读回（跨进程共享的全局文件，§5.6.1）。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"

    av = m.AdapterAvailability.load(path)
    av.disable("kimi_cli", reason="额度耗尽", by="human")
    assert path.exists(), "disable 必须落盘（§5.6.1 原子替换写入）"

    av2 = m.AdapterAvailability.load(path)
    assert av2.is_enabled("kimi_cli") is False
    snap = av2.snapshot()["kimi_cli"]
    assert snap["status"] == "disabled"
    assert snap["by"] == "human"
    assert snap["reason"] == "额度耗尽"


def test_state_enable_restores_and_clears_fail_streak(tmp_dir):
    """§5.6.3：恢复仅限人工 enable，且**同时清零 fail_streak**。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"
    av = m.AdapterAvailability.load(path)

    assert av.record_failure("kimi_cli", trip_after=5, reason="timeout") is False
    assert av.record_failure("kimi_cli", trip_after=5, reason="timeout") is False
    assert av.snapshot()["kimi_cli"]["fail_streak"] == 2

    av.enable("kimi_cli")
    snap = m.AdapterAvailability.load(path).snapshot()["kimi_cli"]
    assert snap["status"] == "enabled"
    assert snap["fail_streak"] == 0


def test_state_record_failure_trips_at_trip_after(tmp_dir):
    """§5.6.3 第2条：fail_streak ≥ trip_after → 跳闸（by=auto），返回值标记"本次跳闸"。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"
    av = m.AdapterAvailability.load(path)

    assert av.record_failure("kimi_cli", trip_after=3, reason="e1") is False
    assert av.record_failure("kimi_cli", trip_after=3, reason="e2") is False
    assert av.record_failure("kimi_cli", trip_after=3, reason="e3") is True

    snap = m.AdapterAvailability.load(path).snapshot()["kimi_cli"]
    assert snap["status"] == "disabled"
    assert snap["by"] == "auto"
    assert snap["fail_streak"] >= 3
    assert av.is_enabled("kimi_cli") is False


def test_state_record_success_resets_streak(tmp_dir):
    """§5.6.3 第2条：成功 invoke 清零 fail_streak。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"
    av = m.AdapterAvailability.load(path)

    av.record_failure("kimi_cli", trip_after=5, reason="e1")
    av.record_failure("kimi_cli", trip_after=5, reason="e2")
    av.record_success("kimi_cli")

    assert av.snapshot()["kimi_cli"]["fail_streak"] == 0
    assert av.is_enabled("kimi_cli") is True


def test_state_writes_are_atomic_replacement(tmp_dir):
    """§5.6.1：写入必须原子替换（临时文件 + rename）。

    可观察判据（不窥探实现细节）：连续多次写盘后，
      · 目标文件**每次**都是完整合法 JSON（没有写到一半的半截文件）；
      · 目录里**不残留**任何临时文件（只有 adapter_state.json 一个文件）。
    """
    m = _state_mod()
    d = tmp_dir / "atomic"
    d.mkdir()
    path = d / "adapter_state.json"
    av = m.AdapterAvailability.load(path)

    for i in range(20):
        if i % 2 == 0:
            av.disable(f"a{i}", reason=f"r{i}", by="human")
        else:
            av.record_failure(f"a{i}", trip_after=99, reason=f"r{i}")
        # 每次写盘后目标文件必须是完整合法 JSON。
        json.loads(path.read_text(encoding="utf-8"))
        # 且不得残留任何临时文件。
        names = sorted(p.name for p in d.iterdir())
        assert names == ["adapter_state.json"], f"第{i}次写盘残留临时文件: {names}"


def test_state_snapshot_has_exactly_five_keys(tmp_dir):
    """契约 §1：snapshot 五键名冻结 {status, reason, by, ts, fail_streak}。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"
    av = m.AdapterAvailability.load(path)
    av.disable("kimi_cli", reason="额度", by="human")

    entry = av.snapshot()["kimi_cli"]
    assert set(entry.keys()) == {"status", "reason", "by", "ts", "fail_streak"}
    assert isinstance(entry["ts"], (int, float))


def test_state_path_for_is_sibling_of_config(tmp_dir):
    """契约 §1：state_path_for(config_path) = 同目录 / adapter_state.json。"""
    m = _state_mod()
    cfg_path = tmp_dir / "config.yaml"
    cfg_path.write_text("roles: {}\n", encoding="utf-8")

    p = Path(m.state_path_for(cfg_path))
    assert p.name == "adapter_state.json"
    assert p.parent == cfg_path.parent


def test_state_module_default_constants(tmp_dir):
    """契约 §1：DEFAULT_TRIP_AFTER=3；DEFAULT_UNAVAILABLE_PATTERNS 为 §17 裁决清单。"""
    m = _state_mod()
    assert m.DEFAULT_TRIP_AFTER == 3
    pats = tuple(m.DEFAULT_UNAVAILABLE_PATTERNS)
    for expected in ("quota", "insufficient", "rate limit", "429", "额度"):
        assert expected in pats, f"§17 默认清单缺 {expected!r}: {pats}"


def test_state_reload_picks_up_external_write(tmp_dir):
    """§5.6.1：写者有二（CLI/控制台 与 调度器）；reload 必须看见外部改动。"""
    m = _state_mod()
    path = tmp_dir / "adapter_state.json"

    sched_view = m.AdapterAvailability.load(path)
    assert sched_view.is_enabled("kimi_cli") is True

    cli_view = m.AdapterAvailability.load(path)
    cli_view.disable("kimi_cli", reason="人工停用", by="human")

    # 未 reload 前允许是旧值；reload 后**必须**是新值（禁止只在启动时读一次）。
    sched_view.reload()
    assert sched_view.is_enabled("kimi_cli") is False


# ======================================================================
# B. 生效绑定解析（resolve_effective_adapter）—— §5.6.2 / 契约 §1
# ======================================================================

def _roles_cfg() -> dict:
    return {
        "pm": {"adapter": "kimi_cli", "fallback": ["claude_cli", "codex_cli"]},
        "moderator": {"adapter": "cheap_api"},          # fallback 缺省
    }


def test_resolve_primary_when_enabled(tmp_dir):
    """§5.6.2：主绑定 enabled → 主绑定。"""
    m = _state_mod()
    av = m.AdapterAvailability.load(tmp_dir / "adapter_state.json")
    assert m.resolve_effective_adapter("pm", _roles_cfg(), av) == "kimi_cli"


def test_resolve_first_enabled_fallback(tmp_dir):
    """§5.6.2：主绑定 disabled → fallback 中**首个** enabled 项。"""
    m = _state_mod()
    av = m.AdapterAvailability.load(tmp_dir / "adapter_state.json")
    av.disable("kimi_cli", reason="额度", by="human")
    assert m.resolve_effective_adapter("pm", _roles_cfg(), av) == "claude_cli"


def test_resolve_skips_disabled_fallback(tmp_dir):
    """§5.6.2："首个 enabled"——跳过同样被禁的备胎，取下一个。"""
    m = _state_mod()
    av = m.AdapterAvailability.load(tmp_dir / "adapter_state.json")
    av.disable("kimi_cli", reason="额度", by="human")
    av.disable("claude_cli", reason="额度", by="auto")
    assert m.resolve_effective_adapter("pm", _roles_cfg(), av) == "codex_cli"


def test_resolve_all_disabled_returns_none(tmp_dir):
    """§5.6.2：全部不可用 → None（调用方据此保持 pending 并通告）。"""
    m = _state_mod()
    av = m.AdapterAvailability.load(tmp_dir / "adapter_state.json")
    for name in ("kimi_cli", "claude_cli", "codex_cli"):
        av.disable(name, reason="额度", by="human")
    assert m.resolve_effective_adapter("pm", _roles_cfg(), av) is None


def test_resolve_fallback_defaults_to_empty_list(tmp_dir):
    """§11.1：fallback 缺省 []（无备胎：不可用即等待人工处理）。"""
    m = _state_mod()
    av = m.AdapterAvailability.load(tmp_dir / "adapter_state.json")
    av.disable("cheap_api", reason="额度", by="human")
    assert m.resolve_effective_adapter("moderator", _roles_cfg(), av) is None


def test_resolve_unrecorded_name_is_enabled(tmp_dir):
    """契约 §1：未记录的名字 = enabled（状态文件只记录被改过的 adapter）。"""
    m = _state_mod()
    av = m.AdapterAvailability.load(tmp_dir / "adapter_state.json")
    av.disable("some_other_adapter", reason="无关", by="human")
    # pm 主绑定 kimi_cli 从未被记录 → 仍视为 enabled。
    assert m.resolve_effective_adapter("pm", _roles_cfg(), av) == "kimi_cli"


# ======================================================================
# C. 配置校验（validate_availability_config）—— §11.1 / 契约 §1
# ======================================================================

def _valid_cfg() -> dict:
    return {
        "adapters": {
            "kimi_cli": {"kind": "cli"},
            "claude_cli": {"kind": "cli"},
            "cheap_api": {"kind": "api"},
        },
        "roles": {
            "moderator": {"adapter": "cheap_api", "fallback": [],
                          "write_scope": [], "tools": []},
            "pm": {"adapter": "kimi_cli", "fallback": ["claude_cli"],
                   "write_scope": ["docs/"], "tools": ["Edit", "Write"]},
        },
    }


def test_validate_accepts_legal_config():
    """§11.1：全合法 → 空错误清单。"""
    m = _state_mod()
    assert m.validate_availability_config(_valid_cfg()) == []


def test_validate_rejects_undeclared_fallback():
    """§11.1：fallback 项必须是**已声明**的 adapter。"""
    m = _state_mod()
    cfg = _valid_cfg()
    cfg["roles"]["pm"]["fallback"] = ["no_such_adapter"]
    errors = m.validate_availability_config(cfg)
    assert errors, "引用未声明 adapter 的 fallback 必须报错"
    assert any("no_such_adapter" in str(e) for e in errors), errors


def test_validate_rejects_api_fallback_for_role_with_tools():
    """§11.1：tools 非空的角色，其 fallback 项必须为 cli 型（API 型不带工具循环 §7.3）。"""
    m = _state_mod()
    cfg = _valid_cfg()
    cfg["roles"]["pm"]["write_scope"] = []
    cfg["roles"]["pm"]["tools"] = ["Edit", "Write"]
    cfg["roles"]["pm"]["fallback"] = ["cheap_api"]
    errors = m.validate_availability_config(cfg)
    assert errors, "tools 非空角色不得以 API 型作 fallback"
    assert any("cheap_api" in str(e) for e in errors), errors


def test_validate_rejects_api_fallback_for_role_with_write_scope():
    """§11.1：write_scope 非空的角色，其 fallback 项同样必须为 cli 型。"""
    m = _state_mod()
    cfg = _valid_cfg()
    cfg["roles"]["pm"]["tools"] = []
    cfg["roles"]["pm"]["write_scope"] = ["docs/"]
    cfg["roles"]["pm"]["fallback"] = ["cheap_api"]
    errors = m.validate_availability_config(cfg)
    assert errors, "write_scope 非空角色不得以 API 型作 fallback"
    assert any("cheap_api" in str(e) for e in errors), errors


def test_validate_rejects_api_primary_for_role_with_tools():
    """§11.1：同一句话约束**主绑定**——tools/write_scope 非空角色主绑定亦须 cli 型。"""
    m = _state_mod()
    cfg = _valid_cfg()
    cfg["roles"]["pm"]["adapter"] = "cheap_api"
    cfg["roles"]["pm"]["fallback"] = ["claude_cli"]
    errors = m.validate_availability_config(cfg)
    assert errors, "tools/write_scope 非空角色的主绑定不得为 API 型"
    assert any("cheap_api" in str(e) for e in errors), errors


# ======================================================================
# D. 调度接线（core.run_thread 同步环 + async_core 冒烟）—— 契约 §3/§4/§5
# ======================================================================

def _sched_config(tmp_dir, *, pm_fallback=("cli_b",), trip_after=None) -> tuple[dict, Path]:
    """M5 调度用最小 config：两角色（pm / moderator）+ 三个已声明 adapter。

    见文件抬头 (1)：状态文件路径经 config['adapter_state_path'] 传给调度层；
    同时提供 config['config_path']（其同目录即 state_path_for 的派生结果）。
    """
    state_path = tmp_dir / "adapter_state.json"
    cfg_path = tmp_dir / "config.yaml"
    cli_a: dict = {"kind": "cli"}
    if trip_after is not None:
        cli_a["trip_after"] = trip_after
    cfg = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {},
        "config_path": str(cfg_path),
        "adapter_state_path": str(state_path),
        "adapters": {
            "cli_a": cli_a,
            "cli_b": {"kind": "cli"},
            "mod_api": {"kind": "api"},
        },
        "roles": {
            "pm": {"adapter": "cli_a", "fallback": list(pm_fallback),
                   "can_decide": True, "write_scope": [], "tools": []},
            "moderator": {"adapter": "mod_api", "can_decide": True,
                          "write_scope": [], "tools": []},
        },
    }
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return cfg, state_path


def _availability(state_path):
    m = _state_mod()
    return m.AdapterAvailability.load(state_path)


def test_d1_disabled_primary_dispatches_via_fallback(thread_dir, tmp_dir):
    """d1 §5.6.2：主绑定 disabled + 有备胎 → 备胎实际接手完成，且五项副作用齐备。

    1) 备胎被 invoke、主绑定一次都没被 invoke；
    2) 产生 meta.kind='fallback_switch' 的 system 事件，且该事件**无派发行**（通告非待办）；
    3) sessions 该角色 backend→备胎、gen+1、sid 置空（视为会话死亡，走冷启动）；
    4) 该派发行 attempts 归零（新后端享有完整重试预算）；
    5) metrics 表出现 key='fallback_switch' 行（§13 降级切换次数）。
    """
    cfg, state_path = _sched_config(tmp_dir)
    _availability(state_path).disable("cli_a", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    # 换绑前既有会话（主绑定）+ 已消耗过的重试预算。
    st.upsert_session(role="pm", sid="sid-old", gen=1, backend="cli_a")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    st.bump_attempt(e1, "pm")
    st.bump_attempt(e1, "pm")
    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == 2

    primary = _fake("pm", {1: _handoff(["moderator"])})
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    orch.scheduler.run_thread(st, cfg, adapters)

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    # (1) 备胎接手，主绑定零调用。
    assert spare.call_no == 1
    assert primary.call_no == 0

    # (2) 切换审计事件 + 无派发行。
    switches = _audit_events(st, "fallback_switch")
    assert len(switches) == 1, [e["body"] for e in switches]
    sw = switches[0]
    assert sw["from"] == "system" and sw["type"] == "system"
    meta = sw["meta"]
    assert meta["role"] == "pm"
    assert meta["primary"] == "cli_a"
    assert meta["effective"] == "cli_b"
    assert _dispatch_count_for_event(thread_dir, sw["id"]) == 0, \
        "§5.6.2：切换审计比照 terminate——落盘但不生成派发行"

    # (3) 会话视为死亡：backend 换、gen+1、sid 置空。
    srow = _session_row(thread_dir, "pm")
    assert srow is not None
    assert srow["backend"] == "cli_b"
    assert srow["sid"] is None
    assert int(srow["gen"]) == 2

    # (4) 换绑重派：该派发行 attempts 归零。
    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == 0

    # (5) §13 降级切换次数埋点。
    rows = _metric_rows(thread_dir, "fallback_switch")
    assert len(rows) == 1, rows
    assert "pm" in str(rows[0]["extra"])


def test_d2_second_dispatch_same_state_adds_no_new_switch_event(thread_dir, tmp_dir):
    """d2 §5.6.2："同一（role，生效绑定）连续派发只在首次记录"（去重靠现查日志）。"""
    cfg, state_path = _sched_config(tmp_dir)
    _availability(state_path).disable("cli_a", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = _fake("pm", {})
    spare = _fake("pm", {
        1: _handoff(["moderator"], "第一次交接"),
        2: {"to": [], "type": "report", "body": "第二次汇报"},
    })
    mod = _fake("moderator", {
        1: {"to": ["pm"], "type": "assign", "body": "再来一轮"},
        2: _terminate(),
    })
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    orch.scheduler.run_thread(st, cfg, adapters)

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert spare.call_no == 2, "pm 应被派发两次（两次都走备胎）"
    assert primary.call_no == 0
    pm_switches = [e for e in _audit_events(st, "fallback_switch")
                   if (e["meta"] or {}).get("role") == "pm"]
    assert len(pm_switches) == 1, \
        f"同状态第二次派发不得新增切换事件，实得 {len(pm_switches)} 条"


def test_d3_all_unavailable_blocks_then_enable_resumes(thread_dir, tmp_dir):
    """d3 §5.6.2 全部不可用：保持 pending / 不耗 attempts / 首次通告；人工 enable 后续跑。

    注：同步环在"本轮无任何可调度组"时必须**返回**（§5.1 的"等待"在同步环退化为返回，
    与 M0 "无待办即返回"同一机制；忙等/内部 sleep 轮询会让本用例挂死——那正是 §5.6.2
    "禁止忙等"要暴露的缺陷）。
    """
    import time as _t

    cfg, state_path = _sched_config(tmp_dir)
    av = _availability(state_path)
    av.disable("cli_a", reason="额度耗尽", by="human")
    av.disable("cli_b", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    st.bump_attempt(e1, "pm")

    primary = _fake("pm", {1: _handoff(["moderator"])})
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    t0 = _t.monotonic()
    orch.scheduler.run_thread(st, cfg, adapters)
    elapsed = _t.monotonic() - t0
    assert elapsed < 5.0, f"§5.6.2 禁止忙等：阻塞态不得空转/长睡（实测 {elapsed:.2f}s）"

    # 保持 pending、attempts 不变、零 invoke。
    row = _dispatch_row(thread_dir, e1, "pm")
    assert row["status"] == "pending", row
    assert row["attempts"] == 1, row
    assert primary.call_no == 0 and spare.call_no == 0
    assert st.get_meta("status") == "running", "阻塞角色不得挂起线程"

    # 首次进入阻塞态 → 一条 adapter_blocked 通告事件，且无派发行。
    blocked = _audit_events(st, "adapter_blocked")
    assert len(blocked) == 1, [e["body"] for e in blocked]
    assert blocked[0]["meta"]["role"] == "pm"
    assert blocked[0]["meta"]["primary"] == "cli_a"
    assert _dispatch_count_for_event(thread_dir, blocked[0]["id"]) == 0

    # 人工 enable 主绑定 → pending 行被主循环自然接手，续跑至完成。
    _availability(state_path).enable("cli_a")
    orch.scheduler.run_thread(st, cfg, adapters)

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert primary.call_no == 1, "enable 后应回归主绑定"
    assert spare.call_no == 0


def test_d4_unavailable_error_trips_without_consuming_attempts(thread_dir, tmp_dir):
    """d4 §5.6.3 第1条（特征命中）：跳闸 by=auto、不计 attempts、行回 pending。

    用契约 §2 的 MockAdapter(unavailable_after=1)：第 1 次 invoke 起恒抛
    AdapterUnavailableError。本用例的角色**无备胎**，以便干净观察"该次失败不计
    attempts + 派发行回 pending"（有备胎时紧接着的换绑会把 attempts 归零，见 d4b）。
    """
    cfg, state_path = _sched_config(tmp_dir, pm_fallback=())

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    st.bump_attempt(e1, "pm")

    primary = orch.adapters.MockAdapter(
        role="pm", script={}, ledger_path=tmp_dir / "ledger.txt",
        unavailable_after=1, unavailable_text="quota exceeded (mock)",
    )
    adapters = {"cli_a": primary, "mod_api": _fake("moderator", {1: _terminate()})}

    orch.scheduler.run_thread(st, cfg, adapters)

    # 状态文件：自动跳闸。
    snap = _availability(state_path).snapshot()["cli_a"]
    assert snap["status"] == "disabled"
    assert snap["by"] == "auto"

    # 跳闸审计事件（trigger=pattern）+ 无派发行。
    trips = _audit_events(st, "adapter_trip")
    assert len(trips) == 1, [e["body"] for e in trips]
    tmeta = trips[0]["meta"]
    assert tmeta["adapter"] == "cli_a"
    assert tmeta["trigger"] == "pattern"
    assert tmeta.get("detail"), "契约 §4：跳闸事件 meta 须含原始报错摘要 detail"
    assert _dispatch_count_for_event(thread_dir, trips[0]["id"]) == 0

    # 该行回 pending、attempts 不变（§5.6.3："该次失败不计 attempts"）。
    row = _dispatch_row(thread_dir, e1, "pm")
    assert row["status"] == "pending", row
    assert row["attempts"] == 1, row

    # §13 自动跳闸次数埋点。
    rows = _metric_rows(thread_dir, "adapter_trip")
    assert len(rows) == 1 and "pattern" in str(rows[0]["extra"]), rows


def test_d4b_trip_then_fallback_takes_over_to_completion(thread_dir, tmp_dir):
    """d4 后半：跳闸后**立即按 §5.6.2 重解析** → 备胎接手，一次 run 跑到 terminated。"""
    cfg, state_path = _sched_config(tmp_dir)

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = orch.adapters.MockAdapter(
        role="pm", script={}, ledger_path=tmp_dir / "ledger.txt",
        unavailable_after=1,
    )
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    orch.scheduler.run_thread(st, cfg, adapters)

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert spare.call_no == 1, "跳闸后应由备胎接手"
    assert _availability(state_path).is_enabled("cli_a") is False
    assert len(_audit_events(st, "adapter_trip")) == 1
    assert len(_audit_events(st, "fallback_switch")) == 1


def test_d5_streak_trip_on_consecutive_transport_failures(thread_dir, tmp_dir):
    """d5 前半 §5.6.3 第2条：连续传输级失败达 trip_after → 跳闸，trigger='streak'。

    trip_after=2；§5.1 attempts 语义不变（第 1 次失败 attempts=1 回 pending 重派、
    第 2 次 attempts=2 → failed + 转 moderator），故一次 run 内恰好 2 次传输级失败。
    """
    cfg, state_path = _sched_config(tmp_dir, pm_fallback=(), trip_after=2)

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = _TransportFailAdapter("pm")
    mod = _fake("moderator", {1: _terminate()})
    adapters = {"cli_a": primary, "mod_api": mod}

    orch.scheduler.run_thread(st, cfg, adapters)

    assert primary.calls == 2, f"§5.1 attempts 语义：应恰好两次传输级失败，实得 {primary.calls}"
    snap = _availability(state_path).snapshot()["cli_a"]
    assert snap["status"] == "disabled"
    assert snap["by"] == "auto"

    trips = _audit_events(st, "adapter_trip")
    assert len(trips) == 1, [e["body"] for e in trips]
    assert trips[0]["meta"]["trigger"] == "streak"
    assert trips[0]["meta"]["adapter"] == "cli_a"
    rows = _metric_rows(thread_dir, "adapter_trip")
    assert len(rows) == 1 and "streak" in str(rows[0]["extra"]), rows


def test_d5b_schema_failure_does_not_grow_fail_streak(thread_dir, tmp_dir):
    """d5 后半 §5.6.3：schema 校验失败**不计入** streak（输出质量 ≠ 可用性）。

    trip_after=1（最敏感设置）：若实现误把 schema 退回计入 streak，第一次非法回复
    就会跳闸——本用例即刻见红。
    """
    cfg, state_path = _sched_config(tmp_dir, pm_fallback=(), trip_after=1)

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    bad = {"to": ["moderator"], "type": "不存在的类型", "body": "非法信封"}
    primary = _fake("pm", {1: dict(bad), 2: dict(bad)})
    mod = _fake("moderator", {1: _terminate()})
    adapters = {"cli_a": primary, "mod_api": mod}

    orch.scheduler.run_thread(st, cfg, adapters)

    assert primary.call_no == 2, "§5.1：非法信封原地重调一次（共两次 invoke）"
    snap = _availability(state_path).snapshot()
    entry = snap.get("cli_a")
    if entry is not None:
        assert entry["status"] == "enabled", entry
        assert entry["fail_streak"] == 0, entry
    assert _availability(state_path).is_enabled("cli_a") is True
    assert _audit_events(st, "adapter_trip") == [], "schema 失败不得触发跳闸"


def test_d6_availability_is_reread_every_round(thread_dir, tmp_dir):
    """§5.6.1：调度器**每轮调度前重读**状态文件（禁止只在启动时读一次）。

    第一段：主绑定可用 → 主绑定接活；线程在 gate 处挂起。
    外部（模拟 CLI/控制台）disable 主绑定后 approve 续跑：
    第二段：同一 config/adapters 对象，必须切到备胎（证明重读生效）。
    """
    cfg, state_path = _sched_config(tmp_dir)

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = _fake("pm", {
        1: {"to": ["human"], "type": "gate_request", "body": "请批准", "corr": "gate-m5"},
    })
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    orch.scheduler.run_thread(st, cfg, adapters)
    assert st.get_meta("status") == "suspended", _types_in_order(st)
    assert primary.call_no == 1 and spare.call_no == 0
    assert _audit_events(st, "fallback_switch") == []

    # 外部写者（CLI/控制台）在两轮之间改状态。
    _availability(state_path).disable("cli_a", reason="额度耗尽", by="human")

    orch.scheduler.apply_gate_decision(
        st, cfg, adapters, corr="gate-m5", approve=True, sender="human",
    )
    orch.scheduler.run_thread(st, cfg, adapters)

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert spare.call_no == 1, "每轮重读状态文件后应切到备胎"
    assert primary.call_no == 1, "主绑定不得在禁用后再被调用"
    assert len(_audit_events(st, "fallback_switch")) == 1


def test_d7_async_ring_fallback_smoke(thread_dir, tmp_dir):
    """d 异步环冒烟（契约 §3："core.py 与 async_core.py 两条环对等"）。"""
    cfg, state_path = _sched_config(tmp_dir)
    _availability(state_path).disable("cli_a", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = _fake("pm", {})
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    asyncio.run(orch.scheduler.run_thread_async(st, cfg, adapters))

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert spare.call_no == 1 and primary.call_no == 0
    switches = _audit_events(st, "fallback_switch")
    assert len(switches) == 1
    assert switches[0]["meta"]["effective"] == "cli_b"
    assert _dispatch_count_for_event(thread_dir, switches[0]["id"]) == 0
    assert len(_metric_rows(thread_dir, "fallback_switch")) == 1


# ======================================================================
# E. CLI（§12 三命令 + status 可用性呈现）—— 契约 §6
# ======================================================================

def _runner():
    from typer.testing import CliRunner
    return CliRunner()


def _cli_config(tmp_dir, *, roles: dict | None = None) -> Path:
    cfg = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "adapters": {
            "cli_a": {"kind": "cli"},
            "cli_b": {"kind": "cli"},
            "mod_api": {"kind": "api"},
        },
        "roles": roles if roles is not None else {
            "pm": {"adapter": "cli_a", "fallback": ["cli_b"],
                   "can_decide": True, "write_scope": [], "tools": []},
            "moderator": {"adapter": "mod_api", "can_decide": True,
                          "write_scope": [], "tools": []},
        },
    }
    p = tmp_dir / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def test_cli_help_lists_adapter_commands():
    """§12：`orch adapters` 与 `orch adapter enable|disable` 必须注册进 CLI。"""
    r = _runner().invoke(orch.cli.app, ["--help"])
    assert r.exit_code == 0, r.output
    assert "adapters" in r.output
    assert "adapter" in r.output


def test_cli_adapters_table_lists_every_adapter_with_badge(tmp_dir):
    """§12：`orch adapters` 列全部 adapter：状态 ✅/⛔、reason、by、ts、fail_streak。"""
    cfg_path = _cli_config(tmp_dir)
    r = _runner().invoke(orch.cli.app, ["adapters", "--config", str(cfg_path)])
    assert r.exit_code == 0, r.output
    for name in ("cli_a", "cli_b", "mod_api"):
        assert name in r.output, f"`orch adapters` 应列出 {name}\n{r.output}"
    assert "✅" in r.output, r.output


def test_cli_adapter_disable_writes_state_and_shows_in_table(tmp_dir):
    """§12：`orch adapter disable X --reason r` → 状态文件与 `orch adapters` 一致变化。"""
    m = _state_mod()
    cfg_path = _cli_config(tmp_dir)

    r = _runner().invoke(orch.cli.app, [
        "adapter", "disable", "cli_a", "--reason", "额度耗尽", "--config", str(cfg_path),
    ])
    assert r.exit_code == 0, r.output

    state_path = Path(m.state_path_for(cfg_path))
    assert state_path.exists(), "disable 必须写透状态文件"
    snap = m.AdapterAvailability.load(state_path).snapshot()["cli_a"]
    assert snap["status"] == "disabled"
    assert snap["by"] == "human"
    assert snap["reason"] == "额度耗尽"

    r2 = _runner().invoke(orch.cli.app, ["adapters", "--config", str(cfg_path)])
    assert r2.exit_code == 0, r2.output
    assert "⛔" in r2.output, r2.output
    assert "额度耗尽" in r2.output, r2.output


def test_cli_adapter_enable_restores_and_clears_streak(tmp_dir):
    """§12：`orch adapter enable X` 恢复可用并清零 fail_streak。"""
    m = _state_mod()
    cfg_path = _cli_config(tmp_dir)
    state_path = Path(m.state_path_for(cfg_path))

    av = m.AdapterAvailability.load(state_path)
    for _ in range(3):
        av.record_failure("cli_a", trip_after=3, reason="timeout")
    assert m.AdapterAvailability.load(state_path).is_enabled("cli_a") is False

    r = _runner().invoke(orch.cli.app, [
        "adapter", "enable", "cli_a", "--config", str(cfg_path),
    ])
    assert r.exit_code == 0, r.output

    snap = m.AdapterAvailability.load(state_path).snapshot()["cli_a"]
    assert snap["status"] == "enabled"
    assert snap["fail_streak"] == 0

    r2 = _runner().invoke(orch.cli.app, ["adapters", "--config", str(cfg_path)])
    assert "✅" in r2.output, r2.output


def _cli_new_thread(tmp_dir) -> str:
    r = _runner().invoke(orch.cli.app, [
        "new", "点赞功能", "--roles", "pm,moderator", "--workspace", str(tmp_dir),
    ])
    assert r.exit_code == 0, r.output
    return next(p.name for p in tmp_dir.iterdir()
                if p.is_dir() and p.name.startswith("t-"))


def test_cli_status_shows_effective_fallback_when_primary_disabled(tmp_dir):
    """§12 可用性呈现：主绑定被禁用时 `orch status` 显示 ⛔ 与生效备胎名。"""
    cfg_path = _cli_config(tmp_dir)
    tid = _cli_new_thread(tmp_dir)

    r0 = _runner().invoke(orch.cli.app, [
        "adapter", "disable", "cli_a", "--reason", "额度耗尽", "--config", str(cfg_path),
    ])
    assert r0.exit_code == 0, r0.output

    r = _runner().invoke(orch.cli.app, [
        "status", tid, "--workspace", str(tmp_dir), "--config", str(cfg_path),
    ])
    assert r.exit_code == 0, r.output
    assert "⛔" in r.output, r.output
    assert "cli_b" in r.output, f"应显示生效备胎名 cli_b\n{r.output}"


def test_cli_status_warns_when_role_has_no_available_adapter(tmp_dir):
    """§12：存在"无可用 adapter"的阻塞角色时必须显著警示（含"无可用"字样）。"""
    cfg_path = _cli_config(tmp_dir)
    tid = _cli_new_thread(tmp_dir)

    for name in ("cli_a", "cli_b"):
        rr = _runner().invoke(orch.cli.app, [
            "adapter", "disable", name, "--reason", "额度耗尽", "--config", str(cfg_path),
        ])
        assert rr.exit_code == 0, rr.output

    r = _runner().invoke(orch.cli.app, [
        "status", tid, "--workspace", str(tmp_dir), "--config", str(cfg_path),
    ])
    assert r.exit_code == 0, r.output
    assert "无可用" in r.output, r.output


# ======================================================================
# F. Web 控制台端点（契约 §7）—— 沿 tests/test_web.py 的 make_server + urllib 姿势
# ======================================================================

def _make_server(workspace: Path):
    import threading
    from orch.web.server import make_server

    srv = make_server(workspace, "127.0.0.1", 0)
    host, port = srv.server_address[0], srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://{host}:{port}", t


class _Serving:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def __enter__(self):
        self.srv, self.base, self.thread = _make_server(self.workspace)
        return self.base

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        return False


def _req(base: str, path: str, method: str = "GET", body: dict | None = None):
    import urllib.error
    import urllib.request

    url = base + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        code = e.code
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw
    return code, parsed


_ADAPTER_ROW_KEYS = {"name", "status", "reason", "by", "ts", "fail_streak"}


def test_web_get_adapters_returns_snapshot_rows(tmp_dir):
    """契约 §7：GET /api/adapters → {"adapters": [{name,status,reason,by,ts,fail_streak}…]}。"""
    _cli_config(tmp_dir)   # workspace 级 config.yaml（声明三个 adapter）
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/adapters")
        assert code == 200, (code, body)
        assert "adapters" in body, body
        rows = body["adapters"]
        assert isinstance(rows, list) and rows, body
        for row in rows:
            assert _ADAPTER_ROW_KEYS <= set(row.keys()), row
        names = {r["name"] for r in rows}
        assert {"cli_a", "cli_b", "mod_api"} <= names, names
        assert all(r["status"] == "enabled" for r in rows), rows


def test_web_post_disable_writes_state_file(tmp_dir):
    """契约 §7：POST /api/adapters/disable 生效并**写透状态文件**（同一原子替换写路径）。"""
    m = _state_mod()
    cfg_path = _cli_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/adapters/disable", "POST",
                          {"name": "cli_a", "reason": "额度耗尽"})
        assert code == 200, (code, body)

        state_path = Path(m.state_path_for(cfg_path))
        assert state_path.exists(), "web disable 必须写透状态文件"
        snap = m.AdapterAvailability.load(state_path).snapshot()["cli_a"]
        assert snap["status"] == "disabled"
        assert snap["by"] == "human"
        assert snap["reason"] == "额度耗尽"

        code, body = _req(base, "/api/adapters")
        assert code == 200, (code, body)
        row = next(r for r in body["adapters"] if r["name"] == "cli_a")
        assert row["status"] == "disabled", row


def test_web_post_enable_restores_and_clears_streak(tmp_dir):
    """契约 §7：POST /api/adapters/enable 恢复可用 + 清零 fail_streak。"""
    m = _state_mod()
    cfg_path = _cli_config(tmp_dir)
    state_path = Path(m.state_path_for(cfg_path))
    av = m.AdapterAvailability.load(state_path)
    for _ in range(3):
        av.record_failure("cli_a", trip_after=3, reason="timeout")

    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/adapters/enable", "POST", {"name": "cli_a"})
        assert code == 200, (code, body)

    snap = m.AdapterAvailability.load(state_path).snapshot()["cli_a"]
    assert snap["status"] == "enabled"
    assert snap["fail_streak"] == 0


def test_web_unknown_adapter_name_400(tmp_dir):
    """契约 §7：未知 name → 400（两个写端点都要）。"""
    _cli_config(tmp_dir)
    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/adapters/disable", "POST",
                          {"name": "no_such_adapter", "reason": "x"})
        assert code == 400, (code, body)
        assert "error" in body, body

        code, body = _req(base, "/api/adapters/enable", "POST",
                          {"name": "no_such_adapter"})
        assert code == 400, (code, body)
        assert "error" in body, body


# ======================================================================
# G. E2E（附录B fixture）：disable 主绑定 → 备胎接手，终态与不中断基准一致
# ======================================================================
#
# 比较口径（沿 R-T1 的"逐字节"精神，见 orch.chaos.BaselineArtifacts）：
#   · mock ledger 与 blackboard/state.json 都内嵌**事件号**（ledger 行 `{role}:{event_id}`、
#     contracts[*].frozen_at、decisions[*].evt）。降级跑合法地多出若干条 M5 审计事件
#     （§5.6.2 "落盘但不生成派发行"），事件号必然整体偏移。
#   · 故按附录B "事件号允许偏移"的既有口径，先把两侧事件号统一映射为"**剔除 M5 审计事件后**
#     的名次"，再做逐字节（规范化 JSON 序列化后逐字符）比较。该映射是双射且确定，
#     不放松任何其它维度：ledger 行数/顺序/角色、黑板契约版本/决策/任务全部仍须逐字相同。

def _rank_map(store) -> dict[int, int]:
    """事件号 → 名次（1 起），剔除 M5 审计事件后按 id 升序编号。"""
    ids = [
        ev["id"] for ev in sorted(store.events(), key=lambda e: e["id"])
        if (ev.get("meta") or {}).get("kind") not in _M5_AUDIT_KINDS
    ]
    return {eid: i + 1 for i, eid in enumerate(ids)}


def _normalized_ledger(ledger_path: Path, rank: dict[int, int]) -> str:
    lines = [
        ln for ln in Path(ledger_path).read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    out = []
    for ln in lines:
        role, _, eid = ln.rpartition(":")
        out.append(f"{role}:{rank[int(eid)]}")
    return "\n".join(out)


def _normalized_state(thread_dir: Path, rank: dict[int, int]) -> str:
    raw = json.loads((Path(thread_dir) / "blackboard" / "state.json").read_text("utf-8"))
    for c in (raw.get("contracts") or {}).values():
        c["frozen_at"] = rank[int(c["frozen_at"])]
    for d in raw.get("decisions") or []:
        d["evt"] = rank[int(d["evt"])]
    return json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True)


def _e2e_config(tmp_dir: Path, roles: list[str]) -> tuple[dict, Path]:
    state_path = tmp_dir / "adapter_state.json"
    cfg_path = tmp_dir / "config.yaml"
    adapters_cfg: dict = {}
    for role in roles:
        adapters_cfg[f"main_{role}"] = {"kind": "cli"}
        adapters_cfg[f"spare_{role}"] = {"kind": "cli"}
    roles_cfg = {
        "moderator": {"can_decide": True, "write_scope": [], "tools": []},
        "pm": {"can_decide": True, "write_scope": ["docs/"], "tools": ["Edit", "Write"]},
        "backend": {"can_decide": False, "write_scope": ["server/"],
                    "tools": ["Edit", "Write"]},
        "frontend": {"can_decide": False, "write_scope": ["web/"],
                     "tools": ["Edit", "Write"]},
        "tester": {"can_decide": False, "write_scope": ["tests/", "reports/"],
                   "tools": ["Edit", "Write"],
                   "verify": {"cmd": "python -c \"print('ok')\"", "cwd": "."}},
    }
    for role in roles_cfg:
        roles_cfg[role]["adapter"] = f"main_{role}"
        roles_cfg[role]["fallback"] = [f"spare_{role}"]
    cfg = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {
            "run_ci": {"cmd": "python -c \"print('ci ok')\"", "cwd": ".", "async": True},
        },
        "config_path": str(cfg_path),
        "adapter_state_path": str(state_path),
        "adapters": adapters_cfg,
        "roles": roles_cfg,
    }
    return cfg, state_path


def _e2e_adapters(script: dict, ledger: Path) -> tuple[dict, dict, dict]:
    """返回 (adapters 映射, main 实例表, spare 实例表)。主备同脚本表、同 ledger。

    每个实例各持一份独立的 `_SeqScript`（各自的取用游标），故"主绑定零调用 +
    备胎从头跑完"与"主绑定跑完 + 备胎零调用"两种跑法产出的信封序列逐条相同。
    """
    main: dict = {}
    spare: dict = {}
    for role, table in script.items():
        main[f"main_{role}"] = _CountingMock(
            role=role, script=_SeqScript(table), ledger_path=ledger,
        )
        spare[f"spare_{role}"] = _CountingMock(
            role=role, script=_SeqScript(table), ledger_path=ledger,
        )
    return {**main, **spare}, main, spare


def _drive_e2e(store, cfg, adapters) -> None:
    """跑到 terminated：中途遇 gate 挂起就 approve 一次（同 tests/test_e2e.py 口径）。"""
    orch.scheduler.run_thread(store, cfg, adapters)
    if store.get_meta("status") == "suspended":
        gate = next(e for e in sorted(store.events(), key=lambda x: x["id"])
                    if e["type"] == "gate_request")
        orch.scheduler.apply_gate_decision(
            store, cfg, adapters,
            corr=gate.get("corr") or "gate-01", approve=True, sender="human",
        )
        orch.scheduler.run_thread(store, cfg, adapters)


def _run_appendix_b(root: Path, like_feature_script: dict, *, disable_primary: bool):
    """跑一遍附录B 任务，返回 (store, thread_dir, ledger, main表, spare表)。"""
    root.mkdir(parents=True, exist_ok=True)
    roles = list(like_feature_script.keys())
    cfg, state_path = _e2e_config(root, roles)
    if disable_primary:
        av = _availability(state_path)
        for role in roles:
            av.disable(f"main_{role}", reason="额度耗尽", by="human")

    tdir = root / "t-001"
    ledger = root / "ledger.txt"
    st = orch.store.Store(tdir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="帖子支持点赞/取消赞", to=[])
    adapters, main, spare = _e2e_adapters(like_feature_script, ledger)
    _drive_e2e(st, cfg, adapters)
    return st, tdir, ledger, main, spare


def test_g_baseline_appendix_b_runs_uninterrupted(tmp_dir, like_feature_script):
    """G 前置：主绑定全部 enabled 时，附录B 基准跑通、零切换事件（不中断基准）。"""
    st, tdir, ledger, main, spare = _run_appendix_b(
        tmp_dir / "base", like_feature_script, disable_primary=False,
    )
    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert _types_in_order(st) == EXPECTED_TYPE_SEQUENCE
    assert _audit_events(st, "fallback_switch") == []
    assert all(inst.calls for inst in main.values()), \
        {k: v.calls for k, v in main.items()}
    assert all(inst.calls == [] for inst in spare.values())


def test_g_fallback_run_matches_baseline_artifacts(tmp_dir, like_feature_script):
    """§15 M5 验收：disable 主绑定 → fallback 接手完成附录B，终态与不中断基准一致。"""
    base_st, base_dir, base_ledger, _bm, _bs = _run_appendix_b(
        tmp_dir / "base", like_feature_script, disable_primary=False,
    )
    assert base_st.get_meta("status") == "terminated"

    st, tdir, ledger, main, spare = _run_appendix_b(
        tmp_dir / "degraded", like_feature_script, disable_primary=True,
    )

    # ① 全程由备胎跑完至 terminated。
    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert all(inst.calls == [] for inst in main.values()), \
        {k: v.calls for k, v in main.items()}
    assert all(inst.calls for inst in spare.values()), \
        {k: v.calls for k, v in spare.items()}

    # ② 每个角色恰一条切换审计**事件**（§5.6.2 "同一（role，生效绑定）连续派发只在
    #    首次记录"），且都不生成派发行。
    switches = _audit_events(st, "fallback_switch")
    assert {e["meta"]["role"] for e in switches} == set(like_feature_script.keys())
    assert len(switches) == len(like_feature_script)
    for ev in switches:
        assert _dispatch_count_for_event(tdir, ev["id"]) == 0

    # ③ §13 "降级切换次数 | 每次 effective ≠ 主绑定的**派发**记一条"——**逐次口径**，
    #    与 ② 的"审计事件每角色首次一条"有意不同：spec §13 与 §5.6.2 措辞不同，
    #    宪法优先，metrics 按 §13 字面逐次派发计数（Lead R1 裁决）。
    #    附录B 降级跑各角色派发次数 = 该角色备胎实例的 invoke 次数，构成：
    #      moderator 5 + pm 2 + backend 3 + frontend 3 + tester 3 = 16 次。
    switch_rows = _metric_rows(tdir, "fallback_switch")
    per_role_dispatches = {inst.role: len(inst.calls) for inst in spare.values()}
    assert per_role_dispatches == {
        "moderator": 5, "pm": 2, "backend": 3, "frontend": 3, "tester": 3,
    }, per_role_dispatches
    assert len(switch_rows) == sum(per_role_dispatches.values()) == 16, (
        f"§13 逐次口径：每次降级派发记一条（期望 16 条），实得 {len(switch_rows)} 条"
    )

    # 稳健叠加（不锁 extra 具体格式——契约 §4 只要求 extra 含 role/from/to）：
    # 按 extra 中出现的角色名分组，角色集合须覆盖全部五角色，逐角色条数须 == 派发次数。
    by_role: dict[str, int] = {}
    for row in switch_rows:
        hits = [r for r in like_feature_script if r in str(row["extra"])]
        assert len(hits) == 1, f"metrics extra 应可唯一定位角色: {row['extra']!r} -> {hits}"
        by_role[hits[0]] = by_role.get(hits[0], 0) + 1
    assert set(by_role) == set(like_feature_script.keys()), by_role
    assert by_role == per_role_dispatches, (by_role, per_role_dispatches)

    # ④ 剔除审计事件后的事件类型序列 == 附录B 期望序列。
    real_types = [
        e["type"] for e in sorted(st.events(), key=lambda x: x["id"])
        if (e.get("meta") or {}).get("kind") not in _M5_AUDIT_KINDS
    ]
    assert real_types == EXPECTED_TYPE_SEQUENCE, real_types

    # ⑤ ledger 与黑板 state.json 与基准逐字节一致（事件号按名次规范化，见本节抬头）。
    base_rank = _rank_map(base_st)
    rank = _rank_map(st)
    assert _normalized_ledger(ledger, rank) == _normalized_ledger(base_ledger, base_rank)
    assert _normalized_state(tdir, rank) == _normalized_state(base_dir, base_rank)


# ======================================================================
# R5 · 独立评审整改的测试先行（Lead 裁决见 IMPLEMENTATION_NOTES.md「M5 独立评审」节）
#
# 统一前缀 test_r5_，便于用 -k 与既有 43 个用例分离。冻结契约逐条对应：
#   major-1  §13 两项指标须有汇总出口（orch metrics + /api/metrics，标签用 §13 行名）
#   major-2  装配复合键 f"{role}::{name}"（每角色×主绑定+各 fallback 一实例，绑该角色
#            worktree/tools）；调度 adapter_instance 兜底链 复合键 → effective → target
#   major-4  新增第四种通告 meta.kind="adapter_recovered"（回归主绑定时，免派发行、
#            首次才记）；switch 去重以"最近一条同类事件之后无 recovered/成功回复"为判据
#   major-5  阻塞通告同理：恢复→再进入阻塞必须重新通告
#   major-6  attempts 归零不得被"有无 sessions 行"把门（prev-binding 从审计链盘上推导）
#   minor-1  裸 `orch status --workspace` 也从 ws/config.yaml 派生可用性呈现
#   minor-2  status 端点补角色投影（primary/effective/blocked）
#   minor-3  状态文件损坏 → 装配启动时探载，一行人话 exit 1（非 Traceback、非进入循环）
#   minor-4  看门狗超时（活循环 kill 路径）计入 fail_streak
#   info-1   异步环补跳闸/阻塞两条取证
# ======================================================================


def _last_number(text: str) -> float:
    """取一行文本里**最后一个**数值（表格行形如 `[8] 降级切换次数(...) : 2.00`）。

    不锁输出措辞/分隔符（§12 只要求字段齐全）；取最后一个数值可避开行首编号
    `[8]`。无数值（例如 "N/A"）→ 断言失败，暴露"有原始量却没汇总出来"。
    """
    import re
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    assert nums, f"该行没有可复算的数值（疑似 N/A）: {text!r}"
    return float(nums[-1])


def _metrics_line_value(output: str, label: str) -> float:
    lines = [ln for ln in output.splitlines() if label in ln]
    assert len(lines) == 1, f"`orch metrics` 应恰有一行含 {label!r}，实得 {lines}"
    return _last_number(lines[0])


def _run_degraded_for_metrics(ws: Path, thread_name: str = "t-m5"):
    """在 ws 下跑一趟"跳闸 → 降级 → 两次降级派发"的 mock 流。

    §13 原始量确定构成（已实测）：
      · adapter_trip     1 条（cli_a 第 1 次 invoke 抛额度错误 → 特征命中跳闸）
      · fallback_switch  2 条（跳闸后同轮换绑接手 1 次 + moderator 再指派后又 1 次；
        §13 "每次 effective ≠ 主绑定的派发记一条"逐次口径）
    返回 (store, thread_dir)。
    """
    cfg, _state_path = _sched_config(ws)
    tdir = ws / thread_name
    st = orch.store.Store(tdir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    primary = orch.adapters.MockAdapter(
        role="pm", script={}, ledger_path=ws / "ledger.txt", unavailable_after=1,
    )
    spare = _fake("pm", {
        1: _handoff(["moderator"], "第一次交接"),
        2: {"to": [], "type": "report", "body": "第二次汇报"},
    })
    mod = _fake("moderator", {
        1: {"to": ["pm"], "type": "assign", "body": "再来一轮"},
        2: _terminate(),
    })
    orch.scheduler.run_thread(
        st, cfg, {"cli_a": primary, "cli_b": spare, "mod_api": mod},
    )
    assert st.get_meta("status") == "terminated", _types_in_order(st)
    return st, tdir


# ——————————————————————————————————————————————————————————————
# major-1 · §13 两项指标的汇总出口（CLI + web）
# ——————————————————————————————————————————————————————————————

def test_r5_major1_cli_metrics_shows_two_section13_rows(tmp_dir):
    """§13：`orch metrics` 必须输出"降级切换次数"与"自动跳闸次数"两行，且可复算。

    可复算 = 输出数值与该线程 metrics 表的现查行数逐一相等（禁止编造/近似）。
    """
    _st, tdir = _run_degraded_for_metrics(tmp_dir)
    switch_n = len(_metric_rows(tdir, "fallback_switch"))
    trip_n = len(_metric_rows(tdir, "adapter_trip"))
    assert (switch_n, trip_n) == (2, 1), (switch_n, trip_n)

    r = _runner().invoke(orch.cli.app, ["metrics", "--workspace", str(tmp_dir)])
    assert r.exit_code == 0, r.output
    assert "降级切换次数" in r.output, r.output
    assert "自动跳闸次数" in r.output, r.output
    assert _metrics_line_value(r.output, "降级切换次数") == float(switch_n)
    assert _metrics_line_value(r.output, "自动跳闸次数") == float(trip_n)


def test_r5_major1_web_metrics_shows_two_section13_rows(tmp_dir):
    """§13：GET /api/metrics 同样必须给出两行，且与 metrics 表现查一致。"""
    _st, tdir = _run_degraded_for_metrics(tmp_dir)
    switch_n = len(_metric_rows(tdir, "fallback_switch"))
    trip_n = len(_metric_rows(tdir, "adapter_trip"))

    with _Serving(tmp_dir) as base:
        code, body = _req(base, "/api/metrics")
        assert code == 200, (code, body)
        rows = body["rows"]
        labels = [str(r["label"]) for r in rows]
        sw = [r for r in rows if "降级切换次数" in str(r["label"])]
        tp = [r for r in rows if "自动跳闸次数" in str(r["label"])]
        assert len(sw) == 1, labels
        assert len(tp) == 1, labels
        assert _last_number(str(sw[0]["value"])) == float(switch_n)
        assert _last_number(str(tp[0]["value"])) == float(trip_n)


# ——————————————————————————————————————————————————————————————
# major-2 · 装配复合键 + 调度取实例兜底链
# ——————————————————————————————————————————————————————————————

def _spec_1111_config(target_repo: Path | None = None) -> dict:
    """spec §11.1 范例形状：pm=kimi_cli(+fallback claude_cli)、backend=claude_cli。

    两个 adapter 均 kind=cli 假命令（本组是**装配层单测**，零真实 CLI 调用）；
    两角色的 tools/write_scope 刻意不同 —— 这正是"一个实例无法同时承载两套
    worktree/tools"的根因，也是复合键必须按 (role, name) 建实例的理由。
    """
    cfg: dict = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "adapters": {
            "kimi_cli": {"kind": "cli", "start_cmd": "kimi-fake -p", "timeout_s": 600},
            "claude_cli": {"kind": "cli", "start_cmd": "claude-fake -p", "timeout_s": 600},
        },
        "roles": {
            "pm": {"adapter": "kimi_cli", "fallback": ["claude_cli"],
                   "can_decide": True, "write_scope": ["docs/"],
                   "tools": ["Edit", "Write"]},
            "backend": {"adapter": "claude_cli", "can_decide": False,
                        "write_scope": ["server/"],
                        "tools": ["Edit", "Write", "Bash(pytest:*)"]},
        },
    }
    if target_repo is not None:
        cfg["target_repo"] = str(target_repo)
    return cfg


def _init_target_repo(root: Path) -> Path:
    """本地真 git 仓库（同 tests/test_permissions.py 口径），供 ensure_worktrees 拉分支。"""
    import subprocess

    def _git(*args):
        subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                       text=True, check=True)

    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "t")
    for d in ("docs", "server"):
        (root / d).mkdir()
        (root / d / ".gitkeep").write_text("", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "init")
    return root


def test_r5_major2_build_adapters_registers_composite_keys(tmp_dir):
    """major-2：§11.1 范例配置过装配层，必须注册 f"{role}::{name}" 复合键。

    评审论证：pm 的 fallback claude_cli 与 backend 的主绑定同名但**配置不同**
    （tools/write_scope 各异），旧实现因"不可共用"而干脆不建实例 → 降级即 KeyError
    （静默停滞）。冻结修复：每角色×(主绑定+各 fallback) 各建一实例，绑该角色配置。
    """
    from orch.cli.main import _build_adapters_from_config

    cfg = _spec_1111_config()
    ads = _build_adapters_from_config(["pm", "backend"], cfg, tmp_dir / "t-001")

    # 复合键三把（pm 主绑定 + pm 备胎 + backend 主绑定）都必须在。
    for key in ("pm::kimi_cli", "pm::claude_cli", "backend::claude_cli"):
        assert key in ads, f"装配缺复合键 {key!r}；实得 {sorted(ads)}"
    # 既有角色名键保持（兜底链最后一环，保 M0–M4 全部调用方与测试绿）。
    assert "pm" in ads and "backend" in ads, sorted(ads)

    # pm 的 claude_cli 实例与 backend 的必须是**不同对象**（一实例扛不了两套配置）。
    assert ads["pm::claude_cli"] is not ads["backend"]
    assert ads["pm::claude_cli"] is not ads["backend::claude_cli"]

    # 该实例归 pm：role 与 tools 都是 pm 的，不是 backend 的。
    inst = ads["pm::claude_cli"]
    assert inst.role == "pm"
    assert list(inst.caps["tools"]) == ["Edit", "Write"], inst.caps["tools"]
    assert list(ads["backend::claude_cli"].caps["tools"]) == [
        "Edit", "Write", "Bash(pytest:*)",
    ]


def test_r5_major2_composite_instance_uses_owner_role_worktree(tmp_dir):
    """major-2 续：pm 的备胎实例 cwd/worktree 必须归 pm，不得串到 backend 的 worktree。

    §8.1 落代码隔离：拿错 worktree 是权限缺陷（不是可用性降级），必须可证伪。
    用本地真 git 仓库让 ensure_worktrees 真建两个 worktree（零真实 CLI 调用）。
    """
    from orch.cli.main import _build_adapters_from_config

    target = _init_target_repo(tmp_dir / "target")
    cfg = _spec_1111_config(target_repo=target)
    ads = _build_adapters_from_config(["pm", "backend"], cfg, tmp_dir / "t-001")

    wts = cfg.get("worktrees") or {}
    assert "pm" in wts and "backend" in wts, wts
    assert Path(wts["pm"]) != Path(wts["backend"])

    assert "pm::claude_cli" in ads, sorted(ads)
    assert Path(ads["pm::claude_cli"].worktree) == Path(wts["pm"])
    assert Path(ads["pm::claude_cli"].worktree) != Path(wts["backend"])
    assert Path(ads["backend::claude_cli"].worktree) == Path(wts["backend"])


def test_r5_major2_adapter_instance_prefers_composite_key():
    """major-2 调度侧：adapter_instance 兜底链 = f"{role}::{effective}" → effective → role。"""
    from orch.scheduler import availability as av

    by_composite, by_name, by_role = object(), object(), object()

    # ① 复合键在场时**优先**（同名不同角色的实例必须各归各的）。
    ads = {"pm::claude_cli": by_composite, "claude_cli": by_name, "pm": by_role}
    assert av.adapter_instance(ads, "pm", "claude_cli", "kimi_cli") is by_composite

    # ② 无复合键 → 退到 adapter 名键（M5 既有分支）。
    ads = {"claude_cli": by_name, "pm": by_role}
    assert av.adapter_instance(ads, "pm", "claude_cli", "kimi_cli") is by_name

    # ③ 两者皆无且生效绑定 == 主绑定 → 退到角色名键（M0–M4 既有映射兼容）。
    ads = {"pm": by_role}
    assert av.adapter_instance(ads, "pm", "pm", "pm") is by_role


# ——————————————————————————————————————————————————————————————
# major-4 · adapter_recovered 通告 + 切换通告的断链重记
# ——————————————————————————————————————————————————————————————

def _gate_env(corr: str) -> dict:
    return {"to": ["human"], "type": "gate_request", "body": "请批准", "corr": corr}


def test_r5_major4_recovered_notice_rearms_switch_notice(thread_dir, tmp_dir):
    """major-4：回归主绑定须落 adapter_recovered（免派发行、连续回归只记一次），
    并把 fallback_switch 的"连续"链**打断**——再次降级必须重新通告。

    四次派发编排（每次回复 gate_request 挂起，人工 approve 后续跑，以便在两次派发
    之间改状态文件）：
      ① disable(cli_a) → 备胎接手（switch #1）
      ② enable(cli_a)  → 回归主绑定（recovered #1）
      ③ 再派发一次仍是主绑定（recovered 不重复记）
      ④ 再 disable(cli_a) → 备胎接手（switch #2：链已被 ② 打断）
    """
    cfg, state_path = _sched_config(tmp_dir)
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = _fake("pm", {1: _gate_env("g2"), 2: _gate_env("g3")})
    spare = _fake("pm", {1: _gate_env("g1"), 2: _gate_env("g4")})
    mod = _fake("moderator", {})       # 不应被派发；若被派发则 KeyError → 红
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    # ① 主绑定停用 → 备胎接手。
    _availability(state_path).disable("cli_a", reason="额度耗尽", by="human")
    orch.scheduler.run_thread(st, cfg, adapters)
    assert st.get_meta("status") == "suspended", _types_in_order(st)
    assert (spare.call_no, primary.call_no) == (1, 0)

    # ② 人工 enable → 下一次派发回归主绑定。
    _availability(state_path).enable("cli_a")
    orch.scheduler.apply_gate_decision(
        st, cfg, adapters, corr="g1", approve=True, sender="human")
    orch.scheduler.run_thread(st, cfg, adapters)
    assert (spare.call_no, primary.call_no) == (1, 1)

    # ③ 连续回归：再派发一次，recovered 不得重复记。
    orch.scheduler.apply_gate_decision(
        st, cfg, adapters, corr="g2", approve=True, sender="human")
    orch.scheduler.run_thread(st, cfg, adapters)
    assert (spare.call_no, primary.call_no) == (1, 2)

    # ④ 再次停用 → 备胎接手；切换通告必须重新记（链已被 recovered 打断）。
    _availability(state_path).disable("cli_a", reason="再次额度耗尽", by="human")
    orch.scheduler.apply_gate_decision(
        st, cfg, adapters, corr="g3", approve=True, sender="human")
    orch.scheduler.run_thread(st, cfg, adapters)
    assert (spare.call_no, primary.call_no) == (2, 2)

    recovered = _audit_events(st, "adapter_recovered")
    assert len(recovered) == 1, [e["body"] for e in recovered]
    assert recovered[0]["from"] == "system" and recovered[0]["type"] == "system"
    assert recovered[0]["meta"]["role"] == "pm"
    assert _dispatch_count_for_event(thread_dir, recovered[0]["id"]) == 0, \
        "adapter_recovered 是通告不是待办：不得生成派发行"

    switches = _audit_events(st, "fallback_switch")
    assert len(switches) == 2, [e["body"] for e in switches]


# ——————————————————————————————————————————————————————————————
# major-5 · 阻塞通告的断链重记
# ——————————————————————————————————————————————————————————————

def test_r5_major5_blocked_notice_rearmed_after_recovery(thread_dir, tmp_dir):
    """major-5：全不可用 → 恢复并成功回复 → 再次全不可用，必须**重新**通告一次。

    spec §5.6.2 的"首次"是"连续状态的首次"；中断后重新进入即新的首次，
    否则运维在第二次阻塞时盘上无锚（通告断链）。
    """
    cfg, state_path = _sched_config(tmp_dir)
    av = _availability(state_path)
    av.disable("cli_a", reason="额度耗尽", by="human")
    av.disable("cli_b", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = _fake("pm", {1: _gate_env("g1")})
    spare = _fake("pm", {})
    mod = _fake("moderator", {})
    adapters = {"cli_a": primary, "cli_b": spare, "mod_api": mod}

    # ① 全不可用 → 阻塞通告 #1，派发行保持 pending，零 invoke。
    orch.scheduler.run_thread(st, cfg, adapters)
    assert len(_audit_events(st, "adapter_blocked")) == 1
    assert (primary.call_no, spare.call_no) == (0, 0)
    assert _dispatch_row(thread_dir, e1, "pm")["status"] == "pending"

    # ② enable 主绑定 → 续跑出一次真实回复（成功回复 = 阻塞态结束的盘上锚）。
    _availability(state_path).enable("cli_a")
    orch.scheduler.run_thread(st, cfg, adapters)
    assert primary.call_no == 1
    assert st.get_meta("status") == "suspended", _types_in_order(st)

    # ③ 再次全不可用 → 新一轮阻塞必须重新通告。
    av2 = _availability(state_path)
    av2.disable("cli_a", reason="再次额度耗尽", by="human")
    assert av2.is_enabled("cli_b") is False
    orch.scheduler.apply_gate_decision(
        st, cfg, adapters, corr="g1", approve=True, sender="human")
    orch.scheduler.run_thread(st, cfg, adapters)

    blocked = _audit_events(st, "adapter_blocked")
    assert len(blocked) == 2, [e["body"] for e in blocked]
    for ev in blocked:
        assert _dispatch_count_for_event(thread_dir, ev["id"]) == 0


# ——————————————————————————————————————————————————————————————
# major-6 · attempts 归零不得被"有无 sessions 行"把门
# ——————————————————————————————————————————————————————————————

def test_r5_major6_attempts_reset_without_session_row(thread_dir, tmp_dir):
    """major-6 §5.6.2："换绑重派时该派发行 attempts 归零"，spec 原文无 sessions 前提。

    mock/API 型角色本就没有 sessions 行（无会话概念），旧实现把归零挂在"有 sessions
    行且 backend 变了"上 → 这类角色降级后仍背着旧后端消耗掉的重试预算。
    """
    cfg, state_path = _sched_config(tmp_dir)
    _availability(state_path).disable("cli_a", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    st.bump_attempt(e1, "pm")
    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == 1
    assert _session_row(thread_dir, "pm") is None, "本用例前提：该角色无 sessions 行"

    primary = _fake("pm", {})
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})
    orch.scheduler.run_thread(
        st, cfg, {"cli_a": primary, "cli_b": spare, "mod_api": mod})

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert spare.call_no == 1 and primary.call_no == 0
    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == 0, \
        "换绑重派后 attempts 必须归零（新后端享有完整重试预算，§5.6.2）"


# ——————————————————————————————————————————————————————————————
# minor-1 · 裸 `orch status --workspace` 也呈现可用性
# ——————————————————————————————————————————————————————————————

def test_r5_minor1_status_without_config_uses_workspace_config(tmp_dir):
    """minor-1 §12：`orch status <t> --workspace <ws>`（不带 --config）也须呈现生效绑定。

    ws 内已有 config.yaml —— 与 `orch run` 同一约定，用户没有理由为看一眼状态
    再手输一遍配置路径；不派生就等于"默认姿势下看不见降级"。
    """
    cfg_path = _cli_config(tmp_dir)
    tid = _cli_new_thread(tmp_dir)

    r0 = _runner().invoke(orch.cli.app, [
        "adapter", "disable", "cli_a", "--reason", "额度耗尽", "--config", str(cfg_path)])
    assert r0.exit_code == 0, r0.output

    r = _runner().invoke(orch.cli.app, ["status", tid, "--workspace", str(tmp_dir)])
    assert r.exit_code == 0, r.output
    assert "⛔" in r.output, r.output
    assert "cli_b" in r.output, f"应显示生效备胎名 cli_b\n{r.output}"

    r1 = _runner().invoke(orch.cli.app, [
        "adapter", "disable", "cli_b", "--reason", "额度耗尽", "--config", str(cfg_path)])
    assert r1.exit_code == 0, r1.output

    r2 = _runner().invoke(orch.cli.app, ["status", tid, "--workspace", str(tmp_dir)])
    assert r2.exit_code == 0, r2.output
    assert "无可用" in r2.output, r2.output


# ——————————————————————————————————————————————————————————————
# minor-2 · web status 端点的角色绑定投影
# ——————————————————————————————————————————————————————————————

def _role_projection(body: dict) -> list[dict]:
    """从 status 响应里按**结构**定位角色投影（容器键名未冻结，建议 "roles"）。

    判据：某个顶层值是"非空的 dict 列表且每项含 role 键"。既有 dispatches 列表的
    键是 event_id/target/status/deadline_ts/attempts，不含 role，不会误命中。
    """
    for value in body.values():
        if (isinstance(value, list) and value
                and all(isinstance(x, dict) and "role" in x for x in value)):
            return value
    raise AssertionError(
        f"status 响应缺角色投影（每项须含 role/primary/effective/blocked）：{body}"
    )


def test_r5_minor2_web_status_exposes_role_binding_projection(tmp_dir):
    """minor-2 §12：控制台角色行要能显示生效绑定与点名警示 → status 端点须给投影。"""
    cfg_path = _cli_config(tmp_dir)
    tid = _cli_new_thread(tmp_dir)
    m = _state_mod()
    state_path = Path(m.state_path_for(cfg_path))

    av = m.AdapterAvailability.load(state_path)
    av.disable("cli_a", reason="额度耗尽", by="human")

    with _Serving(tmp_dir) as base:
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        rows = _role_projection(body)
        pm = next(r for r in rows if r["role"] == "pm")
        for key in ("primary", "effective", "blocked"):
            assert key in pm, pm
        assert pm["primary"] == "cli_a"
        assert pm["effective"] == "cli_b"
        assert pm["blocked"] is False

    # 全链不可用 → blocked 点名该角色。
    m.AdapterAvailability.load(state_path).disable(
        "cli_b", reason="额度耗尽", by="human")
    with _Serving(tmp_dir) as base:
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)
        pm = next(r for r in _role_projection(body) if r["role"] == "pm")
        assert pm["effective"] is None, pm
        assert pm["blocked"] is True, pm


# ——————————————————————————————————————————————————————————————
# minor-3 · 状态文件损坏 → 装配启动即一行人话报错退出
# ——————————————————————————————————————————————————————————————

def _cli_only_config(tmp_dir: Path) -> Path:
    """全 cli 型的 workspace 配置（`orch run` 真实装配只支持 kind=cli）。"""
    cfg = {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "adapters": {
            "cli_a": {"kind": "cli", "start_cmd": "fake-a -p", "timeout_s": 5},
            "cli_b": {"kind": "cli", "start_cmd": "fake-b -p", "timeout_s": 5},
        },
        "roles": {
            "pm": {"adapter": "cli_a", "fallback": ["cli_b"], "can_decide": True,
                   "write_scope": [], "tools": []},
            "moderator": {"adapter": "cli_b", "can_decide": True,
                          "write_scope": [], "tools": []},
        },
    }
    p = tmp_dir / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def test_r5_minor3_corrupt_state_file_aborts_run_with_one_line(tmp_dir):
    """minor-3 §5.6.1"文件损坏 → 启动报错，禁止猜测"：装配期探载 + exit 1 + 无 Traceback。

    旧行为：损坏在 run_thread 内才炸，被 cmd_run 的 per-thread 兜底吞成一行 repr、
    退出码仍 0 —— 既非"启动报错"，也让运维以为跑过了。
    """
    cfg_path = _cli_only_config(tmp_dir)
    _tid = _cli_new_thread(tmp_dir)
    state_path = Path(_state_mod().state_path_for(cfg_path))
    state_path.write_text("{ this is not json", encoding="utf-8")

    r = _runner().invoke(orch.cli.app, ["run", "--workspace", str(tmp_dir), "--once"])
    assert r.exit_code == 1, f"损坏状态文件必须 exit 1，实得 {r.exit_code}\n{r.output}"
    assert "Traceback" not in r.output, r.output
    assert "adapter_state.json" in r.output, r.output


# ——————————————————————————————————————————————————————————————
# minor-4 · 看门狗超时（活循环 kill 路径）计入 fail_streak
# ——————————————————————————————————————————————————————————————

def test_r5_minor4_watchdog_timeout_counts_fail_streak(thread_dir, tmp_dir):
    """minor-4 §5.6.4："超时既走看门狗路径也计入 fail_streak"。

    构造一条过期的 dispatching 行（沿 M1 test_watchdog 姿势：落盘绝对时间戳 +
    注入假时钟 now，绝不 sleep），跑看门狗，断言状态文件里该 adapter 的
    fail_streak 从 0 变 1（既有 attempts+1 语义同时保持）。
    """
    cfg, state_path = _sched_config(tmp_dir, pm_fallback=(), trip_after=5)

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    deadline = 1_000_000.0
    st.mark_dispatching(e1, "pm", deadline)

    actions = orch.scheduler.check_watchdogs(st, cfg, now=deadline + 1.0)

    assert any(a.get("level") == 1 for a in actions), actions
    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == 1, "既有 attempts 语义不变"

    entry = _availability(state_path).snapshot().get("cli_a")
    assert entry is not None, "看门狗超时应在状态文件里给 cli_a 记一次失败"
    assert entry["fail_streak"] == 1, entry
    assert entry["status"] == "enabled", "trip_after=5 时一次超时不应跳闸"


# ——————————————————————————————————————————————————————————————
# info-1 · 异步环取证补两条（与同步环 d4b / d3 对等）
# ——————————————————————————————————————————————————————————————

def test_r5_info1_async_trip_then_fallback_takes_over(thread_dir, tmp_dir):
    """info-1：异步环的"特征命中跳闸 → 备胎接手"取证（同步环 d4b 的 async 版）。"""
    cfg, state_path = _sched_config(tmp_dir)

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    st.append_event(sender="human", type="assign", body="开工", to=["pm"])

    primary = orch.adapters.MockAdapter(
        role="pm", script={}, ledger_path=tmp_dir / "ledger.txt", unavailable_after=1,
    )
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})

    asyncio.run(orch.scheduler.run_thread_async(
        st, cfg, {"cli_a": primary, "cli_b": spare, "mod_api": mod}))

    assert st.get_meta("status") == "terminated", _types_in_order(st)
    assert spare.call_no == 1
    assert _availability(state_path).is_enabled("cli_a") is False
    trips = _audit_events(st, "adapter_trip")
    assert len(trips) == 1 and trips[0]["meta"]["trigger"] == "pattern", trips
    assert len(_audit_events(st, "fallback_switch")) == 1


def test_r5_info1_async_all_unavailable_keeps_pending(thread_dir, tmp_dir):
    """info-1：异步环的"全不可用 → 保持 pending、不耗 attempts、线程不挂起"取证。"""
    cfg, state_path = _sched_config(tmp_dir)
    av = _availability(state_path)
    av.disable("cli_a", reason="额度耗尽", by="human")
    av.disable("cli_b", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    st.bump_attempt(e1, "pm")

    primary = _fake("pm", {})
    spare = _fake("pm", {})
    mod = _fake("moderator", {})

    asyncio.run(orch.scheduler.run_thread_async(
        st, cfg, {"cli_a": primary, "cli_b": spare, "mod_api": mod}))

    row = _dispatch_row(thread_dir, e1, "pm")
    assert row["status"] == "pending", row
    assert row["attempts"] == 1, row
    assert st.get_meta("status") == "running", "阻塞角色不得挂起线程（§5.6.2）"
    assert (primary.call_no, spare.call_no) == (0, 0)
    blocked = _audit_events(st, "adapter_blocked")
    assert len(blocked) == 1, [e["body"] for e in blocked]
    assert _dispatch_count_for_event(thread_dir, blocked[0]["id"]) == 0


# ======================================================================
# R7b · closure 复核整改的测试先行（Lead 回环规格；前缀 test_r7_）
#
#   N1 [blocking] 看门狗放大：滞留 dispatching 行每轮都记 fail_streak → 数轮内把
#      主绑定连同备胎整条链拖垮（假故障，非真实可用性事实）。
#   N3 换绑归零的崩溃窗口：reset_attempts 必须发生在 fallback_switch 审计事件落盘
#      **之前**——否则在两者之间 kill，重启后审计链已表达"已切换"（去重抑制重记），
#      attempts 永远失去归零机会（不自愈）。
#   N2 非 cli 备胎的装配代价：§11.1 允许 tools/write_scope 皆空的角色配 api 型
#      fallback，但真实装配只建 cli 实例 → 降级那一刻运行期 KeyError 崩环。
#      须在**启动装配期**一行人话报错退出（与损坏状态文件同款出口）。
#   N4/N5 按 Lead 规格跳过（web 双线拉取 / 终态线程心跳，改由 R7c 代码修 + 真机验）。
# ======================================================================


def test_r7_n1_watchdog_does_not_amplify_fail_streak(thread_dir, tmp_dir):
    """N1（blocking）：滞留 dispatching 行连跑 6 轮看门狗，fail_streak 恒为 1。

    §5.6.4 只说"超时**既走看门狗路径也计入** fail_streak"——一次超时是一次失败事实，
    不是每轮巡检各一次。滞留行（进程被 kill、行留在 dispatching）会被每一轮 check
    重新枚举；若每轮都记，trip_after=3 时第 3 轮就把主绑定跳闸，之后 resolve 落到备胎
    身上继续每轮记，再 3 轮把备胎也跳闸——整条绑定链被一条陈旧的滞留行拖垮，而这
    期间根本没有发生任何新的 invoke 失败。

    判据（缺一即失败）：
      · cli_a.fail_streak == 1（只在 attempts 0→1 的首轮记这一次）
      · cli_a.status 全程 enabled、零 adapter_trip 事件
      · 备胎 cli_b.fail_streak == 0（不被传染）
      · attempts == 6（§5.3 级别1 既有语义不变：每轮仍计一次 attempt）
    """
    cfg, state_path = _sched_config(tmp_dir, trip_after=3)

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    deadline = 1_000_000.0
    st.mark_dispatching(e1, "pm", deadline)

    rounds = 6
    for k in range(rounds):
        # 假时钟逐轮推进（§16.2：只用落盘绝对时间戳比较，绝不 sleep）。
        orch.scheduler.check_watchdogs(st, cfg, now=deadline + 1.0 + k)

    snap = _availability(state_path).snapshot()
    entry = snap.get("cli_a")
    assert entry is not None, f"首轮超时应给 cli_a 记一次失败：{snap}"
    assert entry["fail_streak"] == 1, (
        f"一条滞留行连跑 {rounds} 轮只算一次超时失败；实得 fail_streak="
        f"{entry['fail_streak']}（每轮重复记 = 假故障放大）"
    )
    assert entry["status"] == "enabled", entry
    assert _audit_events(st, "adapter_trip") == [], \
        [e["body"] for e in _audit_events(st, "adapter_trip")]
    assert (snap.get("cli_b") or {}).get("fail_streak", 0) == 0, \
        f"备胎不得被同一条滞留行传染：{snap.get('cli_b')}"

    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == rounds, \
        "§5.3 级别1 既有语义不变：每轮仍 bump 一次 attempt"


def test_r7_n3_attempts_reset_lands_before_switch_audit(thread_dir, tmp_dir):
    """N3：降级换绑路径里 `reset_attempts` 必须排在 fallback_switch 审计事件落盘之前。

    口径说明（Lead 给的两种写法里选"调用次序"这一种）：
      两步不是单事务。若先落审计、后归零，在两者之间 kill 会留下"审计链已表达已切换、
      attempts 仍是旧值"的盘面——重启后 prev-binding 从审计链推导出的就是备胎，
      `effective == prev` → 归零分支不再进入，这条派发行**永远**失去归零机会（不自愈）。
      反过来先归零、后落审计：窗口里 kill 只会丢掉一条通告事件，重启后 prev 仍推导为
      主绑定 → 归零（幂等重做）+ 补记通告，自愈。故次序本身就是正确性的一部分。

    取证方式：在 Store **实例**上包一层记录用的透明壳（真实方法照常执行，不 mock 被测
    逻辑），只记录 (reset_attempts / 审计事件 append) 的先后。
    """
    cfg, state_path = _sched_config(tmp_dir)
    _availability(state_path).disable("cli_a", reason="额度耗尽", by="human")

    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    e1 = st.append_event(sender="human", type="assign", body="开工", to=["pm"])
    st.bump_attempt(e1, "pm")
    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == 1

    calls: list[str] = []
    real_reset = st.reset_attempts
    real_append = st.append_event

    def _reset_spy(event_id, target):
        calls.append("reset_attempts")
        return real_reset(event_id, target)

    def _append_spy(**kw):
        eid = real_append(**kw)
        kind = (kw.get("meta") or {}).get("kind")
        if kind in _M5_AUDIT_KINDS:
            calls.append(f"audit:{kind}")
        return eid

    st.reset_attempts = _reset_spy
    st.append_event = _append_spy

    primary = _fake("pm", {})
    spare = _fake("pm", {1: _handoff(["moderator"])})
    mod = _fake("moderator", {1: _terminate()})
    orch.scheduler.run_thread(
        st, cfg, {"cli_a": primary, "cli_b": spare, "mod_api": mod})

    assert st.get_meta("status") == "terminated", calls
    assert spare.call_no == 1 and primary.call_no == 0
    assert _dispatch_row(thread_dir, e1, "pm")["attempts"] == 0, "换绑重派须归零"

    assert "reset_attempts" in calls, calls
    assert "audit:fallback_switch" in calls, calls
    assert calls.index("reset_attempts") < calls.index("audit:fallback_switch"), (
        "崩溃窗口：attempts 归零必须先于切换审计落盘，否则两步之间 kill 会让归零"
        f"永久丢失（不自愈）。实测调用序：{calls}"
    )


def _api_fallback_config(tmp_dir: Path) -> Path:
    """§11.1 合法但装配层扛不住的配置：无 tools/write_scope 的角色配 api 型 fallback。

    §11.1 只对"tools 或 write_scope 非空"的角色要求主绑定与 fallback 全为 cli 型，
    故 pm（两者皆空）挂 `cheap_api` 作备胎能通过 validate_availability_config——
    但真实装配只建 cli 实例，降级那一刻按名取实例即 KeyError。
    max_rounds 压到 5：本用例只验启动出口，不让 HEAD 上的旧路径把假 CLI 的失败
    重试灌成长跑（看门狗级别3 会及时挂起线程）。
    """
    cfg = {
        "thread_defaults": {"max_rounds": 5, "loop_limit": 3, "chat_ttl": 10},
        "adapters": {
            "cli_a": {"kind": "cli", "start_cmd": "orch-fake-cli-a -p", "timeout_s": 5},
            "cheap_api": {"kind": "api", "model": "<低成本模型>", "timeout_s": 5},
        },
        "roles": {
            "pm": {"adapter": "cli_a", "fallback": ["cheap_api"],
                   "can_decide": True, "write_scope": [], "tools": []},
            "moderator": {"adapter": "cli_a", "can_decide": True,
                          "write_scope": [], "tools": []},
        },
    }
    p = tmp_dir / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return p


def test_r7_n2_api_fallback_aborts_at_assembly_not_runtime(tmp_dir):
    """N2：api 型 fallback 无法装配 → 启动装配期一行人话 exit 1，而不是运行期 KeyError。

    §11.1 放行这种配置（角色无 tools/write_scope），装配层却只建 cli 实例：主绑定一旦
    停用/跳闸，`adapter_instance` 按名取不到备胎实例 → KeyError 穿出 run_thread、被
    cmd_run 的 per-thread 兜底吞成一行 repr、退出码仍 0 —— 用户看到的是"跑过了"，
    实际整条线程静默停滞。出口须与损坏状态文件一致：启动即报错退出。
    """
    cfg_path = _api_fallback_config(tmp_dir)
    _tid = _cli_new_thread(tmp_dir)
    assert cfg_path.exists()

    r = _runner().invoke(orch.cli.app, ["run", "--workspace", str(tmp_dir), "--once"])

    assert r.exit_code == 1, (
        f"api 型 fallback 无法装配，必须启动即 exit 1；实得 {r.exit_code}\n{r.output}"
    )
    assert "Traceback" not in r.output, r.output
    assert "KeyError" not in r.output, (
        "不得把它拖到运行期变成 KeyError 崩环（那是静默停滞，不是响亮失败）\n" + r.output
    )
    assert "cheap_api" in r.output, r.output
    assert "pm" in r.output, r.output
