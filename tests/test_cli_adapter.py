"""M2 T1 · CliAdapter / FakeCliAdapter 验收测试（spec §7.1/§7.2，M2 契约 §2）。

覆盖任务卡条目 (a)：
  - FakeCliAdapter 冷启动 cwd=worktree 校验（子进程等价语义）。
  - 超时 → kill + attempts+1（M2 契约 §2；本层 mock 版验证语义）。
  - session_id 提取：从输出 JSON 字段（session_id/sid/session）+ 配置正则兜底。
  - 输出解析：取标准输出**最后一个** ```json 块（spec §17 决策 + §7.2）。
  - Caps 结构：M2 契约 §2 与 spec §7.1 一致。

M2 边界（任务卡红线）：
  - 禁止启动真实 claude/codex/kimi 子进程；实测 flag 属 QUESTIONS.md Q1/Q2 陪跑项。
  - 测试双 FakeCliAdapter 是纯 Python 假子进程，接口与真实 CliAdapter 一致。

硬约束（契约 §1/§7）：
  - 顶层只 `import orch.adapters`（包级导入）；具体符号在函数体内引用，
    使未实现符号表现为**运行时红**（AttributeError），而非 collection 中断。
  - 断言只依赖 M2 契约 §2 公开签名与语义；不依赖任何未冻结的内部实现细节。
  - 用 tmp_dir / monkeypatch 模拟；不实调 subprocess.run("claude" ...)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orch.adapters  # 包级导入（未实现符号函数体内引用）


# ——————————————————————————————————————————————————————————————
# Caps 结构：M2 契约 §2 与 spec §7.1 七字段一致
# ——————————————————————————————————————————————————————————————

def test_caps_typed_dict_seven_fields_present(tmp_dir):
    """§7.1 Caps 七字段：context_window / tools / write_scope / cost_tier /
    supports_resume / timeout_s / max_concurrent。M2 契约 §2 沿用。

    以 FakeCliAdapter 实例的 caps 属性为断言载体（M2 未实现符号 → AttributeError 红）。
    """
    wt = tmp_dir / "wt-caps"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="backend", config=_cfg(),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"question","body":"?"}\n```',
    )
    caps = ad.caps
    # 七字段齐（结构性断言，防 TypedDict 抽字段）。
    for key in ("context_window", "tools", "write_scope", "cost_tier",
                "supports_resume", "timeout_s", "max_concurrent"):
        assert key in caps


# ——————————————————————————————————————————————————————————————
# FakeCliAdapter：M2 契约 §2 定义"测试双"（与 CliAdapter 同接口）
# ——————————————————————————————————————————————————————————————

def _cfg(**over) -> dict:
    """FakeCliAdapter 构造用的最小 config（§11.1 子集，M2 契约 §2）。"""
    base = {
        "kind": "cli",
        "start_cmd": "fake-claude -p --allowedTools Edit Write",
        "resume_cmd": "fake-claude -p --resume {sid}",
        "timeout_s": 5,
    }
    base.update(over)
    return base


def _view(role: str = "backend", event_ids: list[int] | None = None,
          text: str = "hello view"):
    return {
        "role": role,
        "event_ids": list(event_ids or [1]),
        "text": text,
        "sections": {},
        "meta": {},
    }


# —— (a1) cwd=worktree 校验 —— #

def test_fake_cli_adapter_uses_worktree_as_cwd(tmp_dir):
    """§7.2：子进程执行 cwd=角色 worktree（M2 契约 §2）。FakeCliAdapter 记录调用 cwd。"""
    wt = tmp_dir / "wt-backend"
    wt.mkdir()

    ad = orch.adapters.FakeCliAdapter(
        role="backend", config=_cfg(),
        worktree=wt,
        # scripted_output 是 FakeCliAdapter 独有的可注入桩输出（合法 JSON 块结尾）。
        scripted_output='```json\n{"to":["pm"],"type":"question","body":"?",'
                        '"session_id":"sid-1"}\n```',
    )
    env, sess = ad.invoke(_view(), None)
    # FakeCliAdapter 应记录实际 cwd（属性 last_cwd 由测试双暴露供验证）。
    assert Path(ad.last_cwd) == wt


# —— (a2) 超时 → kill + attempts+1 —— #

def test_fake_cli_adapter_timeout_kills_and_bumps_attempts(tmp_dir):
    """§5.3/§7.2：超时→kill 子进程 + attempts+1。M2 契约 §2 要求测试双能模拟超时。"""
    wt = tmp_dir / "wt-backend"
    wt.mkdir()

    ad = orch.adapters.FakeCliAdapter(
        role="backend", config=_cfg(timeout_s=1),
        worktree=wt,
        # simulate_timeout：假子进程内部 sleep 超过 timeout_s 触发 kill。
        simulate_timeout=True,
    )
    # 契约 §2：超时 kill 后 attempts+1；测试双通过 attempts 属性/异常任一暴露。
    with pytest.raises(TimeoutError):
        ad.invoke(_view(), None)
    # kill 语义可观察（M2 契约 §2）：调用了 kill 且 attempt 计一次。
    assert ad.killed is True
    assert ad.attempts >= 1


# —— (a3) session_id 提取：JSON 字段 —— #

def test_fake_cli_adapter_extracts_session_id_from_json_field_session_id(tmp_dir):
    """§7.2/M2 契约 §2：从输出 JSON 的 session_id 字段提取 sid。"""
    wt = tmp_dir / "wt-pm"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="pm", config=_cfg(),
        worktree=wt,
        scripted_output='some text before\n```json\n'
                        '{"to":["backend"],"type":"assign","body":"go",'
                        '"session_id":"abc-123"}\n```\nafter',
    )
    env, sess = ad.invoke(_view("pm"), None)
    assert sess is not None
    # 契约 §2：sess.sid 存在并为提取值；结构不作细化（允许 dict 或对象但 sid 可读）。
    sid = sess.get("sid") if isinstance(sess, dict) else getattr(sess, "sid", None)
    assert sid == "abc-123"


def test_fake_cli_adapter_extracts_session_id_from_alias_sid_field(tmp_dir):
    """§7.2/M2 契约 §2：JSON 字段名允许 sid 别名（§17 开放决策：缺省从常见字段）。"""
    wt = tmp_dir / "wt-tester"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="tester", config=_cfg(),
        worktree=wt,
        scripted_output='```json\n'
                        '{"to":["moderator"],"type":"acceptance","body":"ok",'
                        '"sid":"S-42"}\n```',
    )
    _, sess = ad.invoke(_view("tester"), None)
    sid = sess.get("sid") if isinstance(sess, dict) else getattr(sess, "sid", None)
    assert sid == "S-42"


def test_fake_cli_adapter_extracts_session_id_via_config_regex(tmp_dir):
    """§17/M2 契约 §2：无 JSON 字段命中时，走 config.session_id_extract 正则兜底。"""
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    cfg = _cfg(session_id_extract=r"session-id:\s*([A-Za-z0-9\-]+)")
    ad = orch.adapters.FakeCliAdapter(
        role="backend", config=cfg,
        worktree=wt,
        # 输出无 session_id / sid / session 字段，但正则可从其他文本提取。
        scripted_output='session-id: xy-99\n```json\n'
                        '{"to":["pm"],"type":"report","body":"done"}\n```',
    )
    _, sess = ad.invoke(_view(), None)
    sid = sess.get("sid") if isinstance(sess, dict) else getattr(sess, "sid", None)
    assert sid == "xy-99"


def test_fake_cli_adapter_no_session_id_leaves_sid_none_and_bumps_gen(tmp_dir):
    """§7.2/M2 契约 §2：无匹配 → sid=None、gen+=1（冷启动代数计一次）。"""
    wt = tmp_dir / "wt-frontend"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="frontend", config=_cfg(),
        worktree=wt,
        # 无任何 session_id 提示（字段与正则均未命中）。
        scripted_output='```json\n{"to":["tester"],"type":"handoff","body":"go"}\n```',
    )
    _, sess = ad.invoke(_view("frontend"), None)
    if sess is None:
        # 契约 §2 允许 sid=None 时 sess 也为 None；此路径下 gen 计量在 invoke 内部更新。
        assert ad.gen >= 1
    else:
        sid = sess.get("sid") if isinstance(sess, dict) else getattr(sess, "sid", None)
        assert sid is None
        gen = (sess.get("gen") if isinstance(sess, dict)
               else getattr(sess, "gen", None))
        assert gen is not None and int(gen) >= 1


# —— (a4) 输出解析：取最后一个 ```json 块 —— #

def test_fake_cli_adapter_takes_last_json_block(tmp_dir):
    """§7.2/§17：CLI 输出解析取**最后一个** ```json 块。前面的块是过程稿，不算作品。"""
    wt = tmp_dir / "wt-pm"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="pm", config=_cfg(),
        worktree=wt,
        scripted_output=(
            "过程稿：\n"
            "```json\n"
            '{"to":["frontend"],"type":"draft","body":"first-draft"}\n'
            "```\n"
            "最终稿：\n"
            "```json\n"
            '{"to":["backend"],"type":"assign","body":"final"}\n'
            "```\n"
        ),
    )
    env, _ = ad.invoke(_view("pm"), None)
    # 最后一个 json 块中的信封才作数；不能取前面的 draft。
    assert env["type"] == "assign"
    assert env["to"] == ["backend"]
    assert env["body"] == "final"


