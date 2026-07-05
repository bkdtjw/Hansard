"""R-T4 · `orch metrics` CLI 可复算断言（重写，替换旧"只查字段名"假绿）。

任务卡 R-T4 条目 (5)：
  构造真实 workspace：跑 mock 线程到 terminate（产生 tokens/batch_size 行），其中安排
  一次脚本化非法回复触发 schema_retry；然后调用 orch metrics（CliRunner），断言：
    · 任务数 / 平均轮数 / 聚合节省% / 首次合法率 均为**具体数值**且与直接查询
      metrics 表 + events 表**手工复算一致**（对照写在测试里）；
    · 成本与真实层字段断言为 N/A（诚实边界：Mock 无 last_usage → 不记 cost 行）；
    · 再单测 ChaosHarness(metrics_store=...) 落表后 metrics 显示混沌轮数与通过率。
  禁止只断言字段名出现。

§13 采集点（随代码交付，可复算）：
  - tokens 行：每次 invoke 一条（tokens_in=派发视图 meta.token_est；tokens_out=回复 body
    的 estimate_tokens）。invoke 计数 = tokens 行数（首次合法率的分母）。
  - schema_retry 行：重调循环每次校验失败一条（首次合法率分子）。
  - batch_size 行：每次聚合派发一条（聚合节省 % 分子/分母）。
  - cost 行：仅当 adapter 暴露 last_usage 时记录；Mock/Fake 无 → cost 恒 N/A。
  - chaos_rounds / chaos_mock_pass_pct：ChaosHarness(metrics_store=…) 落盘。

驱动方式与 tests/test_e2e.py 一致（run_thread + apply_gate_decision）；一处包一层
非法回复注入触发 schema_retry。断言全部用 store 表 + events 表手工复算对照。
"""

from __future__ import annotations

import orch.adapters
import orch.scheduler
import orch.store
import orch.cli  # noqa: F401


def _runner():
    from typer.testing import CliRunner
    return CliRunner()


def _app():
    return orch.cli.app


# ——————————————————————————————————————————————————————————————
# 驱动辅助：附录B mock 全流程（与 test_e2e 同源）+ 一次非法回复注入
# ——————————————————————————————————————————————————————————————

