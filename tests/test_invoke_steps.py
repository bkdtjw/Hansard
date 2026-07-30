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
# 造数：两种流式 wire_format 的**真实形状**样例
# （opencode 1.18.4 / kimi stream-json，形状来源见 tests/test_cli_adapter.py
#  的 test_unwrap_opencode_stream_shape / test_unwrap_stream_json_kimi_shape）
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