def test_fake_cli_adapter_no_json_block_raises_or_returns_parse_error(tmp_dir):
    """§5.1/§7.2：输出无 ```json 块 → §5.1 原地重调路径（本层抛 ValueError 供调度层处理）。"""
    wt = tmp_dir / "wt-pm"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="pm", config=_cfg(),
        worktree=wt,
        scripted_output="没有任何 json 块，纯自由文本。",
    )
    with pytest.raises((ValueError, RuntimeError)):
        ad.invoke(_view("pm"), None)


# —— (a5) Caps 单元：CliAdapter/FakeCliAdapter 应有 caps 属性 —— #

def test_fake_cli_adapter_exposes_caps_attribute(tmp_dir):
    """§7.1/M2 契约 §2：适配器实例暴露 caps: Caps 属性（供调度层预算 §6.3 与 §8.1 tools 注入）。"""
    wt = tmp_dir / "wt-r"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="backend", config=_cfg(context_window=8000, tools=["Edit"]),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"question","body":"?"}\n```',
    )
    # 契约 §2：caps 属性存在且为 dict（TypedDict 运行时为 dict）。
    assert isinstance(ad.caps, dict)
    for k in ("context_window", "tools", "write_scope", "cost_tier",
              "supports_resume", "timeout_s", "max_concurrent"):
        assert k in ad.caps


def test_cli_adapter_class_exposed_from_package(tmp_dir):
    """§7.2/M2 契约 §2：真实 CliAdapter 骨架符号在包中可导出（构造签名与契约 §2 一致）。

    M2 只做骨架，不实调子进程；这里只断言"符号存在 + 构造签名"，不 invoke。
    """
    wt = tmp_dir / "wt-x"
    wt.mkdir()
    # 未实现 → 触发 AttributeError（顶层已 import，包级）。
    cls = orch.adapters.CliAdapter
    inst = cls(role="backend", config=_cfg(), worktree=wt)
    # 构造完成后 caps 应有值（默认 Caps 或 config 推导）。
    assert isinstance(inst.caps, dict)


# ——————————————————————————————————————————————————————————————
# R-a：§8.1 权限三件套之二"工具白名单：适配器配置注入 CLI 参数"
# 三维评审 C-1：CliAdapter/FakeCliAdapter 必须**自动**根据 caps["tools"] 追加
# --allowedTools 参数，用户无需在 start_cmd 手拼。
# ——————————————————————————————————————————————————————————————

def _cfg_no_hardcoded_tools(**over) -> dict:
    """本组测试专用：start_cmd 不手拼 --allowedTools（验证自动注入）。"""
    base = {
        "kind": "cli",
        "start_cmd": "fake-claude -p",
        "resume_cmd": "fake-claude -p --resume {sid}",
        "timeout_s": 5,
    }
    base.update(over)
    return base


def test_fake_cli_adapter_auto_injects_allowed_tools_flag_spaced_default(tmp_dir):
    """§8.1 权限三件套之二：FakeCliAdapter.invoke 组装 argv 时应**自动**
    根据 caps.tools=["Edit","Write","Bash(pytest:*)"] 追加 --allowedTools 参数。

    缺省风格 spaced：单个 --allowedTools 后跟多个工具名（spec §7.2 示例即此形态）。
    argv 由测试双通过 last_argv 属性暴露。
    """
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    tools = ["Edit", "Write", "Bash(pytest:*)"]
    ad = orch.adapters.FakeCliAdapter(
        role="backend",
        config=_cfg_no_hardcoded_tools(tools=tools),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"question","body":"?"}\n```',
    )
    ad.invoke(_view(), None)
    argv = ad.last_argv
    assert argv is not None, "FakeCliAdapter 应记录组装后的 argv 供测试断言"
    # 自动注入 --allowedTools（不需要 start_cmd 手写）。
    assert "--allowedTools" in argv
    # spaced 风格：--allowedTools 只出现一次，其后紧跟各工具名。
    idx = argv.index("--allowedTools")
    tail = argv[idx + 1 : idx + 1 + len(tools)]
    assert tail == tools, (
        f"spaced 风格下 --allowedTools 后应紧跟工具列表 {tools}，实际 {tail}"
    )
    # 未附加额外 --allowedTools 副本（单 flag 多值）。
    assert argv.count("--allowedTools") == 1


