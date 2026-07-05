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
