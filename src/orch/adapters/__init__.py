"""适配层：统一 invoke 接口与 mock 后端（spec §7）。

分层铁律（spec §2）：适配层**禁止**包含任何角色逻辑，只做格式转换 / 查表 /
进程管理。本模块的 mock 后端因此只做两件事：按 (role, 事件号) 查预置脚本、
把结果规范化为**只含作者字段**的信封；并维护 exactly-once 校验用的 ledger 台账
（spec §7.4 / §9.4）。

冻结契约见 docs/m0-contract.md §3。
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

# 作者字段白名单（spec §3.1 / 附录A）：mock 返回的信封只允许这些键；
# 系统字段（id/thread_id/ts/from/re/meta）由编排器权威赋值，禁止模型自报（§3.1、§16.11）。
_AUTHOR_FIELDS = ("to", "type", "body", "artifacts", "corr", "blackboard_ops")


class Caps(TypedDict):
    """后端能力申报（spec §7.1 原样七字段）。"""

    context_window: int
    tools: list[str]
    write_scope: list[str]
    cost_tier: str
    supports_resume: bool
    timeout_s: int
    max_concurrent: int


# mock 后端的静态能力申报占位（§7.4 测试用；无上下文窗预算约束，无工具/写域）。
_MOCK_CAPS: Caps = {
    "context_window": 0,
    "tools": [],
    "write_scope": [],
    "cost_tier": "cheap",
    "supports_resume": False,
    "timeout_s": 0,
    "max_concurrent": 1,
}


class MockAdapter:
    """脚本化确定性 agent（spec §7.4）。

    按 (role, 事件号) 查 script 返回预置作者字段信封；每处理一个事件号向落盘
    ledger 追加一行 '{role}:{event_id}'，供混沌测试校验 exactly-once（§9.4）。
    适配层无角色逻辑：只查表 + 规范化输出格式。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        script: dict,
        ledger_path: str | Path,
        caps: Caps | None = None,
    ) -> None:
        # script: {触发事件号(int): 预置作者字段信封(dict)}，来自附录B fixture 切片。
        self.role = role
        self.script = script
        self.ledger_path = Path(ledger_path)
        self.caps = caps if caps is not None else dict(_MOCK_CAPS)  # type: ignore[assignment]

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """按 (role, 本批触发号) 返回预置作者字段信封，并追加一行 ledger。

        触发号 = view['event_ids'] 的最大值（= 聚合派发本批的触发事件号，契约 §3）。
        副作用：写 ledger 前自动创建父目录（parents=True, exist_ok=True，T1 裁决③），
        追加一行 '{role}:{event_id}\\n'。返回 (env_dict, sess)；sess 原样透传
        （mock 不产生新会话状态）。
        """
        event_id = max(view["event_ids"])

        # —— 查表：取该角色对该触发号的预置信封，规范化为只含作者字段的副本 —— #
        scripted = self.script[event_id]
        env = {k: scripted[k] for k in _AUTHOR_FIELDS if k in scripted}

        # —— 副作用：ledger 追加一行（exactly-once 校验依据，§9.4）—— #
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{self.role}:{event_id}\n")

        return env, sess