def test_fake_cli_adapter_auto_injects_allowed_tools_flag_joined_style(tmp_dir):
    """§17 开放决策：allowed_tools_style='joined' → 一次一 flag（每个工具单独一个 --allowedTools）。"""
    wt = tmp_dir / "wt-tester"
    wt.mkdir()
    tools = ["Edit", "Bash(pytest:*)"]
    cfg = _cfg_no_hardcoded_tools(tools=tools, allowed_tools_style="joined")
    ad = orch.adapters.FakeCliAdapter(
        role="tester", config=cfg, worktree=wt,
        scripted_output='```json\n{"to":["moderator"],"type":"acceptance","body":"ok"}\n```',
    )
    ad.invoke(_view("tester"), None)
    argv = ad.last_argv
    assert argv is not None
    # joined 风格：--allowedTools 出现 N 次，每次带 1 工具。
    assert argv.count("--allowedTools") == len(tools)
    # 相邻校验：每个 --allowedTools 后紧跟一个工具（顺序保持）。
    positions = [i for i, a in enumerate(argv) if a == "--allowedTools"]
    for pos, tool in zip(positions, tools):
        assert argv[pos + 1] == tool


def test_fake_cli_adapter_custom_allowed_tools_flag_via_config(tmp_dir):
    """§17 开放决策：允许 config.allowed_tools_flag 覆盖默认 flag 名（真实 CLI 差异陪跑）。"""
    wt = tmp_dir / "wt-x"
    wt.mkdir()
    cfg = _cfg_no_hardcoded_tools(tools=["Edit"], allowed_tools_flag="--tools")
    ad = orch.adapters.FakeCliAdapter(
        role="backend", config=cfg, worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"question","body":"?"}\n```',
    )
    ad.invoke(_view(), None)
    argv = ad.last_argv
    assert argv is not None
    assert "--tools" in argv
    # 默认 --allowedTools 不应出现（被 config 覆盖）。
    assert "--allowedTools" not in argv


def test_fake_cli_adapter_no_tools_does_not_inject_flag(tmp_dir):
    """caps.tools 为空时不注入 --allowedTools（避免空 flag 垃圾）。"""
    wt = tmp_dir / "wt-mod"
    wt.mkdir()
    # 无 tools 字段（沿用 _caps_from_config 默认 []）。
    ad = orch.adapters.FakeCliAdapter(
        role="pm", config=_cfg_no_hardcoded_tools(),
        worktree=wt,
        scripted_output='```json\n{"to":["backend"],"type":"assign","body":"go"}\n```',
    )
    ad.invoke(_view("pm"), None)
    argv = ad.last_argv
    assert argv is not None
    assert "--allowedTools" not in argv


def test_fake_cli_adapter_last_argv_places_view_text_last(tmp_dir):
    """§7.2：view['text'] 是最后一个位置参数；自动注入的 --allowedTools 应在其之前。"""
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="backend",
        config=_cfg_no_hardcoded_tools(tools=["Edit", "Write"]),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"question","body":"?"}\n```',
    )
    view_text = "全量视图 payload"
    ad.invoke(_view(text=view_text), None)
    argv = ad.last_argv
    assert argv is not None
    assert argv[-1] == view_text
    # --allowedTools 位于 view_text 之前。
    assert argv.index("--allowedTools") < len(argv) - 1


