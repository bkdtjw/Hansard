"""适配层：统一 invoke 接口与后端（spec §7）。

分层铁律（spec §2）：适配层**禁止**包含任何角色逻辑，只做格式转换 / 查表 /
进程管理。本模块的 mock/CLI/API 三类后端因此各自只做与自身机制相关的最少工作，
并把结果规范化为**只含作者字段**的信封；系统字段（id/thread_id/ts/from/re/meta）
由编排器权威赋值，禁止模型/后端自报（§3.1、§16.11）。

M0 冻结契约见 docs/m0-contract.md §3；M2 追加见 docs/m2-contract.md §2；
M5 错误分类见 docs/m5-contract.md §2（§7.6 输出规范化职责扩展）。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, TypedDict

from .state import DEFAULT_UNAVAILABLE_PATTERNS

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


# ======================================================================
# M5：额度类失败与传输级失败的分类（spec §7.6 末段 / §5.6.3 第 1 条）
#
# §7.6："invoke 的错误报告**必须**区分传输级失败与额度类失败（依 unavailable_patterns
# 识别，识别责任在适配层）；调度器只消费分类结果，**禁止**在调度层散布各家报错文案的
# 字符串匹配。"——因此本节的子串匹配是**唯一**允许出现该匹配的地方。
#
# 边界（§5.6.3）：只有**传输级失败**（超时 / 进程失败 / 无输出）才进分类；
# schema 层非法信封（json 块能取到但内容非法）是输出质量问题，不是可用性问题，
# 一律维持 §5.1 原地重调路径，既有异常逐字不变。
# ======================================================================


class AdapterUnavailableError(Exception):
    """额度类失败（契约 §2）：该 adapter 当前不可用，应由调度层跳闸 + 降级路由。

    - ``adapter_name``：触发的 adapter **配置名**（roles[role].adapter 的键名；
      构造时未知则用角色名兜底，与 ``state.resolve_effective_adapter`` 同一约定）。
      调度层记账时仍应以自身解析出的生效绑定名为准，本字段是审计线索。
    - ``detail``：命中的原始报错摘要（跳闸审计事件 body/meta.detail 的素材，契约 §4）。
    """

    def __init__(self, adapter_name: str, detail: str = "") -> None:
        self.adapter_name = str(adapter_name)
        self.detail = str(detail)
        super().__init__(
            f"适配器 {self.adapter_name!r} 不可用（额度类，§5.6.3）：{self.detail}"
        )


def _as_text(value: object) -> str:
    """把 stderr/stdout（可能是 None / bytes / str）统一成 str，不可解码则替换。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _summarize(text: str, limit: int = 200) -> str:
    """多行报错压成一行摘要（供审计事件展示）；只做空白折叠与截断，不解读语义。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _unavailable_patterns(config: dict) -> tuple[str, ...]:
    """该 adapter 的特征清单：config.unavailable_patterns（§11.1）缺省用契约 §1 常量。

    显式给出的列表**照单全收**（空列表 = 明示关闭特征分类）；键缺失或类型不合
    （非 list/tuple）→ 回落默认清单，装载期校验属 §11.1 调用方职责。
    """
    raw = (config or {}).get("unavailable_patterns")
    if isinstance(raw, (list, tuple)):
        return tuple(str(p) for p in raw if str(p))
    return DEFAULT_UNAVAILABLE_PATTERNS


def _classify_unavailable(config: dict, *texts: object) -> str | None:
    """传输级报错文本命中特征 → 返回 detail 摘要；未命中 / 无文本 → None。

    匹配口径（§5.6.3 第 1 条 + §17 裁决）：**大小写不敏感子串**。
    输入边界（§5.6.3 第 1 条字面列举）：只接受 stderr / 进程退出信息 / 错误文本
    （异常消息）三类；**禁止**传 stdout 正文——正常输出里的十六进制串
    （sessionId/UUID）会撞上 '429' 这类子串清单（code-ws 误跳闸实证，
    ts=1785037196：UUIDv7 尾 "…0758bd76e429" 被记"命中特征 '429'"）。
    "无文本"（如超时且管道空）→ 不分类，走既有失败路径（契约 §2："未命中 → 既有
    失败路径不变"，无文本自然也无从命中）。
    """
    haystack = "\n".join(t for t in (_as_text(x) for x in texts) if t)
    if not haystack.strip():
        return None
    lowered = haystack.lower()
    for pattern in _unavailable_patterns(config):
        if pattern.lower() in lowered:
            return f"命中特征 {pattern!r}：{_summarize(haystack)}"
    return None


def _exit_info(proc: object) -> str:
    """子进程"退出信息"文本（§5.6.3 列举的三类报错文本之一）；正常退出返回空串。"""
    rc = getattr(proc, "returncode", None)
    if rc is None or rc == 0:
        return ""
    return f"exit code {rc}"


def _drain_after_kill(proc: object) -> tuple[str, str]:
    """kill 后排空管道（既有行为，调用次数不变）；仅 stderr 侧交给分类器。"""
    drained = proc.communicate()  # type: ignore[attr-defined]
    if isinstance(drained, tuple) and len(drained) == 2:
        return _as_text(drained[0]), _as_text(drained[1])
    return "", ""


class MockAdapter:
    """脚本化确定性 agent（spec §7.4）。

    按 (role, 事件号) 查 script 返回预置作者字段信封；每处理一个事件号向落盘
    ledger 追加一行 '{role}:{event_id}'，供混沌测试校验 exactly-once（§9.4）。
    适配层无角色逻辑：只查表 + 规范化输出格式。

    M5 追加（契约 §2）——两个可选开关，缺省行为与 M0–M4 逐字一致：
      · ``unavailable_after: int | None``：第 k 次 invoke 起（含该次）恒抛
        ``AdapterUnavailableError``（detail = ``unavailable_text``），供 §5.6.3
        第 1 条"特征命中即跳闸"的调度侧验收。被抛的调用**不查表、不写 ledger**
        （没处理成功就没有副作用），ledger 语义因此不变。
      · ``key_by: "event"|"call"``："call" 改按**该实例的调用序号**（从 1 起）
        查脚本表，与真实事件号解耦——M5 多出的通告/审计事件会让附录B 的事件号
        整体偏移，而脚本表的"第 i 次取用"语义不受偏移影响。ledger 仍记**真实
        触发事件号**（exactly-once 对账口径不变）。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        script: dict,
        ledger_path: str | Path,
        caps: Caps | None = None,
        unavailable_after: int | None = None,
        unavailable_text: str = "quota exceeded (mock)",
        key_by: str = "event",
        adapter_name: str | None = None,
    ) -> None:
        # script: {触发事件号(int): 预置作者字段信封(dict)}，来自附录B fixture 切片；
        # key_by="call" 时键改为调用序号（1,2,3,…）。
        if key_by not in ("event", "call"):
            raise ValueError(
                f"key_by 只允许 'event' | 'call'，实得 {key_by!r}"
            )
        self.role = role
        self.script = script
        self.ledger_path = Path(ledger_path)
        self.caps = caps if caps is not None else dict(_MOCK_CAPS)  # type: ignore[assignment]
        self.unavailable_after = unavailable_after
        self.unavailable_text = unavailable_text
        self.key_by = key_by
        # 额度错误里的 adapter 配置名；mock 通常按角色构造，故缺省用角色名兜底。
        self.adapter_name = str(adapter_name or role)
        # 本实例对该角色的调用序号（从 1 起，与 FakeCli/FakeApi 的 call_no 同一约定）。
        self.call_no = 0

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """按 (role, 本批触发号) 返回预置作者字段信封，并追加一行 ledger。

        触发号 = view['event_ids'] 的最大值（= 聚合派发本批的触发事件号，契约 §3）。
        副作用：写 ledger 前自动创建父目录（parents=True, exist_ok=True，T1 裁决③），
        追加一行 '{role}:{event_id}\\n'。返回 (env_dict, sess)；sess 原样透传
        （mock 不产生新会话状态）。

        M5：``unavailable_after`` 命中时在**任何**查表/副作用之前抛额度类错误。
        """
        self.call_no += 1
        if (
            self.unavailable_after is not None
            and self.call_no >= int(self.unavailable_after)
        ):
            raise AdapterUnavailableError(self.adapter_name, self.unavailable_text)

        event_id = max(view["event_ids"])

        # —— 查表：取该角色对本次调用的预置信封，规范化为只含作者字段的副本 —— #
        scripted = self.script[self.call_no if self.key_by == "call" else event_id]
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


