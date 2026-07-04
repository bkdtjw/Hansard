"""M0 验收测试的**纯**辅助工具与常量（tests/helpers.py）。

只放不依赖 pytest 的普通函数与数据（可被各 test 模块 `from tests.helpers import …`）。
pytest 夹具（thread_dir / like_feature_script / role_script）留在 conftest.py，由 pytest 自动注入。

不实现、不占位、不 mock 任何被测逻辑——被测符号一律在各 test 函数体内引用。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

FIXTURE_DIR = Path(__file__).parent / "fixtures"
LIKE_FEATURE_YAML = FIXTURE_DIR / "like_feature.yaml"


# —— 附录B 期望事件序列（类型层面；顺序与类型必须一致，事件号允许偏移）——
# (from, to, type) 三元组，供 E2E 断言事件类型序列。
EXPECTED_SEQUENCE = [
    ("human", [], "assign"),                            # E1
    ("moderator", ["pm"], "assign"),                    # E2 兜底路由
    ("pm", ["backend", "frontend"], "review"),          # E3 freeze v1
    ("backend", ["pm"], "question"),                    # E4  ── 同批聚合
    ("frontend", ["pm"], "answer"),                     # E5  ──
    ("pm", ["moderator"], "decision"),                  # E6 freeze v2
    ("moderator", ["backend", "frontend"], "assign"),   # E7 并行
    ("backend", ["tester"], "handoff"),                 # E8
    ("frontend", ["moderator"], "report"),              # E9
    ("tester", ["backend"], "defect"),                  # E10 环路计数 1
    ("backend", ["tester"], "handoff"),                 # E11
    ("tester", ["moderator"], "acceptance"),            # E12 verify.exit_code=0
    ("moderator", ["human"], "gate_request"),           # E13 gate_wait + suspended
    ("human", ["moderator"], "gate_decision"),          # E14 approve
    ("system", ["moderator"], "system"),                # E15 CI 回调
    ("moderator", ["frontend"], "assign"),              # E16
    ("frontend", ["tester"], "handoff"),                # E17
    ("tester", ["moderator"], "acceptance"),            # E18
    ("moderator", [], "terminate"),                     # E19 终止清单
]

# 期望的事件类型序列（仅类型，最宽松的一致性判据）。
EXPECTED_TYPE_SEQUENCE = [t for (_frm, _to, t) in EXPECTED_SEQUENCE]


def load_like_feature_script() -> dict:
    """加载 like_feature.yaml 的完整 {role: {event_id: env}} 脚本（事件号转 int）。"""
    raw = yaml.safe_load(LIKE_FEATURE_YAML.read_text(encoding="utf-8"))
    script: dict[str, dict] = {}
    for role, table in (raw or {}).items():
        script[role] = {int(k): v for k, v in (table or {}).items()}
    return script


def make_view(role: str, event_ids: list[int], events=None, board: str = ""):
    """构造 M0 最小占位 view（契约 §3）：{role, event_ids, events, board}。

    mock 只用 role + event_ids；其余字段占位即可，供适配层最小接口对接。
    """
    return {
        "role": role,
        "event_ids": list(event_ids),
        "events": list(events or []),
        "board": board,
    }


def read_ledger_lines(ledger_path) -> list[str]:
    """读取 mock ledger 的非空行（'{role}:{event_id}'）。文件不存在返回 []。"""
    p = Path(ledger_path)
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def read_state_json(thread_dir) -> dict:
    """直接读黑板 state.json（旁路 board_state，用于交叉核对）。"""
    p = Path(thread_dir) / "blackboard" / "state.json"
    return json.loads(p.read_text(encoding="utf-8"))