def test_cli_adapter_real_class_auto_injects_allowed_tools(tmp_dir, monkeypatch):
    """§8.1 R-a：真实 CliAdapter 也必须自动注入 --allowedTools（不仅 FakeCliAdapter）。

    通过 monkeypatch subprocess.Popen 截获真实 argv，避免真启动 CLI 子进程。
    这里断言 CliAdapter 与 FakeCliAdapter 在权限注入上语义一致（骨架层三件套齐）。
    """
    wt = tmp_dir / "wt-y"
    wt.mkdir()

    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = list(argv)
            captured["cwd"] = kw.get("cwd")

        def communicate(self, timeout=None):
            return (
                'here is the reply:\n```json\n'
                '{"to":["pm"],"type":"question","body":"?",'
                '"session_id":"sid-real"}\n```',
                "",
            )

        def kill(self):
            captured["killed"] = True

    monkeypatch.setattr(
        "orch.adapters.subprocess.Popen", _FakeProc
    )

    cls = orch.adapters.CliAdapter
    inst = cls(
        role="backend",
        config=_cfg_no_hardcoded_tools(tools=["Edit", "Write", "Bash(pytest:*)"]),
        worktree=wt,
    )
    env, sess = inst.invoke(_view(), None)
    argv = captured.get("argv")
    assert argv is not None, "CliAdapter 应把 argv 传给 subprocess.Popen"
    assert "--allowedTools" in argv
    # 默认 spaced：--allowedTools 出现一次，其后跟三个工具。
    idx = argv.index("--allowedTools")
    assert argv[idx + 1 : idx + 4] == ["Edit", "Write", "Bash(pytest:*)"]
    # view['text'] 仍是最后一个位置参数。
    assert argv[-1] == "hello view"
    # cwd 仍是 worktree。
    assert Path(captured.get("cwd", "")) == wt


def test_cli_adapter_real_class_no_tools_no_flag(tmp_dir, monkeypatch):
    """§8.1 R-a：真实 CliAdapter tools 空时不注入 flag（对称 FakeCliAdapter）。"""
    wt = tmp_dir / "wt-z"
    wt.mkdir()

    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = list(argv)

        def communicate(self, timeout=None):
            return (
                '```json\n{"to":["pm"],"type":"question","body":"?"}\n```',
                "",
            )

        def kill(self):
            pass

    monkeypatch.setattr("orch.adapters.subprocess.Popen", _FakeProc)
    inst = orch.adapters.CliAdapter(
        role="backend", config=_cfg_no_hardcoded_tools(), worktree=wt,
    )
    inst.invoke(_view(), None)
    argv = captured.get("argv")
    assert argv is not None
    assert "--allowedTools" not in argv


# ——————————————————————————————————————————————————————————————
# Q1 陪跑接入：真实 CLI 输出解包（stream-json / json / text）
# 样例形状取自 kimi.exe / claude 的实测输出（不真调 CLI，避免测试计费）。
# ——————————————————————————————————————————————————————————————

