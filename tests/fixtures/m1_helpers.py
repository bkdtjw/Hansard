"""M1 验收测试的**纯**辅助工具与常量（tests/fixtures/m1_helpers.py）。

只放不依赖被测实现的普通函数/数据，供 tests/test_render.py / test_watchdog.py /
test_terminate_m1.py 复用。**不实现、不占位、不 mock 任何被测逻辑**——被测符号一律在
各 test 函数体内引用（顶层只 import orch.render / orch.scheduler 包）。

配置结构严格遵循 docs/m1-contract.md §5（与 spec §11.1 子集一致）：
  config = {
    thread_defaults: {max_rounds, loop_limit, chat_ttl},
    gate_ops: {...},
    adapters: {<name>: {kind, context_window, timeout_s, ...}},   # render 预算取 context_window
    roles:    {<role>: {adapter, can_decide, write_scope, tools, prompt}},
  }
render 读 config.roles[role].prompt 文件内容组系统层；
预算上限取 config.adapters[roles[role].adapter].context_window。
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_DIR = Path(__file__).parent
M1_PROMPTS_DIR = FIXTURE_DIR / "m1_prompts"

# 各角色提示词夹具内嵌的可逐字断言标记（tests/fixtures/m1_prompts/*.md）。
PROMPT_MARKER = {
    "backend": "M1_BACKEND_PROMPT_MARKER",
    "tester": "M1_TESTER_PROMPT_MARKER",
}


def prompt_path(role: str) -> str:
    """返回该角色 M1 提示词夹具文件的绝对路径字符串（供 config.roles[role].prompt）。"""
    return str(M1_PROMPTS_DIR / f"{role}.md")


def m1_config(
    *,
    context_window: int = 100_000,
    max_rounds: int = 100,
    loop_limit: int = 3,
    chat_ttl: int = 10,
    roles: dict | None = None,
) -> dict:
    """构造 M1 config（docs/m1-contract.md §5 结构）。

    默认声明 backend / tester 两个角色，均绑定 mock adapter（context_window 可调，
    供 §6.3 预算压缩测试构造超窗场景）。roles 传入可覆盖默认角色表。
    """
    default_roles = {
        "backend": {
            "adapter": "mock",
            "can_decide": False,
            "write_scope": ["server/"],
            "tools": ["Edit", "Write", "Bash(pytest:*)"],
            "prompt": prompt_path("backend"),
        },
        "tester": {
            "adapter": "mock",
            "can_decide": False,
            "write_scope": ["tests/", "reports/"],
            "tools": ["Edit", "Write", "Bash(pytest:*)"],
            "prompt": prompt_path("tester"),
        },
    }
    return {
        "thread_defaults": {
            "max_rounds": max_rounds,
            "loop_limit": loop_limit,
            "chat_ttl": chat_ttl,
        },
        "gate_ops": {},
        "adapters": {
            "mock": {
                "kind": "mock",
                "context_window": context_window,
                "timeout_s": 600,
            },
        },
        "roles": roles if roles is not None else default_roles,
    }


def seed_events(store, specs: list[dict]) -> list[int]:
    """按顺序把一串事件写入 store，返回落盘事件号列表（与 specs 等长、升序）。

    每个 spec 是 store.append_event 的 kwargs 子集，至少含 sender/type/body；
    可选 to/re/corr/artifacts/blackboard_ops/meta。纯落盘辅助，不驱动调度。
    """
    ids: list[int] = []
    for s in specs:
        ids.append(
            store.append_event(
                sender=s["sender"],
                type=s["type"],
                body=s.get("body", "x"),
                to=s.get("to"),
                re=s.get("re"),
                corr=s.get("corr"),
                artifacts=s.get("artifacts"),
                blackboard_ops=s.get("blackboard_ops"),
                meta=s.get("meta"),
            )
        )
    return ids