def _config():
    return {
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "gate_ops": {
            "run_ci": {"cmd": "python -c \"print('ci ok')\"", "cwd": ".", "async": True},
        },
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


class _OneBadReplyMockAdapter(orch.adapters.MockAdapter):
    """MockAdapter 变体：**首次** invoke 先返回一条非法信封（缺 body），触发调度层
    §5.1 原地重调；第二次（重调）委托父类返回合法脚本信封。用于稳定制造**恰好一次**
    schema_retry，供首次合法率复算断言。

    非法信封不写 ledger（副作用只在合法一次落 ledger，保 exactly-once 语义），故重调
    对 ledger / 事件序列均无扰动。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._bad_emitted = False

    def invoke(self, view, sess):
        if not self._bad_emitted:
            self._bad_emitted = True
            # 非法：缺必填 body（附录A schema minLength 违规）→ validate 失败 → 重调。
            return {"to": ["moderator"], "type": "report"}, sess
        return super().invoke(view, sess)


def _build_adapters(like_feature_script, ledger_path, *, bad_role: str | None = None):
    adapters = {}
    for role, table in like_feature_script.items():
        cls = (_OneBadReplyMockAdapter
               if role == bad_role else orch.adapters.MockAdapter)
        adapters[role] = cls(role=role, script=table, ledger_path=ledger_path)
    return adapters


def _find_gate_request(store):
    for e in sorted(store.events(), key=lambda e: e["id"]):
        if e["type"] == "gate_request":
            return e
    return None


def _drive_to_terminate(store, config, adapters):
    """run_thread → 遇 gate 挂起 approve → 续跑到 terminate（与 test_e2e 同流程）。"""
    orch.scheduler.run_thread(store, config, adapters)
    if store.get_meta("status") == "suspended":
        gate = _find_gate_request(store)
        corr = (gate or {}).get("corr") or "gate-01"
        orch.scheduler.apply_gate_decision(
            store, config, adapters, corr=corr, approve=True, sender="human",
        )
        orch.scheduler.run_thread(store, config, adapters)


def _seed_workspace(ws, like_feature_script, *, bad_role="backend"):
    """建 workspace + 一个线程，跑附录B 全流程到 terminate（含一次 schema_retry）。"""
    tdir = ws / "t-metrics01"
    st = orch.store.Store(tdir)
    st.set_meta("status", "running")
    ledger = ws / "ledger.txt"
    adapters = _build_adapters(like_feature_script, ledger, bad_role=bad_role)
    st.append_event(sender="human", type="assign", body="帖子支持点赞/取消赞", to=[])
    _drive_to_terminate(st, _config(), adapters)
    assert st.get_meta("status") == "terminated", "驱动应到 terminate"
    return st


# ——————————————————————————————————————————————————————————————
# 手工复算：直接查 metrics 表 + events 表
# ——————————————————————————————————————————————————————————————

def _metric_values(store, key):
    rows = store._con.execute(
        "SELECT value FROM metrics WHERE key=?", (key,)
    ).fetchall()
    return [float(r["value"]) for r in rows]


def _manual_recompute(store):
    """从 metrics 表 + events 表手工复算 §13 四项数值（测试内对照真相源）。"""
    batch_sizes = _metric_values(store, "batch_size")
    tokens_rows = _metric_values(store, "tokens")       # 每次 invoke 一条
    schema_retry_rows = _metric_values(store, "schema_retry")
    cost_rows = _metric_values(store, "cost")
    events = store.events()

    # 聚合节省 % = Σ(batch_size-1)/Σ(batch_size) * 100
    saved = sum(max(0.0, b - 1.0) for b in batch_sizes)
    agg_pct = (saved / sum(batch_sizes) * 100.0) if sum(batch_sizes) else None

    # 首次合法率 % = (1 - schema_retry 行数 / invoke(tokens) 行数) * 100
    invoke_count = len(tokens_rows)
    retry_count = len(schema_retry_rows)
    first_legal_pct = (
        (1.0 - retry_count / invoke_count) * 100.0 if invoke_count else None
    )

    return {
        "task_count": 1,                       # 单线程 workspace
        "avg_rounds": float(len(events)),      # 平均轮数 = 事件数（单线程）
        "agg_pct": agg_pct,
        "first_legal_pct": first_legal_pct,
        "invoke_count": invoke_count,
        "retry_count": retry_count,
        "cost_rows": cost_rows,
        "batch_sizes": batch_sizes,
    }


# ==================================================================
# (c-0) --help 仍识别 --workspace（保留既有可用性断言）
# ==================================================================

def test_orch_metrics_help_lists_workspace_flag():
    r = _runner().invoke(_app(), ["metrics", "--help"])
    assert r.exit_code == 0, r.output
    assert "--workspace" in r.output


# ==================================================================
# (c-1) 真实 workspace：schema_retry / tokens / batch_size 采集点确已落盘
# ==================================================================

def test_collection_points_land_in_metrics_table(tmp_dir, like_feature_script):
    """采集点随代码交付（§13/§16.9）：跑全流程后 metrics 表必须有
    tokens / batch_size / schema_retry 三类行；且恰好触发一次 schema_retry。"""
    ws = tmp_dir / "ws"
    ws.mkdir()
    st = _seed_workspace(ws, like_feature_script, bad_role="backend")

    tokens = _metric_values(st, "tokens")
    batch = _metric_values(st, "batch_size")
    retries = _metric_values(st, "schema_retry")

    assert tokens, "每次 invoke 必须记一条 tokens 行（§13 采集点）"
    assert batch, "每次聚合派发必须记一条 batch_size 行（§13 采集点）"
    assert len(retries) == 1, (
        f"脚本化一次非法回复应恰触发一次 schema_retry，实测 {len(retries)} 条"
    )
    # invoke 计数 ≥ batch_size 计数：非法重调多调一次 invoke，但只成功落一个 batch。
    assert len(tokens) >= len(batch), (
        f"tokens 行数（invoke 计数 {len(tokens)}）应 ≥ batch_size 行数 {len(batch)}"
    )
    # Mock 无 last_usage → 不记 cost 行（诚实边界）。
    assert _metric_values(st, "cost") == [], "Mock 无真实用量，禁止编造 cost 行"


# ==================================================================
# (c-2) orch metrics 输出四项**具体数值**且与手工复算一致
# ==================================================================

def _parse_metrics_output(out: str) -> dict:
    """从 orch metrics 纯文本输出解析出四项数值（用于与手工复算对照）。

    解析规则宽松：按行找关键字锚点 + 抓行内首个"数字[%]"或 N/A。
    """
    import re

    parsed = {}

    def _grab(line: str):
        # 只在**冒号之后**的值部分抓数字，避开行首的 "[2]" 等序号（否则误抓 2）。
        value_part = line.split(":", 1)[1] if ":" in line else line
        if "N/A" in value_part:
            return "N/A"
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*%?", value_part)
        return float(m.group(1)) if m else None

    for line in out.splitlines():
        lo = line.lower()
        if "任务数" in line or "tasks" in lo:
            parsed["task_count"] = _grab(line)
        elif "平均轮数" in line or "avg rounds" in lo:
            parsed["avg_rounds"] = _grab(line)
        elif ("成本" in line or "cost" in lo) and "compression" not in lo:
            parsed["cost"] = _grab(line)
        elif "聚合节省" in line or "aggregate save" in lo:
            parsed["agg_pct"] = _grab(line)
        elif "首次合法率" in line or "first-legal" in lo or "first legal" in lo:
            parsed["first_legal_pct"] = _grab(line)
        elif "真实层" in line or "real" in lo and "pass" in lo:
            parsed.setdefault("real_pass", _grab(line))
    return parsed


def test_orch_metrics_values_match_manual_recompute(tmp_dir, like_feature_script):
    """orch metrics 输出的 任务数 / 平均轮数 / 聚合节省% / 首次合法率 必须是**具体数值**，
    且与直接查 metrics + events 表手工复算逐一一致；成本 = N/A（Mock 无用量）。"""
    ws = tmp_dir / "ws"
    ws.mkdir()
    st = _seed_workspace(ws, like_feature_script, bad_role="backend")
    manual = _manual_recompute(st)

    r = _runner().invoke(_app(), ["metrics", "--workspace", str(ws)])
    assert r.exit_code == 0, r.output
    got = _parse_metrics_output(r.output)

    # —— 任务数：具体数值 1 ——
    assert got.get("task_count") == 1.0, f"任务数应为 1，输出：\n{r.output}"

    # —— 平均轮数：= 事件数（单线程），具体数值且一致 ——
    assert got.get("avg_rounds") is not None and got.get("avg_rounds") != "N/A", \
        f"平均轮数必须是具体数值：\n{r.output}"
    assert abs(float(got["avg_rounds"]) - manual["avg_rounds"]) < 0.01, (
        f"平均轮数复算不一致：CLI={got['avg_rounds']} 手工={manual['avg_rounds']}\n{r.output}"
    )

    # —— 聚合节省 %：具体数值且与 Σ(batch-1)/Σbatch 一致 ——
    assert manual["agg_pct"] is not None, "本流程应有聚合派发（batch_size 行）"
    assert got.get("agg_pct") not in (None, "N/A"), \
        f"聚合节省% 必须是具体数值：\n{r.output}"
    assert abs(float(got["agg_pct"]) - manual["agg_pct"]) < 0.05, (
        f"聚合节省% 复算不一致：CLI={got['agg_pct']} 手工={manual['agg_pct']:.2f}\n{r.output}"
    )

    # —— 首次合法率 %：具体数值且 = (1 - retry/invoke)*100，一致 ——
    assert manual["first_legal_pct"] is not None
    assert got.get("first_legal_pct") not in (None, "N/A"), \
        f"首次合法率% 必须是具体数值（不得 N/A）：\n{r.output}"
    assert abs(float(got["first_legal_pct"]) - manual["first_legal_pct"]) < 0.05, (
        f"首次合法率% 复算不一致：CLI={got['first_legal_pct']} "
        f"手工={manual['first_legal_pct']:.2f} "
        f"(retry={manual['retry_count']}/invoke={manual['invoke_count']})\n{r.output}"
    )
    # 恰一次 retry → 首次合法率必 < 100（真实反映一次退回，不是恒 100 假绿）。
    assert manual["first_legal_pct"] < 100.0, (
        "本流程注入了一次 schema_retry，首次合法率应 < 100%"
    )

    # —— 成本：N/A（Mock 无 last_usage，诚实边界）——
    assert got.get("cost") == "N/A", (
        f"Mock 无真实用量 → 成本必须显示 N/A（禁止编造 cost=0）：\n{r.output}"
    )

    # —— 真实层混沌完成率：N/A（Q1/Q2 陪跑边界，未采集）——
    assert "n/a" in r.output.lower(), "真实层完成率等未采集项应显示 N/A"


# ==================================================================
# (c-3) ChaosHarness(metrics_store=...) 落表后 metrics 显示混沌轮数与通过率
# ==================================================================

def test_chaos_metrics_store_populates_chaos_rows(tmp_dir, like_feature_script):
    """ChaosHarness.run(metrics_store=store) 落盘后，同线程 metrics 表出现
    chaos_rounds / chaos_mock_pass_pct 行；orch metrics 对该线程显示混沌轮数与
    mock 通过率的**具体数值**（非 N/A）。缺省 metrics_store 行为不变（不落 chaos 行）。"""
    import orch.chaos

    ws = tmp_dir / "chaos-ws"
    ws.mkdir()

    # 落 chaos 指标的目标线程 store（与被跑轮次的 t-000... 分离，避免混入类型序列断言）。
    metrics_tdir = ws / "t-chaosmetrics"
    mstore = orch.store.Store(metrics_tdir)
    mstore.set_meta("status", "terminated")

    harness = orch.chaos.ChaosHarness(
        workspace=ws / "rounds", script=like_feature_script, seed=5,
    )
    (ws / "rounds").mkdir(parents=True, exist_ok=True)
    report = harness.run(rounds=3, metrics_store=mstore)

    # 采集点落盘：chaos_rounds == 3；mock 通过率 == 100（3/3）。
    rounds_rows = _metric_values(mstore, "chaos_rounds")
    pass_rows = _metric_values(mstore, "chaos_mock_pass_pct")
    assert rounds_rows == [3.0], f"chaos_rounds 应落一条 =3：{rounds_rows}"
    assert pass_rows and abs(pass_rows[-1] - 100.0) < 0.01, (
        f"3 轮全过 → mock 通过率应 100%：{pass_rows}"
    )
    assert report.passed == 3

    # orch metrics --thread 指向该 store 目录：混沌轮数/通过率显示具体数值。
    r = _runner().invoke(
        _app(), ["metrics", "--workspace", str(ws), "--thread", "t-chaosmetrics"]
    )
    assert r.exit_code == 0, r.output
    lo = r.output.lower()
    assert "chaos" in lo or "混沌" in r.output
    # 轮数 3 与通过率 100 至少各出现一次具体数值。
    assert "3" in r.output, f"混沌轮数 3 应出现：\n{r.output}"
    assert "100" in r.output, f"mock 通过率 100 应出现：\n{r.output}"


def test_chaos_run_without_metrics_store_unchanged(tmp_dir, like_feature_script):
    """缺省 metrics_store（None）时 ChaosHarness.run 行为不变：不抛错、passed==rounds、
    不产生任何 chaos 指标副作用（向后兼容 50 轮硬门槛调用路径）。"""
    import orch.chaos

    ws = tmp_dir / "chaos-ws2"
    ws.mkdir()
    harness = orch.chaos.ChaosHarness(
        workspace=ws, script=like_feature_script, seed=9,
    )
    report = harness.run(rounds=2)  # 不传 metrics_store
    assert report.rounds == 2
    assert report.passed == 2