def test_unwrap_stream_json_kimi_shape():
    """kimi -p --output-format stream-json：逐行 JSON，assistant.content 含信封块，
    session_id 在 session.resume_hint 行（Q1 实测形状）。"""
    import json as _json
    from orch.adapters import _unwrap_agent_output, _extract_last_json_block
    line1 = _json.dumps({
        "role": "assistant",
        "content": '```json\n{"to":["moderator"],"type":"report","body":"ok"}\n```',
    })
    line2 = _json.dumps({
        "role": "meta", "type": "session.resume_hint",
        "session_id": "session_abc123",
    })
    stdout = line1 + "\n" + line2
    text, sid = _unwrap_agent_output(stdout, {"wire_format": "stream-json"})
    assert sid == "session_abc123"
    env = _json.loads(_extract_last_json_block(text))
    assert env["to"] == ["moderator"]
    assert env["type"] == "report"


def test_unwrap_json_result_claude_shape():
    """claude -p --output-format json：单 JSON，result 是回复文本，session_id 顶层字段。"""
    import json as _json
    from orch.adapters import _unwrap_agent_output, _extract_last_json_block
    stdout = _json.dumps({
        "type": "result",
        "result": '方案如下\n```json\n{"to":["pm"],"type":"chat","body":"hi"}\n```',
        "session_id": "11111111-2222-4333-8444-555566667777",
    })
    text, sid = _unwrap_agent_output(stdout, {"wire_format": "json"})
    assert sid == "11111111-2222-4333-8444-555566667777"
    env = _json.loads(_extract_last_json_block(text))
    assert env["type"] == "chat"


def test_unwrap_text_mode_backward_compatible():
    """text（默认）：整段即回复，sid_hint=None（交 _extract_sid 兜底，M2 既有语义不变）。"""
    from orch.adapters import _unwrap_agent_output
    raw = '```json\n{"to":["x"],"type":"chat","body":"y"}\n```'
    text, sid = _unwrap_agent_output(raw, {})
    assert text == raw
    assert sid is None


def test_unwrap_json_grok_shape():
    """grok -p --output-format json（0.2.112 陪跑实测 2026-07-25）：单 JSON，
    回复文本在 text 字段、会话号在 sessionId——与 claude 的 result/session_id
    同构异名，"json" 分支须两组键名都认。"""
    import json as _json
    from orch.adapters import _unwrap_agent_output, _extract_last_json_block
    stdout = _json.dumps({
        "text": '收到\n```json\n{"to":["pm"],"type":"chat","body":"hi"}\n```',
        "stopReason": "EndTurn",
        "sessionId": "019f98e7-3524-7cc2-8b72-c7bfaa27149c",
        "usage": {"input_tokens": 12062, "output_tokens": 22},
    })
    text, sid = _unwrap_agent_output(stdout, {"wire_format": "json"})
    assert sid == "019f98e7-3524-7cc2-8b72-c7bfaa27149c"
    env = _json.loads(_extract_last_json_block(text))
    assert env["type"] == "chat"


def test_unwrap_opencode_stream_shape():
    """opencode run --format json（1.18.4 陪跑实测 2026-07-25）：JSON 行事件流，
    type=="text" 事件的 part.text 依序拼接为回复；sessionID 任一行顶层，取首见。"""
    import json as _json
    from orch.adapters import _unwrap_agent_output, _extract_last_json_block
    sid = "ses_067183819ffegq7nYvGPABOTKF"
    lines = [
        _json.dumps({"type": "step_start", "timestamp": 1, "sessionID": sid,
                     "part": {"type": "step-start"}}),
        _json.dumps({"type": "text", "timestamp": 2, "sessionID": sid,
                     "part": {"type": "text", "text": "前半 "}}),
        _json.dumps({"type": "text", "timestamp": 3, "sessionID": sid,
                     "part": {"type": "text",
                              "text": '```json\n{"to":["pm"],"type":"report","body":"ok"}\n```'}}),
        _json.dumps({"type": "step_finish", "timestamp": 4, "sessionID": sid,
                     "part": {"type": "step-finish", "reason": "stop"}}),
    ]
    text, got_sid = _unwrap_agent_output(
        "\n".join(lines), {"wire_format": "opencode-stream"})
    assert got_sid == sid
    env = _json.loads(_extract_last_json_block(text))
    assert env["type"] == "report"


def test_unwrap_opencode_stream_skips_non_text_events():
    """opencode-stream：非 text 事件（step/tool 等）不得混入回复文本；sid 照常提取。"""
    import json as _json
    from orch.adapters import _unwrap_agent_output
    lines = [
        _json.dumps({"type": "step_start", "sessionID": "ses_x",
                     "part": {"type": "step-start"}}),
        _json.dumps({"type": "tool", "sessionID": "ses_x",
                     "part": {"type": "tool", "text": "不该出现"}}),
    ]
    text, sid = _unwrap_agent_output("\n".join(lines), {"wire_format": "opencode-stream"})
    assert text == ""
    assert sid == "ses_x"


