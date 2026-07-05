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
