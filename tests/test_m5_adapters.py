"""M5 T4 · 适配层错误分类 + MockAdapter 额度故障/调用序查表（spec §5.6.3、§7.6、§7.4）。

契约：docs/m5-contract.md §2（符号一字为准）
  - `AdapterUnavailableError(Exception)`，属性 adapter_name / detail；
  - CliAdapter / ApiAdapter 仅在**传输级失败**（超时 / 进程失败 / 无输出）时，
    把可得的 stderr / 退出信息 / 错误文本与该 adapter 的 unavailable_patterns
    （缺省 state.DEFAULT_UNAVAILABLE_PATTERNS）做**大小写不敏感子串**匹配：
    命中 → raise AdapterUnavailableError；未命中 → 既有失败路径逐字不变；
  - schema 层非法信封**不**分类（§5.6.3："那是输出质量问题不是可用性问题"）；
  - MockAdapter 增 `unavailable_after` / `unavailable_text` / `key_by`。

硬约束（沿 M0/M2 测试惯例）：
  - 顶层只 `import orch.adapters`（包级导入），被测符号在函数体内引用，
    使未实现表现为**运行时红**（AttributeError）而非 collection 中断；
  - 不启动任何真实子进程、不打真实网络：CLI 走 monkeypatch 假 Popen，
    API 走可注入 message_fn。
"""

from __future__ import annotations

import subprocess

import pytest

import orch.adapters  # 包级导入（未实现符号在函数体内引用）
from orch.adapters.state import DEFAULT_UNAVAILABLE_PATTERNS


# ——————————————————————————————————————————————————————————————
# 夹具与假件
# ——————————————————————————————————————————————————————————————

def _cli_cfg(**over) -> dict:
    """CliAdapter 构造用最小 config（§11.1 子集）。"""
    base = {
        "kind": "cli",
        "start_cmd": "fake-claude -p",
        "timeout_s": 5,
    }
    base.update(over)
    return base


def _api_cfg(**over) -> dict:
    base = {"kind": "api", "model": "fake-model", "timeout_s": 30}
    base.update(over)
    return base


def _view(role: str = "backend", event_ids: list[int] | None = None,
          text: str = "hello view") -> dict:
    return {
        "role": role,
        "event_ids": list(event_ids or [1]),
        "text": text,
        "sections": {},
        "meta": {},
    }


_OK_STDOUT = '```json\n{"to":["pm"],"type":"question","body":"?"}\n```'


def _install_fake_popen(
    monkeypatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    simulate_timeout: bool = False,
    after_kill: tuple[str, str] = ("", ""),
) -> dict:
    """装一个假 subprocess.Popen（绝不启真实进程）。

    simulate_timeout=True：带 timeout 参数的首次 communicate 抛 TimeoutExpired；
    之后（kill 后的排空读取，无 timeout 参数）返回 after_kill。
    """
    captured: dict = {"killed": False}

    class _FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = list(argv)
            captured["cwd"] = kw.get("cwd")
            self.returncode = returncode

        def communicate(self, input=None, timeout=None):  # noqa: A002 — 对齐真实签名
            if simulate_timeout:
                if timeout is not None:
                    raise subprocess.TimeoutExpired(captured["argv"], timeout)
                return after_kill
            return (stdout, stderr)

        def kill(self):
            captured["killed"] = True

    monkeypatch.setattr("orch.adapters.subprocess.Popen", _FakeProc)
    return captured


# ——————————————————————————————————————————————————————————————
# ⑧ AdapterUnavailableError 属性齐全（契约 §2）
# ——————————————————————————————————————————————————————————————

def test_adapter_unavailable_error_has_name_and_detail():
    """契约 §2：AdapterUnavailableError(Exception)，属性 adapter_name / detail。"""
    err = orch.adapters.AdapterUnavailableError("cli_a", "429 rate limit exceeded")
    assert isinstance(err, Exception)
    assert err.adapter_name == "cli_a"
    assert err.detail == "429 rate limit exceeded"
    # 人话消息里能看到是哪个 adapter 与原始摘要（跳闸审计事件 body 的素材，契约 §4）。
    assert "cli_a" in str(err)
    assert "429" in str(err)


