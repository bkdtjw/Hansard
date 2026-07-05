"""附录B 期望事件序列常量（orch.chaos 自有，审计 G 解耦）。

审计 G（docs/audit-20260705.md §二）：产品包 `orch.chaos` 曾从 `tests.helpers`
反向导入 `EXPECTED_TYPE_SEQUENCE`，导致离开项目根 `import orch.chaos` 即
`ModuleNotFoundError`（src 不得依赖 tests）。本文件把该常量落为 chaos 自有事实源；
`tests/helpers.py` 反向从此处 import 以保持既有测试兼容。

序列语义（spec 附录B 行633-660）：类型层面一致；顺序与类型必须一致，事件号允许
因实现细节偏移。(from, to) 为可读文档，不参与断言（断言只用类型序列）。

相对附录B 行637-658 原始清单的两处 spec 对齐（详见 tests/fixtures/like_feature.yaml
抬头）：
  · E9 的 to 记为 [tester]（附录B 记 moderator）：report 经 moderator 会被强制回一条
    事件、打断类型序列（§5.1 每个派发目标必被 invoke）；改走与 E8 同目标聚合，类型/
    序位不变。
  · 末尾追加 E20 system：§5.4 规定 terminate 触发"终止清单 system 总结事件"；附录B
    行657 在 E19 注解里点明该总结事件，却未编入 E1-E19 清单。落盘真相含此事件（id=20），
    故期望序列如实补上，保持与 spec §5.4 一致（忠实实现的必然产物，非测试凑绿）。
"""

from __future__ import annotations

# (from, to, type) 三元组。断言只用类型；(from, to) 为可读文档。
EXPECTED_SEQUENCE: list[tuple[str, list[str], str]] = [
    ("human", [], "assign"),                            # E1
    ("moderator", ["pm"], "assign"),                    # E2 兜底路由
    ("pm", ["backend", "frontend"], "review"),          # E3 PRD/评审（v1 不入黑板，§3.3）
    ("backend", ["pm"], "question"),                    # E4  ── 同批聚合
    ("frontend", ["pm"], "answer"),                     # E5  ──
    ("pm", ["moderator"], "decision"),                  # E6 freeze v2 + set_task done
    ("moderator", ["backend", "frontend"], "assign"),   # E7 并行
    ("backend", ["tester"], "handoff"),                 # E8  ── 同批聚合(→tester)
    ("frontend", ["tester"], "report"),                 # E9  ──（附录B 记 moderator，见抬头）
    ("tester", ["backend"], "defect"),                  # E10 环路计数 1
    ("backend", ["tester"], "handoff"),                 # E11
    ("tester", ["moderator"], "acceptance"),            # E12 verify.exit_code=0
    ("moderator", ["human"], "gate_request"),           # E13 gate_wait + suspended
    ("human", ["moderator"], "gate_decision"),          # E14 approve
    ("system", ["moderator"], "system"),                # E15 CI 回调（run_ci 系统执行器）
    ("moderator", ["frontend"], "assign"),              # E16
    ("frontend", ["tester"], "handoff"),                # E17
    ("tester", ["moderator"], "acceptance"),            # E18
    ("moderator", [], "terminate"),                     # E19 终止清单（不生成派发行）
    ("system", ["moderator"], "system"),                # E20 §5.4 终止清单 system 总结事件
]

# 期望的事件类型序列（仅类型，最宽松的一致性判据）。
EXPECTED_TYPE_SEQUENCE: list[str] = [t for (_frm, _to, t) in EXPECTED_SEQUENCE]

__all__ = ["EXPECTED_SEQUENCE", "EXPECTED_TYPE_SEQUENCE"]
