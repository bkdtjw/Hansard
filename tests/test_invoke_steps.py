"""T4 · invoke 执行流步骤解析 + logs/ 原文合规修复 验收测试。

两件事，红转绿判据分别是：
  ① **合规债**（spec §14 行603「每次 invoke 的完整输入/输出原文落
     threads/t-xxx/logs/」）：`core.py` / `async_core.py` 过去调
     `store.write_invoke_log(..., output_text=str(raw_env))` —— 落的是剥到 6 个
     作者字段后的**信封 dict repr**，不是 stdout 原文。本文件用真 `CliAdapter`
     （monkeypatch `subprocess.Popen`，不启真实 CLI）跑一轮 `run_thread`，断言
     logs/ 文件里出现 stdout 的原文关键行；修复前该断言必红。
  ② **展示用步骤解析**（QUESTIONS.md Q11 裁决 A）：`parse_invoke_steps` 是纯函数，
     只对 `wire_format ∈ {stream-json, opencode-stream}` 产出，其余返回 []；
     summary 一律截断（≤120 字符），坏行宽容跳过、绝不抛。
     Q11 暴露口径：**stdout 原文不经 HTTP 直出** —— 故解析产物里不得出现
     工具**输出**正文（sessionId 等敏感串的实证藏身处，见 QUESTIONS.md Q9）。

硬约束（沿 tests/test_cli_adapter.py 同一风格）：
  - 顶层只做包级导入；未实现符号在函数体内引用 → 表现为运行时红而非 collection 中断。
  - 不启真实子进程（Popen 打桩）；临时目录用仓库约定的 `tmp_dir` fixture。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import orch.adapters
import orch.scheduler
import orch.store


# ——————————————————————————————————————————————————————————————
# 造数：两种流式 wire_format 的**构造**样例（评审 建议8 校正溯源口径）
#
# 外层信封形状（顶层 type / part / sessionID；assistant / meta 行）有实测依据：
# tests/test_cli_adapter.py 的 test_unwrap_opencode_stream_shape /
# test_unwrap_stream_json_kimi_shape。但**工具行的内层**——part.state.input /
# state.output / state.time，以及 stream-json 的 tool_calls 行——本仓**没有**实测档案：
# 陪跑只落盘了 assistant / meta 两种行（QUESTIONS.md Q1），state.time 的单位口径也
# 无记录。故下面的工具行是照两家公开形状**构造**的，不是抄来的真实样本；解析层因此
# 一律走宽容兜底（认多家形状、认不出就归 other、单位不可证就不给 dur_ms），
# 而不是依赖这份样例的精确性。
# ——————————————————————————————————————————————————————————————

# 敏感串埋点：真实 opencode/grok stdout 的 sessionId 形状（Q9 档案实证含十六进制尾）。
FIXTURE_SID = "ses_019f98e7352 4-0758bd76e429".replace(" ", "")

_ENVELOPE = ('```json\n{"to":["moderator"],"type":"report","body":"跑完了"}\n```')


def _opencode_lines(sid: str = FIXTURE_SID, envelope: str = _ENVELOPE) -> list[dict]:
    """opencode run --format json 的逐行事件（step/tool/reasoning/text/step_finish）。

    工具行的 `state.output` 里**故意**塞进 sid：它是"工具输出正文"，按 Q11 裁决
    不得随步骤摘要外泄（摘要口径只取工具名 + 命令）。
    """
    return [
        {"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}},
        {"type": "tool", "sessionID": sid, "part": {
            "type": "tool", "tool": "bash", "callID": "call_1",
            "state": {
                "status": "completed",
                "input": {"command": "pytest -q tests/test_web.py"},
                "output": f"3 passed; session={sid}",
                "time": {"start": 1785037196000, "end": 1785037198500},
            },
        }},
        {"type": "reasoning", "sessionID": sid, "part": {
            "type": "reasoning", "text": "先跑一遍测试确认基线",
        }},
        {"type": "text", "sessionID": sid, "part": {
            "type": "text", "text": "结论：\n" + envelope,
        }},
        {"type": "step_finish", "sessionID": sid, "part": {
            "type": "step-finish", "reason": "stop",
        }},
    ]


def _opencode_stdout(sid: str = FIXTURE_SID, envelope: str = _ENVELOPE) -> str:
    return "\n".join(
        json.dumps(x, ensure_ascii=False) for x in _opencode_lines(sid, envelope)
    )


def _kimi_lines(sid: str = FIXTURE_SID) -> list[dict]:
    """kimi -p --output-format stream-json 的逐行 JSON（含工具调用行）。

    Q1 陪跑只记录了 assistant / meta 两种行（QUESTIONS.md Q1），工具行形状未落盘；
    故这里用两家常见形状之一（OpenAI 风 tool_calls）造数，解析器对另一种
    （Anthropic 风 type=tool_use）同样宽容——两者都命中才算"不臆造单一家"。
    """
    return [
        {"role": "assistant", "content": "我先看一下测试"},
        {"role": "assistant", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "Bash", "arguments": '{"command":"pytest -q"}'}},
        ]},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "src/orch/web/server.py"}},
        {"role": "assistant", "content": _ENVELOPE},
        {"role": "meta", "type": "session.resume_hint", "session_id": sid},
    ]


def _kimi_stdout(sid: str = FIXTURE_SID) -> str:
    return "\n".join(json.dumps(x, ensure_ascii=False) for x in _kimi_lines(sid))


# ——————————————————————————————————————————————————————————————
# ②a sniff_log_wire_format：格式判定只认原文形状（评审 应修2）
#
# 为什么必须由内容定：logs/ 的原文是**历史**产物，换绑后当前绑定与产出它的后端可能
# 不是一家；按当前绑定硬解析会吐出假步骤（真实工具名与命令全丢，而页面看不出错）。
# ——————————————————————————————————————————————————————————————

def test_sniff_recognizes_both_stream_shapes():
    """两家流式形状各自认出来（判据是 part/sessionID vs role/tool_calls/事件型名）。"""
    from orch.adapters import sniff_log_wire_format

    assert sniff_log_wire_format(_opencode_stdout()) == "opencode-stream"
    assert sniff_log_wire_format(_kimi_stdout()) == "stream-json"
    # 只有一行的流式日志也不能被"整段能 json.loads"误判成 json（逐行票先算）。
    one_line = json.dumps(_opencode_lines()[1], ensure_ascii=False)
    assert sniff_log_wire_format(one_line) == "opencode-stream"


def test_sniff_recognizes_non_stream_shapes():
    """单 JSON 直出 → "json"；没有任何 JSON 行 → "text"（都不是流式，产物必为空）。"""
    from orch.adapters import parse_invoke_steps, sniff_log_wire_format

    single = json.dumps({"text": "收到\n" + _ENVELOPE, "sessionId": FIXTURE_SID})
    assert sniff_log_wire_format(single) == "json"
    assert sniff_log_wire_format("收到。\n" + _ENVELOPE) == "text"
    # sessionId（小写 d）/ session_id 都不是 opencode 的 sessionID，不许误命中。
    assert sniff_log_wire_format(json.dumps({"session_id": FIXTURE_SID})) == "json"
    # 作者信封行不是事件行：它自己就是合法 JSON 对象，且 type 可以合法地取
    # "system"/"user"（附录 A 枚举内，与 stream-json 的事件型名撞名）。两件事都不许
    # 让裸文本被读成流式 —— 否则会按流式去解析一段散文，吐出满屏 other 假步骤。
    for env_type in ("report", "system", "user"):
        raw = ("先说结论。\n```json\n"
               + json.dumps({"to": ["pm"], "type": env_type, "body": "done"})
               + "\n```")
        assert sniff_log_wire_format(raw) == "text", env_type
    for raw in (single, "收到。\n" + _ENVELOPE):
        wire = sniff_log_wire_format(raw)
        assert parse_invoke_steps(raw, wire) == []


def test_sniff_returns_none_when_shape_is_unrecognized():
    """两家特征都不像 / 票数相等 → None（诚实空态的服务端依据），绝不猜。"""
    from orch.adapters import parse_invoke_steps, sniff_log_wire_format

    weird = "\n".join(json.dumps({"evt": i, "payload": {"cmd": "ls"}}) for i in range(3))
    assert sniff_log_wire_format(weird) is None
    # 混杂（一行 opencode 一行 stream-json）：说不清是哪一家，同样不猜。
    mixed = "\n".join([
        json.dumps({"type": "text", "part": {"type": "text", "text": "a"}}),
        json.dumps({"role": "assistant", "content": "b"}),
    ])
    assert sniff_log_wire_format(mixed) is None
    assert sniff_log_wire_format("") is None
    assert sniff_log_wire_format(None) is None      # type: ignore[arg-type]
    # None 传给解析器 → 空（不按行试探）。
    assert parse_invoke_steps(weird, sniff_log_wire_format(weird) or "") == []


def test_sniff_ignores_current_binding_by_construction():
    """判据里没有"配置"这个入参——嗅探是纯函数，签名上就不可能读到绑定（应修2）。"""
    import inspect

    from orch.adapters import sniff_log_wire_format

    params = list(inspect.signature(sniff_log_wire_format).parameters)
    assert params == ["raw_text"], params


# ——————————————————————————————————————————————————————————————
# ② parse_invoke_steps：两种流式格式 / 非流式空 / 截断 / 坏行宽容
# ——————————————————————————————————————————————————————————————

def test_parse_steps_opencode_stream_real_shape():
    """opencode-stream：工具行→kind=tool（name=工具名、summary=命令），
    reasoning→thinking，text→text；seq 从 1 起连续。"""
    from orch.adapters import parse_invoke_steps

    steps = parse_invoke_steps(_opencode_stdout(), "opencode-stream")
    assert steps, "流式原文应解析出步骤"
    assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
    kinds = [s["kind"] for s in steps]
    assert "tool" in kinds and "thinking" in kinds and "text" in kinds
    tool = next(s for s in steps if s["kind"] == "tool")
    assert tool["name"] == "bash"
    assert "pytest -q tests/test_web.py" in tool["summary"]
    think = next(s for s in steps if s["kind"] == "thinking")
    assert "先跑一遍测试确认基线" in think["summary"]
    # 每步四键齐（前端渲染契约）。
    for s in steps:
        for key in ("seq", "kind", "name", "summary"):
            assert key in s, s


def test_parse_steps_stream_json_kimi_shape():
    """stream-json：assistant.content→text；tool_calls / type=tool_use 两种形状都算 tool。"""
    from orch.adapters import parse_invoke_steps

    steps = parse_invoke_steps(_kimi_stdout(), "stream-json")
    tools = [s for s in steps if s["kind"] == "tool"]
    assert [t["name"] for t in tools] == ["Bash", "Read"], steps
    assert "pytest -q" in tools[0]["summary"]
    assert "src/orch/web/server.py" in tools[1]["summary"]
    texts = [s for s in steps if s["kind"] == "text"]
    assert texts and "我先看一下测试" in texts[0]["summary"]


def test_parse_steps_empty_for_non_stream_wire_formats():
    """"json"（claude/grok 整段单 JSON）与 "text"（直出）没有逐行事件 → 一律 []。

    诚实空态的服务端根据：不是"解析失败"，是**这种后端不产生步骤流**。
    """
    from orch.adapters import parse_invoke_steps

    single = json.dumps({"text": "收到\n" + _ENVELOPE, "sessionId": FIXTURE_SID})
    assert parse_invoke_steps(single, "json") == []
    assert parse_invoke_steps(_ENVELOPE, "text") == []
    assert parse_invoke_steps(_opencode_stdout(), "") == []
    # 未知 wire_format 同样不猜（不按行试探）。
    assert parse_invoke_steps(_opencode_stdout(), "some-future-format") == []


def test_parse_steps_summary_truncated_with_ellipsis():
    """summary ≤120 字符且超长加省略号（模型可控文本，绝不整段外泄）。"""
    from orch.adapters import parse_invoke_steps

    long_cmd = "echo " + ("A" * 500)
    line = {"type": "tool", "sessionID": FIXTURE_SID, "part": {
        "type": "tool", "tool": "bash",
        "state": {"input": {"command": long_cmd}},
    }}
    steps = parse_invoke_steps(json.dumps(line), "opencode-stream")
    assert len(steps) == 1
    summary = steps[0]["summary"]
    assert len(summary) <= 120, len(summary)
    assert summary.endswith("…")
    assert "A" * 500 not in summary


def test_parse_steps_name_is_capped_too():
    """name 与 summary 同源同截断（评审 建议3）：工具名也是模型可控文本，不许无界。

    有上限而另一边没有，等于留一条整段外泄的旁路——造一个超长"工具名"即可绕过。
    """
    from orch.adapters import _STEP_NAME_LIMIT, parse_invoke_steps

    long_name = "T" * 4000
    oc = {"type": "tool", "sessionID": FIXTURE_SID, "part": {
        "type": "tool", "tool": long_name, "state": {"input": {"command": "ls"}}}}
    got = parse_invoke_steps(json.dumps(oc), "opencode-stream")
    assert len(got[0]["name"]) <= _STEP_NAME_LIMIT, len(got[0]["name"])
    assert got[0]["name"].endswith("…")
    assert long_name not in json.dumps(got, ensure_ascii=False)

    # stream-json 侧同样受限（两条入口都不能漏）。
    sj = {"role": "assistant", "tool_calls": [
        {"function": {"name": long_name, "arguments": '{"command":"ls"}'}}]}
    got = parse_invoke_steps(json.dumps(sj), "stream-json")
    assert len(got[0]["name"]) <= _STEP_NAME_LIMIT, len(got[0]["name"])


def test_parse_steps_respects_max_steps_cap():
    """max_steps 到量即停（建议4 的解析侧半边）：不白解析后面几万行。"""
    from orch.adapters import parse_invoke_steps

    raw = "\n".join(
        json.dumps({"type": "tool", "part": {
            "type": "tool", "tool": f"t{i}", "state": {"input": {"command": "ls"}}}})
        for i in range(300))
    assert len(parse_invoke_steps(raw, "opencode-stream")) == 300      # 不给上限=不截
    got = parse_invoke_steps(raw, "opencode-stream", max_steps=25)
    assert len(got) == 25, len(got)
    assert [s["seq"] for s in got] == list(range(1, 26))
    # 一行产出多步时也不越界（stream-json 的 tool_calls 可一行多个）。
    multi = json.dumps({"role": "assistant", "tool_calls": [
        {"function": {"name": f"n{i}", "arguments": "{}"}} for i in range(10)]})
    assert len(parse_invoke_steps(multi, "stream-json", max_steps=3)) == 3
    # 非正数/None 视为"不设上限"（不静默截成 0 条）。
    assert len(parse_invoke_steps(raw, "opencode-stream", max_steps=0)) == 300


def test_parse_steps_tolerates_broken_lines_and_never_raises():
    """坏行（非 JSON / 非 dict / 空行 / 缺 part）宽容跳过，已解析部分照常返回。"""
    from orch.adapters import parse_invoke_steps

    raw = "\n".join([
        "not json at all",
        "",
        "[1,2,3]",
        json.dumps({"type": "tool"}),                       # 缺 part
        json.dumps({"type": "text", "part": {"type": "text", "text": "有效一行"}}),
        "{半个 JSON",
    ])
    steps = parse_invoke_steps(raw, "opencode-stream")
    assert any(s["kind"] == "text" and "有效一行" in s["summary"] for s in steps)
    # 极端输入不抛（None / 非字符串同样宽容）。
    assert parse_invoke_steps("", "opencode-stream") == []
    assert parse_invoke_steps(None, "opencode-stream") == []      # type: ignore[arg-type]
    assert parse_invoke_steps(12345, "stream-json") == []         # type: ignore[arg-type]


def test_parse_steps_never_carries_tool_output_or_session_id():
    """Q11 暴露口径：步骤摘要只出工具名 + 命令；工具**输出**正文与 sessionId 不外带。"""
    from orch.adapters import parse_invoke_steps

    for wire, raw in (
        ("opencode-stream", _opencode_stdout()),
        ("stream-json", _kimi_stdout()),
    ):
        steps = parse_invoke_steps(raw, wire)
        blob = json.dumps(steps, ensure_ascii=False)
        assert FIXTURE_SID not in blob, (wire, blob)
        assert "3 passed" not in blob, (wire, blob)


def test_parse_steps_dur_ms_only_when_timestamps_are_epoch_ms():
    """耗时只在能**证明**单位时才出：两端时间戳都是毫秒纪元才给 dur_ms，否则省略键。

    不臆造单位（陪跑记录未写明 opencode state.time 的单位口径）。
    """
    from orch.adapters import parse_invoke_steps

    steps = parse_invoke_steps(_opencode_stdout(), "opencode-stream")
    tool = next(s for s in steps if s["kind"] == "tool")
    assert tool["dur_ms"] == 2500

    vague = {"type": "tool", "part": {"type": "tool", "tool": "bash", "state": {
        "input": {"command": "ls"}, "time": {"start": 1, "end": 3}}}}
    got = parse_invoke_steps(json.dumps(vague), "opencode-stream")
    assert "dur_ms" not in got[0], got


# ——————————————————————————————————————————————————————————————
# ① logs/ 落真 stdout 原文（红转绿：修复前落的是信封 dict repr）
# ——————————————————————————————————————————————————————————————

def _cli_role_config() -> dict:
    """单角色 config：CLI 型 moderator，无 target_repo（不建 worktree，无需 git）。"""
    return {
        "thread_defaults": {"max_rounds": 5, "loop_limit": 3, "chat_ttl": 10},
        "roles": {
            "moderator": {
                "can_decide": True, "write_scope": [], "tools": [],
                "adapter": "oc", "caps": {"timeout_s": 30},
            },
        },
        "adapters": {
            "oc": {"kind": "cli", "start_cmd": "oc run --format json",
                   "wire_format": "opencode-stream", "timeout_s": 30},
        },
    }


def _patch_popen(monkeypatch, stdout: str, captured: dict | None = None):
    """打桩 subprocess.Popen：不启真实 CLI，communicate 直接吐给定 stdout。"""
    class _FakeProc:
        returncode = 0

        def __init__(self, argv, **kw):
            if captured is not None:
                captured["argv"] = list(argv)
                captured["cwd"] = kw.get("cwd")

        def communicate(self, timeout=None):
            return (stdout, "")

        def kill(self):
            pass

    monkeypatch.setattr("orch.adapters.subprocess.Popen", _FakeProc)


def _read_invoke_log(thread_dir: Path, role: str) -> str:
    """取该角色最新一份 invoke 日志原文（文件名 {ts}_E{ids}_{role}.log，§14）。"""
    logs = sorted(
        p for p in (thread_dir / "logs").iterdir()
        if p.name.endswith(f"_{role}.log")
    )
    assert logs, f"logs/ 下应有 {role} 的审计文件：{list((thread_dir / 'logs').iterdir())}"
    return logs[-1].read_text(encoding="utf-8")


def test_write_invoke_log_carries_real_stdout_not_envelope_repr(tmp_dir, monkeypatch):
    """§14 行603：logs/ 必须是**输出原文**。

    红（修复前）：output 段是 `{'to': ['moderator'], 'type': 'report', ...}` 这段
    dict repr，工具调用行一个字都没有 —— 与 §14「完整输出原文」直接冲突。
    """
    envelope = '```json\n{"to":[],"type":"terminate","body":"收尾"}\n```'
    stdout = _opencode_stdout(envelope=envelope)
    _patch_popen(monkeypatch, stdout)

    thread_dir = tmp_dir / "t-log1"
    store = orch.store.Store(thread_dir)
    store.set_meta("status", "running")
    store.append_event(sender="human", type="assign", body="跑一轮", to=["moderator"])

    adapter = orch.adapters.CliAdapter(
        role="moderator",
        config={**_cli_role_config()["adapters"]["oc"], "adapter": "oc"},
        worktree=thread_dir,
    )
    orch.scheduler.run_thread(store, _cli_role_config(), {"moderator": adapter})

    text = _read_invoke_log(thread_dir, "moderator")
    # 原文关键行：工具调用行整行、以及只存在于原文里的 sessionID/命令。
    assert '"type": "tool"' in text or '"type":"tool"' in text, text[-800:]
    assert "pytest -q tests/test_web.py" in text
    assert FIXTURE_SID in text, "原文落盘（供审计）；对外 HTTP 才做摘要"
    # 输入侧照旧是完整渲染视图（那一半本来就是真输入，不动）。
    assert "=== VIEW (role=moderator" in text


def test_cli_adapter_exposes_last_raw_output(tmp_dir, monkeypatch):
    """CliAdapter 暂存本次 stdout 完整原文（供调度层落盘；不参与任何分类/判定）。"""
    stdout = _opencode_stdout()
    _patch_popen(monkeypatch, stdout)
    wt = tmp_dir / "wt-raw"
    wt.mkdir()
    ad = orch.adapters.CliAdapter(
        role="backend",
        config={"kind": "cli", "start_cmd": "oc run", "wire_format": "opencode-stream",
                "timeout_s": 5},
        worktree=wt,
    )
    ad.invoke({"role": "backend", "event_ids": [1], "text": "v", "sections": {},
               "meta": {}}, None)
    assert ad.last_raw_output == stdout


def test_cli_adapter_keeps_partial_raw_output_on_timeout(tmp_dir, monkeypatch):
    """超时路径"拿到多少存多少"：kill 后排空到的 stdout 也进 last_raw_output，不臆造。"""
    import subprocess as _sp

    partial = json.dumps(_opencode_lines()[0], ensure_ascii=False)

    class _SlowProc:
        returncode = -9

        def __init__(self, argv, **kw):
            pass

        def communicate(self, timeout=None):
            if timeout is not None:
                raise _sp.TimeoutExpired(cmd="oc", timeout=timeout)
            return (partial, "")

        def kill(self):
            pass

    monkeypatch.setattr("orch.adapters.subprocess.Popen", _SlowProc)
    wt = tmp_dir / "wt-to"
    wt.mkdir()
    ad = orch.adapters.CliAdapter(
        role="backend",
        config={"kind": "cli", "start_cmd": "oc run", "timeout_s": 1},
        worktree=wt,
    )
    with pytest.raises(TimeoutError):
        ad.invoke({"role": "backend", "event_ids": [1], "text": "v", "sections": {},
                   "meta": {}}, None)
    assert ad.last_raw_output == partial


def test_invoke_log_falls_back_to_envelope_repr_for_adapters_without_stdout(tmp_dir):
    """无 stdout 的适配器（Fake/mock/API 型）落信封 repr **并注明**，不写空段。

    这是现状语义的显式化，不算回归（任务卡口径）：拿不到原文就如实说拿不到。
    """
    from orch.scheduler.core import _invoke_output_text

    class _NoRaw:
        pass

    text = _invoke_output_text(_NoRaw(), {"to": ["pm"], "type": "chat", "body": "hi"})
    assert "'type': 'chat'" in text
    assert "未提供" in text, text

    class _WithRaw:
        last_raw_output = "line-1\nline-2"

    assert _invoke_output_text(_WithRaw(), {"type": "chat"}) == "line-1\nline-2"


def test_async_and_sync_scheduler_share_one_output_text_helper():
    """孪生防漂移（adapters/__init__.py:429-431 的既往伤）：两环用**同一个**实现。

    不是"两处都改对"，而是 async_core 直接 import core 的 helper —— 单一实现，
    不存在单边漂移的可能。
    """
    import orch.scheduler.async_core as ac
    import orch.scheduler.core as sc

    assert ac._invoke_output_text is sc._invoke_output_text
    # 失败路径的落盘 helper 同样单一实现（评审 建议7）。
    assert ac._write_invoke_log_on_failure is sc._write_invoke_log_on_failure


# ——————————————————————————————————————————————————————————————
# ③ 失败路径也落 invoke 日志（评审 建议7 · 红转绿）
#
# §14 行603 说的是「**每次** invoke 的完整输入/输出原文」。此前只有成功路径落盘：
# 超时 / 无合法 JSON 块 / 额度跳闸这三条 return 前把适配层已暂存的 last_raw_output
# 当场丢掉——最需要事后勘查的那几次，盘上一个字都没有。
# ——————————————————————————————————————————————————————————————

def _timeout_popen(monkeypatch, partial: str):
    """打桩 Popen：带 timeout 的 communicate 必超时；kill 后排空能读到 partial。"""
    import subprocess as _sp

    class _SlowProc:
        returncode = -9

        def __init__(self, argv, **kw):
            pass

        def communicate(self, timeout=None):
            if timeout is not None:
                raise _sp.TimeoutExpired(cmd="oc", timeout=timeout)
            return (partial, "")

        def kill(self):
            pass

    monkeypatch.setattr("orch.adapters.subprocess.Popen", _SlowProc)


def _seeded_thread(tmp_dir, name: str):
    thread_dir = tmp_dir / name
    store = orch.store.Store(thread_dir)
    store.set_meta("status", "running")
    store.append_event(sender="human", type="assign", body="跑一轮", to=["moderator"])
    return thread_dir, store


def _cli_adapter(thread_dir):
    return orch.adapters.CliAdapter(
        role="moderator",
        config={**_cli_role_config()["adapters"]["oc"], "adapter": "oc"},
        worktree=thread_dir,
    )


def test_timeout_path_writes_invoke_log_with_partial_stdout(tmp_dir, monkeypatch):
    """超时（§16.2 传输级失败）也落审计日志：抬头注明异常 + 已捕获的半程原文。

    红（修复前）：logs/ 目录里一个文件都没有——`_read_invoke_log` 直接 assert 失败。
    """
    partial = _opencode_stdout()
    _timeout_popen(monkeypatch, partial)
    thread_dir, store = _seeded_thread(tmp_dir, "t-fail-timeout")

    # availability 未启用（无 adapter_state_path）→ 该路径按既有语义**上抛**，
    # 不因为多了一次落盘而改变（落盘在 raise 之前，语义不变）。
    with pytest.raises(TimeoutError):
        orch.scheduler.run_thread(
            store, _cli_role_config(), {"moderator": _cli_adapter(thread_dir)})

    text = _read_invoke_log(thread_dir, "moderator")
    assert "本次invoke异常" in text.replace(" ", "") or "本次 invoke 异常" in text, text[:400]
    assert "TimeoutError" in text, text[:400]
    assert "部分" in text, "半程原文必须标明可能不完整，否则会被当成完整输出读"
    # 已捕获的片段确实落进去了（工具行命令 + 只存在于原文里的 sessionID）。
    assert "pytest -q tests/test_web.py" in text
    assert FIXTURE_SID in text
    # 输入侧照旧是本次真实送出的渲染视图。
    assert "=== VIEW (role=moderator" in text


def test_transport_failure_path_writes_invoke_log_with_availability_enabled(
        tmp_dir, monkeypatch):
    """M5 启用时超时走 return（不上抛）那条出口，同样要落日志，且不改跳闸/派发语义。"""
    _timeout_popen(monkeypatch, _opencode_stdout())
    thread_dir, store = _seeded_thread(tmp_dir, "t-fail-trip")
    cfg = {**_cli_role_config(),
           "adapter_state_path": str(tmp_dir / "adapter_state.json")}

    orch.scheduler.run_thread(store, cfg, {"moderator": _cli_adapter(thread_dir)})

    text = _read_invoke_log(thread_dir, "moderator")
    assert "TimeoutError" in text, text[:400]
    assert FIXTURE_SID in text
    # 语义不变：派发行仍按既有 attempts 语义留在盘上（落盘只是旁路，不改状态机）。
    rows = store.dispatches_snapshot()
    assert rows and any(int(r["attempts"]) >= 1 for r in rows), rows


def test_no_json_block_path_writes_invoke_log(tmp_dir, monkeypatch):
    """"无合法 JSON 块"（ValueError）那条出口同样落盘：原文全在，才看得出后端说了啥。"""
    junk = "我想了很久但忘了输出信封。\n随便几行散文。"
    _patch_popen(monkeypatch, junk)
    thread_dir, store = _seeded_thread(tmp_dir, "t-fail-nojson")

    with pytest.raises(ValueError):
        orch.scheduler.run_thread(
            store, _cli_role_config(), {"moderator": _cli_adapter(thread_dir)})

    text = _read_invoke_log(thread_dir, "moderator")
    assert "ValueError" in text, text[:400]
    assert "随便几行散文" in text, "原文必须在（这正是排查'为什么没信封'的唯一依据）"


def test_failure_log_helper_is_pure_audit_and_never_breaks_caller(tmp_dir):
    """落盘 helper 是**纯审计旁路**：盘写不动时也不得把异常带给调用点（建议7 硬约束）。

    调用点接着要做的（跳闸记账 / 派发行回 pending / 标 failed）是可恢复性的命脉，
    绝不能被"审计少一行"这种事顶掉。
    """
    from orch.scheduler.core import _invoke_failure_output_text, _write_invoke_log_on_failure

    class _NoRaw:
        pass

    head = _invoke_failure_output_text(_NoRaw(), TimeoutError("boom"))
    assert "TimeoutError" in head and "未捕获到任何 stdout" in head, head

    class _WithRaw:
        last_raw_output = "line-1\nline-2"

    body = _invoke_failure_output_text(_WithRaw(), ValueError("no json"))
    assert body.endswith("line-1\nline-2"), body
    assert "ValueError" in body.splitlines()[0], body

    class _BrokenStore:
        def write_invoke_log(self, **kw):
            raise OSError("disk full")

    # 不抛（否则调用点的跳闸/状态落盘会被顶掉）。
    _write_invoke_log_on_failure(
        _BrokenStore(), event_ids=[1], role="pm", view_text="v",
        adapter=_NoRaw(), exc=TimeoutError("x"),
    )

    class _WeirdStore:
        def write_invoke_log(self, **kw):
            raise RuntimeError("programming error")

    # 非 OSError（真 bug）照旧上抛，不被降级成"日志没落上"。
    with pytest.raises(RuntimeError):
        _write_invoke_log_on_failure(
            _WeirdStore(), event_ids=[1], role="pm", view_text="v",
            adapter=_NoRaw(), exc=TimeoutError("x"),
        )