def test_adapter_unavailable_error_is_catchable_as_exception():
    """调度层用 except AdapterUnavailableError 捕获；不得是 BaseException 旁支。"""
    cls = orch.adapters.AdapterUnavailableError
    with pytest.raises(cls):
        raise cls("mod_api", "quota exhausted")


# ——————————————————————————————————————————————————————————————
# ① CLI 传输失败命中 pattern → 抛（含大小写不敏感、中文"额度"）
# ——————————————————————————————————————————————————————————————

def test_cli_process_failure_with_quota_stderr_raises_unavailable(tmp_dir, monkeypatch):
    """§5.6.3 第1条：进程失败（无输出）+ stderr 命中特征 → AdapterUnavailableError。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(
        monkeypatch,
        stdout="",
        stderr="API Error: insufficient quota for organization org-1",
        returncode=1,
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei:
        ad.invoke(_view(), None)
    err = ei.value
    assert err.adapter_name, "契约 §2：须带触发的 adapter 配置名"
    assert "quota" in err.detail.lower(), err.detail


def test_cli_pattern_match_is_case_insensitive(tmp_dir, monkeypatch):
    """§5.6.3：大小写不敏感子串匹配（默认清单是小写，报错文本常是大写/混合）。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(
        monkeypatch,
        stdout="",
        stderr="FATAL: QUOTA EXCEEDED — Rate Limit reached",
        returncode=2,
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(orch.adapters.AdapterUnavailableError):
        ad.invoke(_view(), None)


def test_cli_pattern_match_hits_chinese_quota_text(tmp_dir, monkeypatch):
    """默认清单含中文"额度"（契约 §1 常量）：中文报错同样命中。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(
        monkeypatch,
        stdout="",
        stderr="调用失败：本月额度已用尽，请稍后再试",
        returncode=1,
    )
    ad = orch.adapters.CliAdapter(role="pm", config=_cli_cfg(), worktree=wt)
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei:
        ad.invoke(_view("pm"), None)
    assert "额度" in ei.value.detail, ei.value.detail


@pytest.mark.parametrize("pattern", list(DEFAULT_UNAVAILABLE_PATTERNS))
def test_cli_every_default_pattern_trips(tmp_dir, monkeypatch, pattern):
    """复用 state.DEFAULT_UNAVAILABLE_PATTERNS（契约 §1 常量，禁止重复定义一份）。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(
        monkeypatch, stdout="", stderr=f"backend said: {pattern} ...", returncode=1,
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(orch.adapters.AdapterUnavailableError):
        ad.invoke(_view(), None)


def test_cli_config_unavailable_patterns_override_defaults(tmp_dir, monkeypatch):
    """契约 §2：adapter 级 `unavailable_patterns` 覆盖默认清单（配置说了算）。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    cfg = _cli_cfg(unavailable_patterns=["balance depleted"])
    # (a) 自定义特征命中 → 抛额度类。
    _install_fake_popen(
        monkeypatch, stdout="", stderr="ERR Balance Depleted (acct 9)", returncode=1,
    )
    ad = orch.adapters.CliAdapter(role="backend", config=cfg, worktree=wt)
    with pytest.raises(orch.adapters.AdapterUnavailableError):
        ad.invoke(_view(), None)
    # (b) 覆盖后默认清单不再生效 → 走既有失败路径（ValueError），不误分类。
    _install_fake_popen(
        monkeypatch, stdout="", stderr="quota exceeded", returncode=1,
    )
    ad2 = orch.adapters.CliAdapter(role="backend", config=cfg, worktree=wt)
    with pytest.raises(ValueError):
        ad2.invoke(_view(), None)


def test_cli_adapter_name_comes_from_config_key_and_can_be_overridden(tmp_dir, monkeypatch):
    """契约 §2：adapter_name 来自 config 里的键名（roles[role].adapter），可显式覆盖。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(monkeypatch, stdout="", stderr="quota", returncode=1)
    # (a) config 带 adapter 键（_build_adapters_from_config 的 merged 形态）。
    ad = orch.adapters.CliAdapter(
        role="backend", config=_cli_cfg(adapter="kimi_cli"), worktree=wt,
    )
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei:
        ad.invoke(_view(), None)
    assert ei.value.adapter_name == "kimi_cli"
    # (b) 显式 adapter_name 参数优先。
    ad2 = orch.adapters.CliAdapter(
        role="backend", config=_cli_cfg(adapter="kimi_cli"), worktree=wt,
        adapter_name="cli_b",
    )
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei2:
        ad2.invoke(_view(), None)
    assert ei2.value.adapter_name == "cli_b"
    # (c) 都没有 → 角色名兜底（与 resolve_effective_adapter 主绑定同一约定）。
    ad3 = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei3:
        ad3.invoke(_view(), None)
    assert ei3.value.adapter_name == "backend"


# ——————————————————————————————————————————————————————————————
# ② CLI 未命中 → 既有异常/失败路径不变
# ——————————————————————————————————————————————————————————————

def test_cli_transport_failure_without_pattern_keeps_valueerror(tmp_dir, monkeypatch):
    """未命中特征 → 既有 §5.1 原地重调路径（ValueError）逐字不变，不得改抛新异常。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(
        monkeypatch,
        stdout="纯自由文本，没有 json 块",
        stderr="connection reset by peer",
        returncode=1,
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(ValueError) as ei:
        ad.invoke(_view(), None)
    assert not isinstance(ei.value, orch.adapters.AdapterUnavailableError)
    assert "no ```json block in stdout" in str(ei.value)


def test_cli_bad_json_block_is_not_classified(tmp_dir, monkeypatch):
    """§5.6.3：schema/内容层问题不属传输级——json 块存在但内容非法 → 既有 ValueError。

    即便 stderr 同时带额度字样也不分类（有输出 = 后端能说话，不是额度不可用）。
    """
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(
        monkeypatch,
        stdout="```json\n{不是合法 JSON}\n```",
        stderr="warning: quota is low",
        returncode=0,
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(ValueError) as ei:
        ad.invoke(_view(), None)
    assert not isinstance(ei.value, orch.adapters.AdapterUnavailableError)
    assert "JSON decode failed" in str(ei.value)


# ——————————————————————————————————————————————————————————————
# ③ 成功路径不受影响
# ——————————————————————————————————————————————————————————————

def test_cli_success_path_unaffected_even_with_quota_word_in_stderr(tmp_dir, monkeypatch):
    """成功 invoke（取到最后一个 json 块）→ 一律照常返回；分类只在失败分支生效。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    _install_fake_popen(
        monkeypatch,
        stdout=_OK_STDOUT,
        stderr="notice: 剩余额度 12%",   # 含特征词，但本次 invoke 成功 → 不得抛
        returncode=0,
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    env, sess = ad.invoke(_view(), None)
    assert env == {"to": ["pm"], "type": "question", "body": "?"}
    assert sess is not None and sess["gen"] == 1


# ——————————————————————————————————————————————————————————————
# ④ 超时：无文本 → 不抛额度类（走既有 TimeoutError）；有文本命中 → 抛
# ——————————————————————————————————————————————————————————————

def test_cli_timeout_without_text_still_raises_timeout_error(tmp_dir, monkeypatch):
    """§5.3/§7.2：超时且无任何可得报错文本 → 既有 TimeoutError + kill，不分类。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    cap = _install_fake_popen(
        monkeypatch, simulate_timeout=True, after_kill=("", ""),
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(TimeoutError) as ei:
        ad.invoke(_view(), None)
    assert not isinstance(ei.value, orch.adapters.AdapterUnavailableError)
    assert cap["killed"] is True, "§7.2 超时必须 kill 子进程"


def test_cli_timeout_with_quota_text_raises_unavailable(tmp_dir, monkeypatch):
    """超时但 kill 后排空读到额度类报错 → 归为额度类（§5.6.3 第1条优先于纯超时）。"""
    wt = tmp_dir / "wt"
    wt.mkdir()
    cap = _install_fake_popen(
        monkeypatch,
        simulate_timeout=True,
        after_kill=("", "HTTP 429 Too Many Requests: rate limit"),
    )
    ad = orch.adapters.CliAdapter(role="backend", config=_cli_cfg(), worktree=wt)
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei:
        ad.invoke(_view(), None)
    assert "429" in ei.value.detail, ei.value.detail
    assert cap["killed"] is True, "分类不得吞掉 kill 语义"


# ——————————————————————————————————————————————————————————————
# ⑤ ApiAdapter 同 ①②
# ——————————————————————————————————————————————————————————————

def test_api_transport_failure_with_pattern_raises_unavailable():
    """§7.3 + §5.6.3：message_fn 抛传输级错误且文本命中 → AdapterUnavailableError。"""
    def _boom(view, config):
        raise RuntimeError("HTTP 429: rate limit exceeded, retry later")

    ad = orch.adapters.ApiAdapter(
        role="moderator", config=_api_cfg(adapter="mod_api"), message_fn=_boom,
    )
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei:
        ad.invoke(_view("moderator"), None)
    assert ei.value.adapter_name == "mod_api"
    assert "429" in ei.value.detail or "rate limit" in ei.value.detail.lower()


def test_api_transport_failure_without_pattern_reraises_original():
    """未命中 → 既有失败路径逐字不变：原异常原样上抛（同一实例，不包装）。"""
    original = RuntimeError("connection reset by peer")

    def _boom(view, config):
        raise original

    ad = orch.adapters.ApiAdapter(
        role="moderator", config=_api_cfg(), message_fn=_boom,
    )
    with pytest.raises(RuntimeError) as ei:
        ad.invoke(_view("moderator"), None)
    assert ei.value is original


def test_api_success_path_unaffected():
    """成功路径：仍只留作者字段、sess 恒 None（§7.3 无会话）。"""
    def _ok(view, config):
        return {"to": ["pm"], "type": "assign", "body": "go", "id": 7, "from": "x"}

    ad = orch.adapters.ApiAdapter(
        role="moderator", config=_api_cfg(), message_fn=_ok,
    )
    env, sess = ad.invoke(_view("moderator"), None)
    assert env == {"to": ["pm"], "type": "assign", "body": "go"}
    assert sess is None


def test_api_without_message_fn_still_not_implemented():
    """M2 边界不变：未注入 message_fn → NotImplementedError（不得被分类逻辑改写）。"""
    ad = orch.adapters.ApiAdapter(role="moderator", config=_api_cfg())
    with pytest.raises(NotImplementedError):
        ad.invoke(_view("moderator"), None)


def test_api_config_patterns_override_defaults():
    """契约 §2：API 型同样读 adapter 级 unavailable_patterns。"""
    def _boom(view, config):
        raise RuntimeError("upstream said: no credits left")

    ad = orch.adapters.ApiAdapter(
        role="moderator",
        config=_api_cfg(unavailable_patterns=["no credits"]),
        message_fn=_boom,
    )
    with pytest.raises(orch.adapters.AdapterUnavailableError):
        ad.invoke(_view("moderator"), None)


# ——————————————————————————————————————————————————————————————
# ⑥ MockAdapter unavailable_after / unavailable_text（契约 §2）
# ——————————————————————————————————————————————————————————————

def _mock(tmp_dir, script: dict, **over):
    kwargs = dict(role="pm", script=script, ledger_path=tmp_dir / "ledger.txt")
    kwargs.update(over)
    return orch.adapters.MockAdapter(**kwargs)


def _ledger_lines(tmp_dir) -> list[str]:
    p = tmp_dir / "ledger.txt"
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines()


def test_mock_unavailable_after_raises_from_kth_invoke(tmp_dir):
    """契约 §2：第 k 次 invoke 起（含该次）恒抛；此前行为与 ledger 语义不变。"""
    env1 = {"to": ["moderator"], "type": "handoff", "body": "one"}
    ad = _mock(tmp_dir, {1: env1, 2: dict(env1, body="two")}, unavailable_after=2)

    # 第 k-1 次：正常返回 + ledger 记一行。
    got, sess = ad.invoke(_view("pm", [1]), None)
    assert got == env1
    assert sess is None
    assert _ledger_lines(tmp_dir) == ["pm:1"]

    # 第 k 次与其后：恒抛，且**不**记 ledger（未处理的调用不留副作用）。
    for evt in (2, 3):
        with pytest.raises(orch.adapters.AdapterUnavailableError):
            ad.invoke(_view("pm", [evt]), None)
    assert _ledger_lines(tmp_dir) == ["pm:1"], "被抛的调用不得写 ledger"


def test_mock_unavailable_text_becomes_detail(tmp_dir):
    """契约 §2：unavailable_text 缺省 'quota exceeded (mock)'，作为 detail 摘要。"""
    ad = _mock(tmp_dir, {}, unavailable_after=1)
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei:
        ad.invoke(_view("pm", [1]), None)
    assert ei.value.detail == "quota exceeded (mock)"
    assert ei.value.adapter_name, "须带 adapter 名（缺省角色名兜底）"

    ad2 = _mock(tmp_dir, {}, unavailable_after=1, unavailable_text="额度耗尽（mock）")
    with pytest.raises(orch.adapters.AdapterUnavailableError) as ei2:
        ad2.invoke(_view("pm", [1]), None)
    assert ei2.value.detail == "额度耗尽（mock）"


def test_mock_unavailable_raises_before_script_lookup(tmp_dir):
    """d4 用例形态：script={} + unavailable_after=1 → 抛额度类，而不是 KeyError。"""
    ad = _mock(tmp_dir, {}, unavailable_after=1)
    with pytest.raises(orch.adapters.AdapterUnavailableError):
        ad.invoke(_view("pm", [42]), None)
    assert _ledger_lines(tmp_dir) == []


def test_mock_default_never_raises_unavailable(tmp_dir):
    """缺省 unavailable_after=None → 永不抛额度类（M0/M4 既有行为逐字不变）。"""
    env = {"to": ["moderator"], "type": "handoff", "body": "ok"}
    ad = _mock(tmp_dir, {1: env, 2: env})
    for evt in (1, 2):
        got, _ = ad.invoke(_view("pm", [evt]), None)
        assert got == env
    assert _ledger_lines(tmp_dir) == ["pm:1", "pm:2"]


# ——————————————————————————————————————————————————————————————
# ⑦ MockAdapter key_by："call" 换序查表；缺省 "event" 逐字不变
# ——————————————————————————————————————————————————————————————

def test_mock_key_by_call_looks_up_by_call_index(tmp_dir):
    """契约 §2 扩展（裁决③）：key_by='call' → 按该实例调用序号（从 1 起）查表。

    附录B 事件号偏移场景解耦：脚本表键与真实事件号无关；ledger 仍记真实事件号。
    """
    first = {"to": ["moderator"], "type": "handoff", "body": "first"}
    second = {"to": ["moderator"], "type": "handoff", "body": "second"}
    ad = _mock(tmp_dir, {1: first, 2: second}, key_by="call")

    got1, _ = ad.invoke(_view("pm", [7]), None)      # 事件号 7 ≠ 调用序 1
    got2, _ = ad.invoke(_view("pm", [11]), None)     # 事件号 11 ≠ 调用序 2
    assert got1 == first
    assert got2 == second
    # ledger 语义不变：记的是真实触发事件号，不是调用序号（§9.4 exactly-once 口径）。
    assert _ledger_lines(tmp_dir) == ["pm:7", "pm:11"]


def test_mock_key_by_event_is_default_and_unchanged(tmp_dir):
    """缺省 key_by='event'：按触发事件号（view.event_ids 最大值）查表，逐字不变。"""
    env7 = {"to": ["moderator"], "type": "handoff", "body": "by-event-7"}
    implicit = _mock(tmp_dir, {7: env7})
    got, _ = implicit.invoke(_view("pm", [3, 7]), None)
    assert got == env7
    assert _ledger_lines(tmp_dir) == ["pm:7"]

    explicit = _mock(tmp_dir, {7: env7}, key_by="event")
    got2, _ = explicit.invoke(_view("pm", [7]), None)
    assert got2 == env7


def test_mock_key_by_event_missing_entry_still_keyerror(tmp_dir):
    """缺省路径的错误语义不变：脚本缺该事件号 → KeyError（暴露编排错误，不静默兜底）。"""
    ad = _mock(tmp_dir, {1: {"to": [], "type": "chat", "body": "x"}})
    with pytest.raises(KeyError):
        ad.invoke(_view("pm", [9]), None)


def test_mock_rejects_unknown_key_by(tmp_dir):
    """key_by 只允许 'event' | 'call'；其它值构造即报错（不猜测、不静默降级）。"""
    with pytest.raises(ValueError):
        _mock(tmp_dir, {}, key_by="role")