def test_build_adapters_from_config_cli_kind():
    """真实装配：config kind=cli → CliAdapter，role 层字段覆盖 adapter 层（Q1/Q2 陪跑）。"""
    from orch.cli.main import _build_adapters_from_config
    import orch.adapters as _ad
    config = {
        "adapters": {"kimi_cli": {"kind": "cli", "start_cmd": "kimi -p",
                                  "wire_format": "stream-json"}},
        "roles": {"backend": {"adapter": "kimi_cli", "can_decide": False,
                              "write_scope": []}},
    }
    ads = _build_adapters_from_config(["backend"], config, Path("."))
    assert isinstance(ads["backend"], _ad.CliAdapter)
    assert ads["backend"].config.get("wire_format") == "stream-json"


def test_build_adapters_from_config_rejects_non_cli():
    """真实装配暂只支持 kind=cli；其它 kind 显式报错，不臆造后端（诚实边界）。"""
    from orch.cli.main import _build_adapters_from_config
    config = {"adapters": {"a": {"kind": "api"}}, "roles": {"m": {"adapter": "a"}}}
    with pytest.raises(ValueError):
        _build_adapters_from_config(["m"], config, Path("."))


# ——————————————————————————————————————————————————————————————
# T-CWD：start_cmd 支持字面量 {cwd} 占位（token 级替换为本次调用 worktree）
#
# 动机（陪跑实测 2026-07-25）：opencode 无视进程 cwd、自寻项目根，必须靠
# `--dir <worktree>` 显式压制；而 worktree 路径只有编排器在运行期才知道，
# 配置里写不出来。故 start_cmd 允许写 `oc run --dir {cwd}`。
#
# 冻结设计（三条硬约束，测试逐条钉死）：
#   1) 先 .split() 再逐 token 做 str.replace —— 含空格路径天然仍是**单个** argv
#      元素，无需任何引号/转义逻辑；
#   2) 无占位时逐字节回归原状（str.replace 未命中返回等值原串）；
#   3) 作用域仅限 start_cmd 分词产物：不碰 tools_args，更不碰 view['text']
#      （正文参与替换 = 给 agent 开模板注入面）。
# 两处孪生（CliAdapter / FakeCliAdapter）必须同修同字，否则 last_argv 假绿。
# ——————————————————————————————————————————————————————————————

def test_fake_cli_adapter_start_cmd_cwd_placeholder_replaced_with_worktree(tmp_dir):
    """{cwd} 被替换为该次调用的 worktree 绝对路径（argv 里不得残留字面占位）。"""
    wt = tmp_dir / "wt-tester"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="tester",
        config=_cfg_no_hardcoded_tools(
            start_cmd="oc run --dir {cwd} --format json"),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"report","body":"ok"}\n```',
    )
    ad.invoke(_view("tester"), None)
    argv = ad.last_argv
    assert argv is not None
    # (a) 任何元素都不得残留字面 "{cwd}"。
    assert not any("{cwd}" in a for a in argv), f"argv 残留未替换占位: {argv}"
    # (b) --dir 的取值就是 worktree。
    assert argv[argv.index("--dir") + 1] == str(wt)


def test_fake_cli_adapter_no_placeholder_argv_byte_identical_regression(tmp_dir):
    """回归护栏：start_cmd 不含占位时，argv 与改造前逐元素逐字节一致。"""
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="backend",
        config=_cfg_no_hardcoded_tools(tools=["Edit", "Write"]),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"question","body":"?"}\n```',
    )
    ad.invoke(_view(text="hello view"), None)
    assert ad.last_argv == [
        "fake-claude", "-p", "--allowedTools", "Edit", "Write", "hello view",
    ]


