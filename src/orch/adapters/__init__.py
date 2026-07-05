"""适配层：统一 invoke 接口与后端（spec §7）。

分层铁律（spec §2）：适配层**禁止**包含任何角色逻辑，只做格式转换 / 查表 /
进程管理。本模块的 mock/CLI/API 三类后端因此各自只做与自身机制相关的最少工作，
并把结果规范化为**只含作者字段**的信封；系统字段（id/thread_id/ts/from/re/meta）
由编排器权威赋值，禁止模型/后端自报（§3.1、§16.11）。

M0 冻结契约见 docs/m0-contract.md §3；M2 追加见 docs/m2-contract.md §2。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, TypedDict

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


# ======================================================================
# M2 追加：CliAdapter / ApiAdapter / FakeCliAdapter / FakeApiAdapter
# ======================================================================

# CLI 输出中 ```json 代码块的正则（跨行，非贪婪）；spec §7.2/§17：取最后一个。
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)

# session_id 提取的默认 JSON 字段名优先级（§17 开放决策：常见字段兜底）。
_DEFAULT_SID_FIELDS = ("session_id", "sid", "session")


def _extract_last_json_block(stdout: str) -> str | None:
    """取标准输出中**最后一个** ```json 块的原文（不含围栏）。

    spec §7.2/§17：CLI 输出可能夹带过程稿，只有最后一个 json 块才是作品。
    无块返回 None。
    """
    matches = _JSON_BLOCK_RE.findall(stdout)
    if not matches:
        return None
    return matches[-1]


def _extract_sid(parsed_env: dict, stdout: str, config: dict) -> str | None:
    """M2 session_id 提取策略（§17 开放决策 + M2 契约 §2）：

    1. 从解析出的 JSON 信封中依次查 config.session_id_fields（默认
       ('session_id', 'sid', 'session')）；命中即返回。
    2. 未命中时，若 config.session_id_extract 提供正则，则对整段 stdout 应用；
       捕获组 1 作为 sid（无捕获组则用 group(0)）。
    3. 均未命中 → None（调用方负责 gen+=1）。

    该函数在适配层内是**格式转换**（§7.6），不含任何角色逻辑。
    """
    fields = config.get("session_id_fields", _DEFAULT_SID_FIELDS)
    for f in fields:
        v = parsed_env.get(f)
        if isinstance(v, str) and v:
            return v
    pattern = config.get("session_id_extract")
    if pattern:
        m = re.search(pattern, stdout)
        if m:
            try:
                return m.group(1)
            except IndexError:
                return m.group(0)
    return None


def _strip_to_author_fields(raw: dict) -> dict:
    """§3.1/§7.6：只保留作者字段。系统字段由编排器权威赋值，禁止后端自报（§16.11）。"""
    return {k: raw[k] for k in _AUTHOR_FIELDS if k in raw}


def _caps_from_config(config: dict, *, supports_resume: bool) -> Caps:
    """从 config（§11.1 子集）派生 Caps，缺省字段用最小合理默认。适配层不判角色语义。"""
    return {  # type: ignore[return-value]
        "context_window": int(config.get("context_window", 0)),
        "tools": list(config.get("tools", [])),
        "write_scope": list(config.get("write_scope", [])),
        "cost_tier": str(config.get("cost_tier", "mid")),
        "supports_resume": bool(supports_resume),
        "timeout_s": int(config.get("timeout_s", 0)),
        "max_concurrent": int(config.get("max_concurrent", 1)),
    }


class CliAdapter:
    """CLI 型适配器骨架（spec §7.2）。

    子进程冷启动，cwd=角色 worktree；权限经 CLI 参数注入（§8.1）；
    从 stdout 取最后一个 ```json 块解析为作者字段信封（§7.2/§17）；
    超时 kill；提取 session_id（默认从 session_id/sid/session 字段任一，
    否则用 config.session_id_extract 正则兜底）。

    M2 只做冷启动路径；resume_cmd 保留但不调用（M3）。
    真实 CLI 的 flag/session_id 正则以 `--help` 实测为准（QUESTIONS.md Q1/Q2 陪跑）。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        worktree: Path,
        caps: Caps | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.worktree = Path(worktree)
        self.caps = caps if caps is not None else _caps_from_config(
            config, supports_resume=True
        )

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """冷启动路径：`start_cmd + view['text']` → 解析最后一个 json 块。

        - cwd=self.worktree；超时按 config.timeout_s 触发 kill 并抛 TimeoutError。
        - 无 json 块或 JSON 解析失败 → ValueError（调度层按 §5.1 重调）。
        - 返回 (env_dict, {"sid":..., "gen": gen+1})；无 sid 时 sess 仍带 gen（gen+1）。
        """
        start_cmd = str(self.config["start_cmd"])
        cmd = start_cmd.split() + [str(view["text"])]
        timeout_s = int(self.config.get("timeout_s", self.caps.get("timeout_s", 0)) or 0)
        proc = subprocess.Popen(  # noqa: S603 — 冷启动子进程是 §7.2 明列职责
            cmd,
            cwd=str(self.worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, _stderr = proc.communicate(timeout=timeout_s or None)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise TimeoutError(
                f"CliAdapter[{self.role}] timed out after {timeout_s}s"
            )

        block = _extract_last_json_block(stdout or "")
        if block is None:
            raise ValueError(
                f"CliAdapter[{self.role}] no ```json block in stdout"
            )
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"CliAdapter[{self.role}] JSON decode failed: {e}"
            ) from e

        env = _strip_to_author_fields(parsed)
        sid = _extract_sid(parsed, stdout or "", self.config)
        prev_gen = int((sess or {}).get("gen", 0))
        new_sess: dict | None = {"sid": sid, "gen": prev_gen + 1}
        return env, new_sess


class ApiAdapter:
    """API 型适配器（spec §7.3）。

    单步；无会话；supports_resume=False；每次**全量组装**（不复用旧会话）。
    本项目 API 型角色（moderator）不配工具，保持单步。

    M2 边界（任务卡红线）：不做真实网络调用；接受可注入 `message_fn(view, config)`
    骨架，默认实现在真实联网前抛 NotImplementedError（M2 不启用；由 FakeApiAdapter
    在测试路径下取代默认 fn）。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        caps: Caps | None = None,
        message_fn: Callable[[dict, dict], dict] | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.caps = caps if caps is not None else _caps_from_config(
            config, supports_resume=False
        )
        # §7.3 硬性：API 型 supports_resume 恒为 False，即使 caps 参数被外部误传。
        self.caps["supports_resume"] = False
        # 本项目 API 型角色（moderator）不配工具（§7.3）；仅 config 未显式列 tools 时兜底。
        if "tools" not in config:
            self.caps["tools"] = []
        self._message_fn = message_fn

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """单步：发送全量 view['text'] → 假/真 messages → 归一化为作者字段信封。

        - 忽略 sess（§7.3 无会话概念），返回 sess=None（永远全量组装）。
        - 未注入 message_fn 时（真实网络路径），M2 不启用 → NotImplementedError。
          测试请用 FakeApiAdapter 或注入 message_fn。
        """
        if self._message_fn is None:
            raise NotImplementedError(
                "ApiAdapter 真实网络路径未启用（M2 边界）；"
                "测试请用 FakeApiAdapter 或注入 message_fn。"
            )
        raw = self._message_fn(view, self.config)
        env = _strip_to_author_fields(raw)
        return env, None


class FakeCliAdapter:
    """CliAdapter 的测试双（M2 契约 §2）。

    对外行为等价于 CliAdapter，但不启动真实子进程：
      - scripted_output：假子进程 stdout（供解析最后一个 json 块）。
      - simulate_timeout=True：模拟超时 → kill + attempts+1 + TimeoutError。
      - last_cwd / attempts / killed / gen：暴露给测试断言的可观测点。

    session_id 提取策略与 CliAdapter 一致（§17）。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        worktree: Path,
        scripted_output: str = "",
        simulate_timeout: bool = False,
        caps: Caps | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.worktree = Path(worktree)
        self.scripted_output = scripted_output
        self.simulate_timeout = simulate_timeout
        self.caps = caps if caps is not None else _caps_from_config(
            config, supports_resume=True
        )
        # —— 测试可观测点 —— #
        self.last_cwd: str | None = None
        self.last_view_text: str | None = None
        self.attempts: int = 0
        self.killed: bool = False
        self.gen: int = 0

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """假子进程冷启动：不真启动进程，直接使用 scripted_output 走同样的解析路径。"""
        self.attempts += 1
        self.last_cwd = str(self.worktree)
        self.last_view_text = str(view.get("text", ""))

        if self.simulate_timeout:
            # 模拟 kill 语义（§5.3/§7.2）：不真 sleep，直接抛超时（测试语义等价）。
            self.killed = True
            timeout_s = int(self.config.get("timeout_s", 0))
            raise TimeoutError(
                f"FakeCliAdapter[{self.role}] simulated timeout after {timeout_s}s"
            )

        stdout = self.scripted_output
        block = _extract_last_json_block(stdout)
        if block is None:
            raise ValueError(
                f"FakeCliAdapter[{self.role}] no ```json block in stdout"
            )
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"FakeCliAdapter[{self.role}] JSON decode failed: {e}"
            ) from e

        env = _strip_to_author_fields(parsed)
        sid = _extract_sid(parsed, stdout, self.config)
        prev_gen = int((sess or {}).get("gen", 0))
        self.gen = prev_gen + 1
        new_sess: dict | None = {"sid": sid, "gen": self.gen}
        return env, new_sess


class FakeApiAdapter:
    """ApiAdapter 的测试双（M2 契约 §2）。

    直接使用可注入 scripted_reply（模拟 messages 返回的 dict），
    走与 ApiAdapter 一致的归一化路径（§3.1/§7.6：只留作者字段）。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        scripted_reply: dict,
        caps: Caps | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.scripted_reply = dict(scripted_reply)
        self.caps = caps if caps is not None else _caps_from_config(
            config, supports_resume=False
        )
        # §7.3 硬性：API 型 supports_resume=False；tools 默认空（moderator 无工具）。
        self.caps["supports_resume"] = False
        if "tools" not in config:
            self.caps["tools"] = []
        # —— 测试可观测点 —— #
        self.last_view_text: str | None = None
        self.step_count: int = 0

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """§7.3：单步；忽略入参 sess；返回 sess=None（每次全量组装，不复用会话）。"""
        # 观察点：每次都记录 view.text 全量（不做增量差分）。
        self.last_view_text = str(view.get("text", ""))
        self.step_count += 1
        env = _strip_to_author_fields(self.scripted_reply)
        return env, None