def _unwrap_agent_output(stdout: str, config: dict) -> tuple[str, str | None]:
    """按 config.wire_format 从子进程 stdout 解出 (agent 回复文本, session_id)。

    §7.2 真实 CLI 输出包装各异（陪跑实测，QUESTIONS.md Q1）：
      - "text"（默认；claude 裸文本 / M2 既有行为）：整段 stdout 即回复；sid 交 _extract_sid。
      - "stream-json"（kimi -p --output-format stream-json，实测）：逐行 JSON；
        role=="assistant" 的 content 依序拼接为回复；带 session_id 的行（session.resume_hint）取会话号。
      - "json"（claude -p --output-format json / grok -p --output-format json，
        实测 2026-07-25 grok 0.2.112）：整段是一个 JSON；回复文本在 result（claude）
        或 text（grok）字段；会话号在 session_id（claude）或 sessionId（grok）。
      - "opencode-stream"（opencode run --format json，实测 2026-07-25 v1.18.4）：
        逐行 JSON 事件流；type=="text" 事件的 part.text 依序拼接为回复；
        sessionID 在各行顶层，取首见。
    回复文本再交 _extract_last_json_block 取信封。纯格式转换，无角色逻辑（§7.6）。
    """
    wire = str(config.get("wire_format", "text"))
    if wire == "opencode-stream":
        parts: list[str] = []
        sid: str | None = None
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            part = obj.get("part")
            if (
                obj.get("type") == "text"
                and isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
            if sid is None:
                cand = obj.get("sessionID")
                if isinstance(cand, str) and cand:
                    sid = cand
        return "".join(parts), sid
    if wire == "stream-json":
        parts: list[str] = []
        sid: str | None = None
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("role") == "assistant" and isinstance(obj.get("content"), str):
                parts.append(obj["content"])
            if sid is None:
                cand = obj.get("session_id")
                if isinstance(cand, str) and cand:
                    sid = cand
        return "\n".join(parts), sid
    if wire == "json":
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout, None
        if isinstance(obj, dict):
            # claude: result/session_id；grok: text/sessionId（同构异名，两组都认）
            text = obj.get("result")
            if not isinstance(text, str):
                text = obj.get("text")
            sid = obj.get("session_id")
            if not (isinstance(sid, str) and sid):
                sid = obj.get("sessionId")
            return (
                text if isinstance(text, str) else stdout,
                sid if isinstance(sid, str) and sid else None,
            )
        return stdout, None
    return stdout, None


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


# ——————————————————————————————————————————————————————————————
# §8.1 权限三件套之二：工具白名单——适配器配置注入 CLI 参数
# 三维评审 C-1 修复：CliAdapter / FakeCliAdapter 组装 argv 时**自动**根据
# self.caps["tools"] 追加 --allowedTools 参数，用户无需在 start_cmd 手拼。
# §17 开放决策（IMPLEMENTATION_NOTES.md 记录）：
#   - config["allowed_tools_flag"] 覆盖默认 "--allowedTools"（真实 CLI 差异陪跑）。
#   - config["allowed_tools_style"] ∈ {"spaced" 默认, "joined"}：
#       spaced：单个 flag 后跟多值（"--allowedTools" "Edit" "Write" ...）；
#       joined：一次一 flag（"--allowedTools" "Edit" "--allowedTools" "Write" ...）。
#   - 当 caps["tools"] 为空时不追加 flag（避免空 flag 垃圾）。
#   - 注入是**追加式**，不去删除 start_cmd 里可能手拼的旧 --allowedTools（back-compat）；
#     使用自动注入的 config 不应再在 start_cmd 手写该 flag。
# ——————————————————————————————————————————————————————————————
_DEFAULT_ALLOWED_TOOLS_FLAG = "--allowedTools"
_DEFAULT_ALLOWED_TOOLS_STYLE = "spaced"


def _build_allowed_tools_args(config: dict, tools: list[str]) -> list[str]:
    """按 §17 决策，把 caps.tools 折成 argv 片段。空列表返回 []（不注入 flag）。"""
    if not tools:
        return []
    flag = str(config.get("allowed_tools_flag", _DEFAULT_ALLOWED_TOOLS_FLAG))
    style = str(config.get("allowed_tools_style", _DEFAULT_ALLOWED_TOOLS_STYLE))
    if style == "joined":
        out: list[str] = []
        for t in tools:
            out.append(flag)
            out.append(str(t))
        return out
    # spaced（默认）：单 flag 多值。
    return [flag, *[str(t) for t in tools]]


def _start_cmd_argv(start_cmd, worktree) -> list[str]:
    """把 start_cmd 分词为 argv 前缀，并把字面量 ``{cwd}`` 替换为本次调用的 worktree。

    背景：opencode 无视进程 cwd、自寻项目根（陪跑实测 2026-07-25），必须靠
    ``--dir <worktree>`` 显式压制；而 worktree 路径只有运行期才知道，配置里写不出来。

    三条约束（改动前先读，别照搬"整串 replace 再 split"）：
      1) **先 split 再逐 token replace**。整串替换后再 ``.split()`` 会把含空格的
         Windows 路径裂成多个 argv 元素，且无法靠引号补救（``str.split`` 不解析引号）。
         在单个 token 内部做子串替换，产物天然仍是一个 argv 元素，零转义逻辑。
      2) 无占位时逐字节回归：``str.replace`` 未命中返回等值原串，故整条 argv 与
         改造前完全一致（既有 config / 测试零影响）。
      3) 作用域只到 start_cmd 的分词产物——不含 tools_args（flag 与工具名，无 cwd
         语义），更不含 ``view['text']``（正文是 agent 可写的，若参与替换等于开一条
         模板注入面）。分离式 ``--dir {cwd}`` 与等号式 ``--dir={cwd}`` 天然都支持。

    CliAdapter 与 FakeCliAdapter 两处 argv 组装**必须**共用本函数：孪生单边漂移会让
    基于 last_argv 的断言给出假绿。
    """
    return [tok.replace("{cwd}", str(worktree)) for tok in str(start_cmd).split()]


class CliAdapter:
    """CLI 型适配器骨架（spec §7.2）。

    子进程冷启动，cwd=角色 worktree；权限经 CLI 参数注入（§8.1）；
    从 stdout 取最后一个 ```json 块解析为作者字段信封（§7.2/§17）；
    超时 kill；提取 session_id（默认从 session_id/sid/session 字段任一，
    否则用 config.session_id_extract 正则兜底）。

    M2 只做冷启动路径；resume_cmd 保留但不调用（M3）。
    真实 CLI 的 flag/session_id 正则以 `--help` 实测为准（QUESTIONS.md Q1/Q2 陪跑）。

    M5（§7.6 末段 / §5.6.3 第 1 条）：传输级失败（超时 / 进程失败 / 无输出）时，
    把可得的 stderr / 退出信息 / 错误文本与 ``unavailable_patterns`` 匹配，命中即抛
    ``AdapterUnavailableError``；未命中则既有失败路径（TimeoutError / ValueError）
    逐字不变。json 块取到但内容非法属输出质量问题，**不**分类。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        worktree: Path,
        caps: Caps | None = None,
        adapter_name: str | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.worktree = Path(worktree)
        self.caps = caps if caps is not None else _caps_from_config(
            config, supports_resume=bool(config.get("supports_resume", True))
        )
        # adapter 配置名：显式参数 > config 里的键名（roles[role].adapter，见
        # cli.main._build_adapters_from_config 的 merged）> 角色名兜底（同
        # state.resolve_effective_adapter 的主绑定约定）。仅用于错误归属，不影响 invoke。
        self.adapter_name = str(adapter_name or self.config.get("adapter") or role)

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """冷启动路径：`start_cmd + view['text']` → 解析最后一个 json 块。

        - cwd=self.worktree；超时按 config.timeout_s 触发 kill 并抛 TimeoutError。
        - 无 json 块或 JSON 解析失败 → ValueError（调度层按 §5.1 重调）。
        - 返回 (env_dict, {"sid":..., "gen": gen+1})；无 sid 时 sess 仍带 gen（gen+1）。
        """
        start_cmd = str(self.config["start_cmd"])
        # §8.1 R-a：根据 self.caps["tools"] 自动追加 --allowedTools（含各工具名）。
        tools_args = _build_allowed_tools_args(
            self.config, list(self.caps.get("tools", []) or [])
        )
        # start_cmd 里的字面量 {cwd} → 本次调用的 worktree（token 级替换，见 _start_cmd_argv）。
        cmd = _start_cmd_argv(start_cmd, self.worktree) + tools_args + [str(view["text"])]
        timeout_s = int(self.config.get("timeout_s", self.caps.get("timeout_s", 0)) or 0)
        proc = subprocess.Popen(  # noqa: S603 — 冷启动子进程是 §7.2 明列职责
            cmd,
            cwd=str(self.worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",  # 真实 CLI（kimi/claude）输出恒 UTF-8；Windows 默认 gbk 会乱码（Q1 实测）
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s or None)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            _drained_out, drained_err = _drain_after_kill(proc)
            # 超时属传输级失败：kill 后能读到的 stderr 仍要过一遍特征匹配（§5.6.3-1）。
            # stdout 侧（exc.output / 排空读到的正文）不进分类——不属 §5.6.3 列举的
            # 三类报错文本，且正常输出里的 UUID 会撞子串清单（见 _classify_unavailable）。
            # 无文本可判 → 不分类，既有 TimeoutError 路径逐字不变。
            detail = _classify_unavailable(
                self.config,
                getattr(exc, "stderr", None),
                drained_err,
            )
            if detail is not None:
                raise AdapterUnavailableError(self.adapter_name, detail) from exc
            raise TimeoutError(
                f"CliAdapter[{self.role}] timed out after {timeout_s}s"
            )

        agent_text, sid_hint = _unwrap_agent_output(stdout or "", self.config)
        block = _extract_last_json_block(agent_text or "")
        if block is None:
            # 无输出 / 进程失败（拿不到信封块）——传输级失败，先分类后回落既有路径。
            # 分类输入只有 stderr 与退出信息（§5.6.3 列举）；stdout 正文不进分类
            # ——stopReason=Cancelled 之类正常输出里的 sessionId 会撞 '429' 误跳闸。
            detail = _classify_unavailable(
                self.config, stderr, _exit_info(proc),
            )
            if detail is not None:
                raise AdapterUnavailableError(self.adapter_name, detail)
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
        # stream-json/json 解包已给出 sid_hint（kimi resume_hint / claude session_id）；
        # 兜底走 _extract_sid（text 模式的信封字段 / 正则），保持 M2 既有语义。
        sid = sid_hint or _extract_sid(parsed, stdout or "", self.config)
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

    M5（§7.6 末段）：message_fn 抛出的传输级失败按 ``unavailable_patterns`` 分类，
    命中 → ``AdapterUnavailableError``；未命中 → 原异常原样上抛（不包装、不吞）。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        caps: Caps | None = None,
        message_fn: Callable[[dict, dict], dict] | None = None,
        adapter_name: str | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        # 与 CliAdapter 同一约定：显式参数 > config 键名 > 角色名兜底（仅用于错误归属）。
        self.adapter_name = str(adapter_name or self.config.get("adapter") or role)
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
        - M5：messages 调用抛出的传输级失败经特征匹配分类（§7.6/§5.6.3-1）。
        """
        if self._message_fn is None:
            raise NotImplementedError(
                "ApiAdapter 真实网络路径未启用（M2 边界）；"
                "测试请用 FakeApiAdapter 或注入 message_fn。"
            )
        try:
            raw = self._message_fn(view, self.config)
        except AdapterUnavailableError:
            raise  # 已由更底层分类过，原样上抛（不重复包装）
        except Exception as exc:  # noqa: BLE001 — §7.6：分类责任在适配层，此处必须兜住
            detail = _classify_unavailable(
                self.config, f"{type(exc).__name__}: {exc}",
            )
            if detail is not None:
                raise AdapterUnavailableError(self.adapter_name, detail) from exc
            raise  # 未命中 → 既有失败路径逐字不变
        env = _strip_to_author_fields(raw)
        return env, None


class FakeCliAdapter:
    """CliAdapter 的测试双（M2 契约 §2）。

    对外行为等价于 CliAdapter，但不启动真实子进程：
      - scripted_output：假子进程 stdout（单次/兜底，供解析最后一个 json 块）。
      - scripted_replies：多步控制流脚本 `{call_no: 作者字段信封}`（M2 契约 §2/§6）。
        call_no **从 1 起**（第一次 invoke 记为 call_no=1，与 §4.5 事件序号习惯一致，
        更符合直觉；非 0 起）。invoke 每次调用先自增 self.call_no 再查表。
        提供 scripted_replies 时优先于 scripted_output/simulate_timeout（脚本化多步
        场景不再模拟单次超时路径——如需超时仍用 scripted_output+simulate_timeout）。
        缺对应 call_no 的表项 → KeyError（暴露测试脚本编排错误，不静默兜底）。
      - inject_side_effect：`Callable[[Path], None] | None`，每次 invoke **首行**调用
        一次（在返回信封前，模拟 CLI 子进程刚落文件的时序，供 §8.2 越权注入验证）。
        参数为 self.worktree。
      - simulate_timeout=True：模拟超时 → kill + attempts+1 + TimeoutError（仅在未提供
        scripted_replies 时的单次路径生效）。
      - last_cwd / attempts / killed / gen / call_no：暴露给测试断言的可观测点。

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
        scripted_replies: dict[int, dict] | None = None,
        inject_side_effect: Callable[[Path], None] | None = None,
        caps: Caps | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.worktree = Path(worktree)
        self.scripted_output = scripted_output
        self.simulate_timeout = simulate_timeout
        self.scripted_replies = (
            dict(scripted_replies) if scripted_replies is not None else None
        )
        self._inject_side_effect = inject_side_effect
        self.caps = caps if caps is not None else _caps_from_config(
            config, supports_resume=True
        )
        # —— 测试可观测点 —— #
        self.last_cwd: str | None = None
        self.last_view_text: str | None = None
        self.last_argv: list[str] | None = None
        self.attempts: int = 0
        self.killed: bool = False
        self.gen: int = 0
        self.call_no: int = 0

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """假子进程冷启动：不真启动进程。

        scripted_replies 提供时按 call_no（从 1 起）分派；否则回退到既有
        scripted_output/simulate_timeout 单次路径。inject_side_effect 存在则
        本次 invoke 首行调用一次（cwd=self.worktree），模拟"CLI 子进程刚落文件"
        的时序，供调度环随后走 §8.2 audit_write_scope 审计。
        """
        self.attempts += 1
        self.call_no += 1
        self.last_cwd = str(self.worktree)
        self.last_view_text = str(view.get("text", ""))
        # §8.1 R-a：等价于 CliAdapter 的 argv 组装（含自动注入 --allowedTools）；
        # last_argv 供测试断言"配置注入生效"。
        start_cmd = str(self.config.get("start_cmd", ""))
        tools_args = _build_allowed_tools_args(
            self.config, list(self.caps.get("tools", []) or [])
        )
        self.last_argv = (
            _start_cmd_argv(start_cmd, self.worktree)
            + tools_args
            + [str(view.get("text", ""))]
        )

        # —— 越权/合规注入：在返回信封前执行一次（§8.2）—— #
        if self._inject_side_effect is not None:
            self._inject_side_effect(self.worktree)

        if self.scripted_replies is not None:
            if self.call_no not in self.scripted_replies:
                raise KeyError(
                    f"FakeCliAdapter[{self.role}] no scripted reply for "
                    f"call_no={self.call_no}"
                )
            raw = self.scripted_replies[self.call_no]
            env = _strip_to_author_fields(raw)
            sid = None
            for f in _DEFAULT_SID_FIELDS:
                v = raw.get(f)
                if isinstance(v, str) and v:
                    sid = v
                    break
            prev_gen = int((sess or {}).get("gen", 0))
            self.gen = prev_gen + 1
            new_sess: dict | None = {"sid": sid, "gen": self.gen}
            return env, new_sess

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

    直接使用可注入 scripted_reply（模拟 messages 返回的 dict，单步/兜底），
    或 scripted_replies（多步控制流脚本 `{call_no: 信封}`，M2 契约 §2/§6），
    走与 ApiAdapter 一致的归一化路径（§3.1/§7.6：只留作者字段）。

    scripted_replies 的 call_no 分派规则与 FakeCliAdapter 一致：**从 1 起**，
    每次 invoke 先自增 self.call_no 再查表；提供 scripted_replies 时优先于
    scripted_reply。缺对应 call_no 的表项 → KeyError。

    inject_side_effect：ApiAdapter 无 worktree 概念（§7.3 直连 messages，无子进程/
    无 cwd），因此本参数在 FakeApiAdapter 上是 **None-兼容占位**——不做任何调用；
    仅为与 FakeCliAdapter 签名对称、便于测试代码统一构造而保留。若传入非 None
    值，本类**忽略**它（API 型无落盘 worktree 可越权注入，§8.2 审计只对 CLI 型
    worktree 生效）。
    """

    caps: Caps

    def __init__(
        self,
        *,
        role: str,
        config: dict,
        scripted_reply: dict | None = None,
        scripted_replies: dict[int, dict] | None = None,
        inject_side_effect: Callable[[Any], None] | None = None,
        caps: Caps | None = None,
    ) -> None:
        self.role = role
        self.config = dict(config)
        self.scripted_reply = dict(scripted_reply) if scripted_reply is not None else None
        self.scripted_replies = (
            dict(scripted_replies) if scripted_replies is not None else None
        )
        # ApiAdapter 无 worktree；inject_side_effect 在本类上是 None-兼容占位（见 docstring），
        # 记录但不调用——API 型无 cwd/子进程时序可模拟。
        self._inject_side_effect = inject_side_effect
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
        self.call_no: int = 0

    def invoke(
        self, view: dict, sess: dict | None
    ) -> tuple[dict, dict | None]:
        """§7.3：单步；忽略入参 sess；返回 sess=None（每次全量组装，不复用会话）。

        scripted_replies 提供时按 call_no（从 1 起）分派；否则回退到既有
        scripted_reply 单次路径。
        """
        # 观察点：每次都记录 view.text 全量（不做增量差分）。
        self.last_view_text = str(view.get("text", ""))
        self.step_count += 1
        self.call_no += 1

        if self.scripted_replies is not None:
            if self.call_no not in self.scripted_replies:
                raise KeyError(
                    f"FakeApiAdapter[{self.role}] no scripted reply for "
                    f"call_no={self.call_no}"
                )
            env = _strip_to_author_fields(self.scripted_replies[self.call_no])
            return env, None

        env = _strip_to_author_fields(self.scripted_reply or {})
        return env, None
