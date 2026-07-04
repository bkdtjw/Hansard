"""适配层验收测试（spec §7.1、§7.4、§9.4）。

覆盖任务卡条目 (e)：
  - §7.4 MockAdapter 按 (role, 事件号) 返回预置作者字段信封；
    事件号取自 view['event_ids'] 的最大值（= 本批触发号）。
  - 每处理一个事件号，向 ledger 文件**追加一行** '{role}:{event_id}'。
  - §9.4：mock ledger 无重复事件号（exactly-once 校验的基础）。

硬约束：顶层只 import orch.adapters；符号在函数体内引用。
断言仅依赖契约 §3 公开签名（Caps / MockAdapter(role, script, ledger_path, caps) / invoke）。
"""

from __future__ import annotations

from pathlib import Path

import orch.adapters  # 包级导入

from tests.helpers import make_view, read_ledger_lines


def _mock(role, script, ledger_path):
    return orch.adapters.MockAdapter(role=role, script=script, ledger_path=ledger_path)


# ——————————————————————————————————————————————————————————————
# §7.4 返回预置作者字段信封（按 role + 最大事件号）
# ——————————————————————————————————————————————————————————————

def test_invoke_returns_scripted_envelope(role_script, tmp_dir):
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("moderator", role_script("moderator"), ledger)
    env, sess = ad.invoke(make_view("moderator", [1]), None)
    # 附录B E2：moderator 对 E1(兜底) 回 assign→pm。
    assert env["to"] == ["pm"]
    assert env["type"] == "assign"
    assert isinstance(env["body"], str) and env["body"]


def test_invoke_only_returns_author_fields(role_script, tmp_dir):
    # §3.1 / 契约 §3：mock 返回**只含作者字段**的信封；不得自报系统字段。
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("pm", role_script("pm"), ledger)
    env, _ = ad.invoke(make_view("pm", [2]), None)
    author_keys = {"to", "type", "body", "artifacts", "corr", "blackboard_ops"}
    system_keys = {"id", "thread_id", "ts", "from", "re", "meta"}
    assert set(env.keys()) <= author_keys, f"信封含非作者字段: {set(env.keys()) - author_keys}"
    assert not (set(env.keys()) & system_keys), "mock 不得自报系统字段 from/re/id/ts/meta"


def test_invoke_uses_max_event_id_as_trigger(role_script, tmp_dir):
    # 契约 §3：聚合批次触发号 = event_ids 最大值。
    # pm 对 E4+E5 同批（触发号取 5）→ decision→moderator，冻结 v2。
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("pm", role_script("pm"), ledger)
    env, _ = ad.invoke(make_view("pm", [4, 5]), None)
    assert env["type"] == "decision"
    assert env["to"] == ["moderator"]
    ops = env.get("blackboard_ops") or []
    assert any(o.get("op") == "freeze_contract" and o.get("version") == 2 for o in ops)


def test_invoke_returns_session_passthrough(role_script, tmp_dir):
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("backend", role_script("backend"), ledger)
    sentinel = {"sid": "s-xyz"}
    _env, sess = ad.invoke(make_view("backend", [3]), sentinel)
    # 返回 (env, sess)；sess 透传（mock 不产生新会话状态）。
    assert sess == sentinel or sess is None


def test_caps_present_and_typed(role_script, tmp_dir):
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("moderator", role_script("moderator"), ledger)
    caps = ad.caps
    # §7.1 Caps 字段齐全。
    for k in ["context_window", "tools", "write_scope", "cost_tier",
              "supports_resume", "timeout_s", "max_concurrent"]:
        assert k in caps, f"Caps 缺字段 {k}"


# ——————————————————————————————————————————————————————————————
# §7.4 ledger：每处理一个事件号追加一行 '{role}:{event_id}'
# ——————————————————————————————————————————————————————————————

def test_ledger_appends_one_line_per_event(role_script, tmp_dir):
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("backend", role_script("backend"), ledger)
    ad.invoke(make_view("backend", [3]), None)
    ad.invoke(make_view("backend", [7]), None)
    lines = read_ledger_lines(ledger)
    assert lines == ["backend:3", "backend:7"]


def test_ledger_line_format(role_script, tmp_dir):
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("moderator", role_script("moderator"), ledger)
    ad.invoke(make_view("moderator", [1]), None)
    lines = read_ledger_lines(ledger)
    assert lines == ["moderator:1"]


def test_ledger_aggregated_batch_records_trigger_id(role_script, tmp_dir):
    # 聚合批 [4,5] 只处理一个事件号（最大值 5）→ ledger 记一行 pm:5。
    ledger = tmp_dir / "ledger.txt"
    ad = _mock("pm", role_script("pm"), ledger)
    ad.invoke(make_view("pm", [4, 5]), None)
    assert read_ledger_lines(ledger) == ["pm:5"]


def test_ledger_shared_across_roles_no_duplicate_event_ids(like_feature_script, tmp_dir):
    """§9.4：整条脚本跑一遍，共享 ledger 内**事件号无重复**（exactly-once 基础）。

    这里手工驱动"每个触发号只 invoke 一次"，模拟不中断基准；ledger 每行唯一。
    """
    ledger = tmp_dir / "ledger.txt"
    adapters = {
        role: _mock(role, like_feature_script.get(role, {}), ledger)
        for role in like_feature_script
    }
    # 按附录B 顺序，每个回复由对应角色针对其触发号 invoke 一次（与 E2E 实跑的 16 次 invoke 对齐）。
    # 注：E8(backend→tester handoff)+E9(frontend→tester report) 同批聚合 → tester 触发号 max=9
    #     （见 fixture 抬头对齐(2)）；故 tester 首次驱动为 [8,9]，ledger 记 tester:9。
    drive = [
        ("moderator", [1]), ("pm", [2]), ("backend", [3]), ("frontend", [3]),
        ("pm", [4, 5]), ("moderator", [6]), ("backend", [7]), ("frontend", [7]),
        ("tester", [8, 9]), ("backend", [10]), ("tester", [11]), ("moderator", [12]),
        ("moderator", [15]), ("frontend", [16]), ("tester", [17]), ("moderator", [18]),
    ]
    for role, eids in drive:
        adapters[role].invoke(make_view(role, eids), None)

    lines = read_ledger_lines(ledger)
    # exactly-once：无任何一行重复。
    assert len(lines) == len(set(lines)), f"ledger 出现重复事件号: {lines}"


def test_ledger_persists_to_disk_file(role_script, tmp_dir):
    # ledger 是**落盘**文件（供跨进程/崩溃后的 exactly-once 校验）。
    # tester 首次被触发是 E8(handoff)+E9(report) 同批聚合 → 触发号 max=9（见 fixture 抬头对齐(2)）。
    ledger = tmp_dir / "sub" / "ledger.txt"
    ad = _mock("tester", role_script("tester"), ledger)
    ad.invoke(make_view("tester", [8, 9]), None)
    assert Path(ledger).exists()
    assert Path(ledger).read_text(encoding="utf-8").strip() == "tester:9"