def test_fake_cli_adapter_cwd_with_spaces_stays_single_argv_element(tmp_dir):
    """本卡核心防裂断言：含空格路径替换后仍是**单个** argv 元素，token 数不增。"""
    wt = tmp_dir / "有 空 格" / "wt-tester"
    wt.mkdir(parents=True)
    ad = orch.adapters.FakeCliAdapter(
        role="tester",
        config=_cfg_no_hardcoded_tools(start_cmd="oc run --dir {cwd}"),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"report","body":"ok"}\n```',
    )
    ad.invoke(_view("tester"), None)
    argv = ad.last_argv
    assert argv is not None
    # (a) token 数与替换前相同：oc / run / --dir / <路径> / view_text。
    assert len(argv) == 5, f"含空格路径被拆裂: {argv}"
    # (b) --dir 取值 == 完整路径且确实含空格。
    val = argv[argv.index("--dir") + 1]
    assert val == str(wt)
    assert " " in val


def test_cli_adapter_real_class_replaces_cwd_placeholder(tmp_dir, monkeypatch):
    """防"只改孪生不改真身"：真实 CliAdapter 同样替换 {cwd}，且 Popen 的 cwd kwarg 不变。"""
    wt = tmp_dir / "wt-real"
    wt.mkdir()
    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = list(argv)
            captured["cwd"] = kw.get("cwd")

        def communicate(self, timeout=None):
            return ('```json\n{"to":["pm"],"type":"chat","body":"hi"}\n```', "")

        def kill(self):
            pass

    monkeypatch.setattr("orch.adapters.subprocess.Popen", _FakeProc)
    inst = orch.adapters.CliAdapter(
        role="tester",
        config=_cfg_no_hardcoded_tools(start_cmd="oc run --dir {cwd} --format json"),
        worktree=wt,
    )
    inst.invoke(_view("tester"), None)
    argv = captured.get("argv")
    assert argv is not None
    assert not any("{cwd}" in a for a in argv), f"真身 argv 残留未替换占位: {argv}"
    assert argv[argv.index("--dir") + 1] == str(wt)
    # 新增替换不得改动既有 cwd 行为（§7.2：子进程 cwd 仍是 worktree）。
    assert captured.get("cwd") == str(wt)


def test_fake_and_real_argv_twins_agree_on_cwd_placeholder(tmp_dir, monkeypatch):
    """孪生一致性护栏：同 config + 同 worktree 下两处 argv 逐元素相等。"""
    wt = tmp_dir / "wt-twin"
    wt.mkdir()
    cfg = _cfg_no_hardcoded_tools(
        start_cmd="oc run --dir {cwd}", tools=["Edit", "Write"])
    view = _view("tester", text="twin view")
    reply = '```json\n{"to":["pm"],"type":"chat","body":"hi"}\n```'

    fake = orch.adapters.FakeCliAdapter(
        role="tester", config=cfg, worktree=wt, scripted_output=reply)
    fake.invoke(view, None)

    captured = {}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = list(argv)

        def communicate(self, timeout=None):
            return (reply, "")

        def kill(self):
            pass

    monkeypatch.setattr("orch.adapters.subprocess.Popen", _FakeProc)
    orch.adapters.CliAdapter(
        role="tester", config=cfg, worktree=wt).invoke(view, None)

    assert fake.last_argv == captured.get("argv")


def test_cwd_placeholder_not_applied_to_view_text(tmp_dir):
    """作用域最小化：view['text'] 正文里的 {cwd} 原样保留（不给 agent 开模板注入面）。"""
    wt = tmp_dir / "wt-noinject"
    wt.mkdir()
    text = "请解释配置里 {cwd} 这个占位的含义"
    ad = orch.adapters.FakeCliAdapter(
        role="tester",
        config=_cfg_no_hardcoded_tools(),  # start_cmd 无占位
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"chat","body":"hi"}\n```',
    )
    ad.invoke(_view("tester", text=text), None)
    assert ad.last_argv[-1] == text
    assert "{cwd}" in ad.last_argv[-1]


def test_cwd_placeholder_supports_equals_form(tmp_dir):
    """等号式 `--dir={cwd}` 与分离式同样支持，且仍是单 token。"""
    wt = tmp_dir / "wt-eq"
    wt.mkdir()
    ad = orch.adapters.FakeCliAdapter(
        role="tester",
        config=_cfg_no_hardcoded_tools(start_cmd="oc run --dir={cwd}"),
        worktree=wt,
        scripted_output='```json\n{"to":["pm"],"type":"report","body":"ok"}\n```',
    )
    ad.invoke(_view("tester"), None)
    argv = ad.last_argv
    assert argv is not None
    assert argv.count("--dir=" + str(wt)) == 1
    assert not any("{cwd}" in a for a in argv)


# ——————————————————————————————————————————————————————————————
# T-CWD 附带缺口：§8.3 verify 钩子的 cwd 占位渲染（{worktree:role} / {target_repo}）
#
# 本组测的是 orch.scheduler.core，本应另起测试文件；受本卡可写路径白名单
# （只含 tests/test_cli_adapter.py）限制暂寄存于此，Lead 可后续搬迁。
#
# 缺口现状（裁决实测）：core._run_verify 只读 verify['cwd'] 且**不做任何占位渲染**，
# 于是：
#   - 按 §11.1:541 原文写 cwd:"{worktree:backend}" → 占位被当成真实目录名 →
#     NotADirectoryError → exit_code=1 → acceptance 100% 恒降级 report；
#   - 按 §8.3:450 原文写 cwd_template → 该键根本不被读取 → 兜底 cwd="." →
#     验证在编排器自身进程目录里跑、可能返回 0 → **假绿**（更危险）。
# ——————————————————————————————————————————————————————————————

# 无害且跨平台的验证命令：在**当前工作目录**留下一个标记文件。
# 用落盘标记而非打印 cwd 做断言，避开 Windows cp936 管道解码 CJK 路径的噪声。
_MARK = "verify_ran.txt"
_MARK_CMD = 'python -c "open(\'%s\',\'w\').close()"' % _MARK


