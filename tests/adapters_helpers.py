"""测试专用适配器桩（tests/adapters_helpers.py）——只在测试层扩展 Fake* 适配器行为。

背景：M2 契约 §2 描述了 FakeCliAdapter/FakeApiAdapter 的两类扩展需求：
  1) 多回复脚本（call_no -> envelope）——供 E2E 多轮控制流断言（`scripted_replies`）。
  2) 越权注入（inject_side_effect(worktree)）——供 §8.2 审计端到端验证。

src 层 FakeCliAdapter/FakeApiAdapter 当前只支持单次 `scripted_output`/`scripted_reply`。
本文件在**测试层**提供薄包装适配器，行为等价于"call_no 匹配的信封串行返回"，
以驱动 tests/test_m2_e2e.py 与 tests/test_permissions.py 走通调度层控制流。

铁律（M2 T5 任务卡红线）：
  · 本模块只在 tests/ 内部使用；不修改 src、不改 spec、不弱化被测断言。
  · 仅暴露 invoke(view, sess) 与 caps 属性（core.py 只依赖此两点）；
    路由（作者字段 → 落盘信封）仍走 orch.scheduler.core 的权威赋值（§16.11）。
  · 越权注入回调在 invoke 早段执行，模拟"CLI 子进程刚跑完就写文件"的时序——
    调度环随后走 §8.2 audit_write_scope 审计（越权 → reset+system 事件转 moderator）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# 作者字段白名单（与 src/orch/adapters/__init__.py 中的 _AUTHOR_FIELDS 一致；§3.1）。
# 独立列于本文件避免对私有符号的深耦合（内部实现细节允许变化，测试双只用作者字段白名单）。
_AUTHOR_FIELDS = ("to", "type", "body", "artifacts", "corr", "blackboard_ops")


def _strip_to_author_fields(raw: dict) -> dict:
    """§3.1/§7.6：只保留作者字段（系统字段由编排器赋值）。"""
    return {k: raw[k] for k in _AUTHOR_FIELDS if k in raw}


def _default_caps(*, supports_resume: bool) -> dict:
    """契约 §2/§7.1：Caps 七字段最小占位（core.py 不读，测试也不断言此值）。"""
    return {
        "context_window": 0,
        "tools": [],
        "write_scope": [],
        "cost_tier": "cheap",
        "supports_resume": supports_resume,
        "timeout_s": 0,
        "max_concurrent": 1,
    }


class MultiReplyFakeCliAdapter:
    """CLI 型适配器测试双——支持 `scripted_replies={call_no: env}` 与 inject_side_effect。

    每次 invoke 递增 call_no（从 1 起），取 scripted_replies[call_no] 作为作者字段信封。
    inject_side_effect(worktree) 在返回信封前执行——用于模拟子进程写入 worktree 的时序。
    """

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        worktree: Path,
        scripted_replies: dict[int, dict],
        inject_side_effect: Callable[[Path], None] | None = None,
        caps: dict | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.worktree = Path(worktree)
        self.scripted_replies = dict(scripted_replies)
        self._inject_side_effect = inject_side_effect
        self.caps = caps if caps is not None else _default_caps(supports_resume=True)
        # —— 测试可观测点 —— #
        self.call_no: int = 0
        self.last_cwd: str | None = None
        self.attempts: int = 0
        self.gen: int = 0

    def invoke(self, view: dict, sess: dict | None) -> tuple[dict, dict | None]:
        """按 call_no 顺序返回预置作者字段信封；每次调用前触发 inject_side_effect。"""
        self.call_no += 1
        self.attempts += 1
        self.last_cwd = str(self.worktree)

        # 越权/合规注入：调度环随后走 §8.2 审计（M2 契约 §2）。
        if self._inject_side_effect is not None:
            self._inject_side_effect(self.worktree)

        if self.call_no not in self.scripted_replies:
            raise KeyError(
                f"MultiReplyFakeCliAdapter[{self.role}] no scripted reply for call_no={self.call_no}"
            )
        env = _strip_to_author_fields(self.scripted_replies[self.call_no])
        prev_gen = int((sess or {}).get("gen", 0))
        self.gen = prev_gen + 1
        # session_id 提取（M2 契约 §2）：脚本可携带 session_id/sid/session 字段。
        raw = self.scripted_replies[self.call_no]
        sid = None
        for f in ("session_id", "sid", "session"):
            v = raw.get(f)
            if isinstance(v, str) and v:
                sid = v
                break
        new_sess = {"sid": sid, "gen": self.gen}
        return env, new_sess


class MultiReplyFakeApiAdapter:
    """API 型适配器测试双——支持 `scripted_replies={call_no: env}`。

    §7.3：无会话；supports_resume=False；每次全量组装；返回 sess=None。
    """

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        scripted_replies: dict[int, dict],
        caps: dict | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.scripted_replies = dict(scripted_replies)
        self.caps = caps if caps is not None else _default_caps(supports_resume=False)
        # 本项目 API 型角色（moderator）不配工具（§7.3）。
        if "tools" not in config:
            self.caps["tools"] = []
        # —— 测试可观测点 —— #
        self.call_no: int = 0
        self.last_view_text: str | None = None
        self.step_count: int = 0

    def invoke(self, view: dict, sess: dict | None) -> tuple[dict, dict | None]:
        """按 call_no 顺序返回预置作者字段信封；忽略 sess，返回 sess=None（§7.3）。"""
        self.call_no += 1
        self.step_count += 1
        self.last_view_text = str(view.get("text", "")) if isinstance(view, dict) else ""

        if self.call_no not in self.scripted_replies:
            raise KeyError(
                f"MultiReplyFakeApiAdapter[{self.role}] no scripted reply for call_no={self.call_no}"
            )
        env = _strip_to_author_fields(self.scripted_replies[self.call_no])
        return env, None