def test_run_verify_renders_worktree_role_placeholder_in_cwd(tmp_dir):
    """§11.1:541 原文形态 cwd:"{worktree:backend}" → 渲染为该角色 worktree 并在其中执行。"""
    from orch.scheduler.core import _run_verify
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    config = {
        "worktrees": {"backend": str(wt)},
        "roles": {"tester": {"verify": {
            "cmd": _MARK_CMD, "cwd": "{worktree:backend}"}}},
    }
    res = _run_verify(config, "tester")
    assert res is not None
    assert res["exit_code"] == 0, res
    # 硬证据：命令确实在渲染后的 worktree 里执行（而非编排器自身 cwd）。
    assert (wt / _MARK).exists(), f"verify 未在 {wt} 执行：{res}"


def test_run_verify_accepts_spec_8_3_cwd_template_field(tmp_dir):
    """§8.3:450 原文字段名 cwd_template 必须被读取（否则静默兜底 '.' → 假绿）。"""
    from orch.scheduler.core import _run_verify
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    config = {
        "worktrees": {"backend": str(wt)},
        "roles": {"tester": {"verify": {
            "cmd": _MARK_CMD, "cwd_template": "{worktree:backend}"}}},
    }
    res = _run_verify(config, "tester")
    assert res is not None
    assert res["exit_code"] == 0, res
    assert (wt / _MARK).exists(), f"cwd_template 被忽略，verify 跑错目录：{res}"


def test_run_verify_renders_target_repo_placeholder(tmp_dir):
    """§8.3:450 第二个占位 {target_repo} 同样渲染。"""
    from orch.scheduler.core import _run_verify
    repo = tmp_dir / "repo"
    repo.mkdir()
    config = {
        "target_repo": str(repo),
        "roles": {"tester": {"verify": {
            "cmd": _MARK_CMD, "cwd_template": "{target_repo}"}}},
    }
    res = _run_verify(config, "tester")
    assert res is not None
    assert res["exit_code"] == 0, res
    assert (repo / _MARK).exists(), f"{{target_repo}} 未渲染：{res}"


def test_run_verify_unresolved_placeholder_fails_closed_with_diagnosable_output(tmp_dir):
    """占位解析不了必须 fail-closed：非 0 退出码 + 输出点名那个占位，且不执行命令。

    反面教材是静默兜底 '.'——那会让验收证据来自错误目录。
    """
    from orch.scheduler.core import _run_verify
    config = {"roles": {"tester": {"verify": {
        "cmd": _MARK_CMD, "cwd_template": "{worktree:nosuch}"}}}}
    res = _run_verify(config, "tester")
    assert res is not None
    assert res["exit_code"] != 0
    # 报错要可诊断：点名未解析的占位本身，而不是一句 NotADirectoryError。
    assert "{worktree:nosuch}" in res["output"], res["output"]
    # 命令不得被执行（编排器自身 cwd 不该多出标记文件）。
    assert not (Path.cwd() / _MARK).exists()


def test_run_verify_plain_cwd_unchanged_regression(tmp_dir):
    """回归护栏：不含占位的 cwd（M0 fixture 形态 '.'）行为逐字不变。"""
    from orch.scheduler.core import _run_verify
    config = {"roles": {"tester": {"verify": {
        "cmd": 'python -c "print(1)"', "cwd": "."}}}}
    res = _run_verify(config, "tester")
    assert res is not None
    assert res["exit_code"] == 0
    assert "1" in res["output"]


def test_finalize_envelope_acceptance_survives_spec_11_1_config(tmp_dir):
    """演示链端到端：按 §11.1:541 原文配置发 acceptance，type 必须**保持** acceptance。

    这是"验收钩子看起来没生效"的真正判据——占位渲染缺失时本用例恒红（被降级为 report）。
    """
    from orch.scheduler.core import _finalize_envelope
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    config = {
        "worktrees": {"backend": str(wt)},
        "roles": {"tester": {"verify": {
            "cmd": 'python -c "raise SystemExit(0)"',
            "cwd": "{worktree:backend}"}}},
    }
    env = {"to": ["moderator"], "type": "acceptance", "body": "我测过了"}
    out = _finalize_envelope(None, config, "tester", env)
    assert out["meta"]["verify"]["exit_code"] == 0, out
    assert out["type"] == "acceptance", out


def test_finalize_envelope_acceptance_degrades_when_verify_fails(tmp_dir):
    """§8.3 反向护栏：渲染成功但命令退出码非 0 → 仍降级 report（不得被本卡改坏）。"""
    from orch.scheduler.core import _finalize_envelope
    wt = tmp_dir / "wt-backend"
    wt.mkdir()
    config = {
        "worktrees": {"backend": str(wt)},
        "roles": {"tester": {"verify": {
            "cmd": 'python -c "raise SystemExit(3)"',
            "cwd_template": "{worktree:backend}"}}},
    }
    env = {"to": ["moderator"], "type": "acceptance", "body": "我测过了"}
    out = _finalize_envelope(None, config, "tester", env)
    assert out["meta"]["verify"]["exit_code"] == 3, out
    assert out["type"] == "report", out
