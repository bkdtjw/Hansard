"""语法错 config.yaml 的受控降级（M5 后·T3 缺陷卡）。

缺陷根因：``yaml.YAMLError`` 的 MRO 是 (YAMLError, Exception, BaseException, object)，
**不是** ValueError 子类——``_read_config_file`` 原先只写 ``except (OSError, ValueError)``，
workspace 的 config.yaml 一旦语法写错（缩进错 / 冒号后少空格 / 括号不闭合），解析异常
直接穿透：CLI 侧带栈崩给用户，web 侧 ``GET /api/threads/{id}/status`` 被 server 顶层
兜底吞成 500，整个状态面板黑掉。

本文件三层证据：
  ① 单元层：``_read_config_file`` 对语法错文件返回降级值且不抛；
  ② HTTP 层：真起 in-process server（沿用 tests/test_web.py 的 make_server(port=0)
     + 后台线程用法），workspace 放一份语法错 config.yaml，断言 status 端点仍 200
     且响应可解析（面板不黑）；
  ③ 对照层：合法 config.yaml 的行为逐字不变，且**非** yaml/OSError 的真 bug 仍然穿透
     （证明修复没有退化成裸 ``except Exception``，spec §16 反模式）。

临时目录沿用仓库约定的 `tmp_dir` fixture（tests/conftest.py：项目本地 .pytmp/）。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

import orch.cli.main as clim


# ——————————————————————————————————————————————————————————————
# 语法错 yaml 样本：先自证"确实解析不了"，避免测试因样本恰好合法而假绿。
# ——————————————————————————————————————————————————————————————

BROKEN_YAML_FLOW_SEQ = """\
roles:
  pm:
    adapter: claude
  backend: [unclosed
"""

BROKEN_YAML_INDENT = """\
adapters:
  claude:
    kind: cli
   start_cmd: claude
"""

VALID_YAML = """\
adapters:
  claude:
    kind: cli
    start_cmd: claude
roles:
  pm:
    adapter: claude
  moderator:
    adapter: claude
"""


@pytest.mark.parametrize("text", [BROKEN_YAML_FLOW_SEQ, BROKEN_YAML_INDENT])
def test_broken_samples_really_are_yaml_errors(text):
    """自证样本：两份样本都必须让 pyyaml 抛 YAMLError（否则下面的用例是假绿）。"""
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)


# ——————————————————————————————————————————————————————————————
# ① 单元层：_read_config_file 对语法错 yaml 降级不抛
# ——————————————————————————————————————————————————————————————

@pytest.mark.parametrize("text", [BROKEN_YAML_FLOW_SEQ, BROKEN_YAML_INDENT])
def test_read_config_file_degrades_on_yaml_syntax_error(tmp_dir, text):
    """语法错 config.yaml → 返回既有降级值（空配置 dict），且不抛任何异常。"""
    cfg_path = tmp_dir / "config.yaml"
    cfg_path.write_text(text, encoding="utf-8")

    cfg = clim._read_config_file(cfg_path)

    assert cfg == {}, f"语法错 yaml 应降级为空配置，实得 {cfg!r}"
    assert isinstance(cfg, dict), "返回形状必须仍是 dict（函数签名与返回形状不变）"


def test_load_config_degrades_on_yaml_syntax_error(tmp_dir):
    """workspace 级 `_load_config` 共用同一份读取实现，同样受控降级（不另立口径）。"""
    (tmp_dir / "config.yaml").write_text(BROKEN_YAML_FLOW_SEQ, encoding="utf-8")

    assert clim._load_config(tmp_dir) == {}


# ——————————————————————————————————————————————————————————————
# ③ 对照层之一：合法 config.yaml 行为不变 + 真 bug 不被吞
# ——————————————————————————————————————————————————————————————

def test_read_config_file_valid_yaml_unchanged(tmp_dir):
    """合法 config.yaml 逐字解析，行为与修复前一致（降级只对错误路径生效）。"""
    cfg_path = tmp_dir / "config.yaml"
    cfg_path.write_text(VALID_YAML, encoding="utf-8")

    cfg = clim._read_config_file(cfg_path)

    assert cfg["adapters"]["claude"]["kind"] == "cli"
    assert cfg["roles"]["pm"]["adapter"] == "claude"
    assert sorted(cfg["roles"]) == ["moderator", "pm"]


def test_read_config_file_missing_and_nonmapping_unchanged(tmp_dir):
    """既有两条降级约定不变：文件不存在 → {}；顶层非 mapping（如列表）→ {}。"""
    assert clim._read_config_file(tmp_dir / "nope.yaml") == {}

    list_cfg = tmp_dir / "list.yaml"
    list_cfg.write_text("- a\n- b\n", encoding="utf-8")
    assert clim._read_config_file(list_cfg) == {}


def test_read_config_file_does_not_swallow_unrelated_errors(tmp_dir, monkeypatch):
    """修复口径只扩到 yaml 解析错，**不是**裸 except Exception（spec §16 反模式）。

    用一个与 yaml/IO 无关的异常（RuntimeError）证明真 bug 仍会穿透而非被吞成空配置。
    """
    cfg_path = tmp_dir / "config.yaml"
    cfg_path.write_text(VALID_YAML, encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("真 bug，不该被降级吞掉")

    monkeypatch.setattr(yaml, "safe_load", _boom)
    with pytest.raises(RuntimeError):
        clim._read_config_file(cfg_path)


# ——————————————————————————————————————————————————————————————
# ④ 严格读取 helper：区分"没配置"与"配置坏了"（评审"应修3"的下层支点）
# ——————————————————————————————————————————————————————————————

def test_read_config_file_checked_distinguishes_missing_empty_and_broken(tmp_dir):
    """三态各就各位：缺失/空 → 无错误；语法错 → ({}, 一句人话)。"""
    missing = tmp_dir / "nope.yaml"
    assert clim._read_config_file_checked(missing) == ({}, None)

    empty = tmp_dir / "empty.yaml"
    empty.write_text("# 只有注释\n", encoding="utf-8")
    assert clim._read_config_file_checked(empty) == ({}, None)

    ok = tmp_dir / "ok.yaml"
    ok.write_text(VALID_YAML, encoding="utf-8")
    cfg, err = clim._read_config_file_checked(ok)
    assert err is None
    assert cfg["roles"]["pm"]["adapter"] == "claude"

    bad = tmp_dir / "bad.yaml"
    bad.write_text(BROKEN_YAML_FLOW_SEQ, encoding="utf-8")
    cfg, err = clim._read_config_file_checked(bad)
    assert cfg == {}
    assert isinstance(err, str) and "config.yaml" in err
    assert "\n" not in err, f"错误人话须压成一行（要进 JSON 与警示条）：{err!r}"


def test_read_config_file_checked_flags_non_mapping_top_level(tmp_dir):
    """顶层写成列表：解析得动但配置不可用 —— 同样报错，别让假健康换扇门再进来。"""
    p = tmp_dir / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")

    cfg, err = clim._read_config_file_checked(p)

    assert cfg == {}
    assert isinstance(err, str) and err.strip()
    # 降级读取器的既有约定不受影响（返回形状仍是空 dict）。
    assert clim._read_config_file(p) == {}


def test_read_config_file_delegates_to_checked_no_second_loader(tmp_dir):
    """只有一处 yaml 装载：打桩 checked 版后，宽松版必须跟着变（证明是委托而非复制）。"""
    sentinel = ({"marker": 1}, None)
    monkeypatched = tmp_dir / "whatever.yaml"

    import unittest.mock as _mock
    with _mock.patch.object(clim, "_read_config_file_checked", return_value=sentinel):
        assert clim._read_config_file(monkeypatched) == {"marker": 1}


# ——————————————————————————————————————————————————————————————
# ② HTTP 层：真起 in-process server（沿用 tests/test_web.py 的起法）
# ——————————————————————————————————————————————————————————————

class _Serving:
    """with _Serving(ws) as base: ... —— make_server(port=0) + 后台线程，退出即停。"""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    def __enter__(self) -> str:
        from orch.web.server import make_server

        self.srv = make_server(self.workspace, "127.0.0.1", 0)
        host, port = self.srv.server_address[0], self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        return f"http://{host}:{port}"

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        return False


def _req(base: str, path: str, method: str = "GET", body: dict | None = None):
    """打一次真实 HTTP，返回 (status_code, parsed_json_or_raw_text)。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw, code = resp.read().decode("utf-8"), resp.getcode()
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8"), e.code
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw


def _new_thread(base: str) -> str:
    code, body = _req(
        base, "/api/threads", "POST",
        {"task": "点赞功能", "roles": ["pm", "moderator"]},
    )
    assert code == 200, (code, body)
    return body["id"]


def test_thread_status_survives_broken_config_yaml(tmp_dir):
    """面板不黑：workspace 里躺着语法错 config.yaml 时 status 端点仍 200 且可解析。

    该端点经 `_role_binding_projection` → `clim._read_config_file_checked` 读 config；
    最初 YAMLError 穿透到 server 顶层兜底 → 500 + 非 JSON 体，前端状态面板全黑。
    """
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        # 建线程之后再写坏配置：证明是"读配置"这一步崩，而非建线程本身。
        (tmp_dir / "config.yaml").write_text(BROKEN_YAML_FLOW_SEQ, encoding="utf-8")

        code, body = _req(base, f"/api/threads/{tid}/status")

        assert code == 200, f"语法错 config.yaml 不应把 status 端点打成 {code}：{body!r}"
        assert isinstance(body, dict), f"响应应为可解析 JSON 对象，实得 {body!r}"
        assert body.get("status") == "running"
        assert isinstance(body.get("dispatches"), list)


def test_broken_config_yields_empty_projection_and_error_signal(tmp_dir):
    """评审"应修3"：坏 config 下**不得**臆造健康绑定，且必须给出可被前端渲染的错误信号。

    改坏实现即红的两条钉子（原先只断言 `isinstance(roles, list)`，对臆造毫无区分力——
    退回"角色名兜底"时 roles 是 [{pm, pm, pm, blocked:false}…]，同样是 list，照样绿）：
      ① roles 投影为**空表**——一行都没有，才谈得上"没臆造"；
      ② 顶层 config_error 是**非空字符串**，且带上路径线索，运维看得懂该修哪。
    """
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        (tmp_dir / "config.yaml").write_text(BROKEN_YAML_FLOW_SEQ, encoding="utf-8")

        code, body = _req(base, f"/api/threads/{tid}/status")

        assert code == 200, (code, body)
        assert body["roles"] == [], (
            f"坏 config 下角色绑定不可知，必须给空投影而非兜底臆造：{body['roles']!r}"
        )
        # 反面钉死：任何一行"健康"绑定都是假绿（这正是修复前的实际形态）。
        assert not [r for r in body["roles"] if r.get("blocked") is False]
        err = body.get("config_error")
        assert isinstance(err, str) and err.strip(), f"缺 config_error 信号：{body!r}"
        assert "config.yaml" in err, f"错误人话应点名文件：{err!r}"


def test_broken_config_error_signal_does_not_confuse_role_probe(tmp_dir):
    """新增顶层键不得撞 tests/test_m5_availability.py 的结构探测（判据同源复刻）。

    该探测取"首个每项含 role 键的**顶层列表**"。config_error 是字符串，
    既不该被它命中，也不该让它误取 dispatches。
    """
    (tmp_dir / "config.yaml").write_text(BROKEN_YAML_FLOW_SEQ, encoding="utf-8")
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/status")
        assert code == 200, (code, body)

        hits = [
            v for v in body.values()
            if isinstance(v, list) and v and all(
                isinstance(x, dict) and "role" in x for x in v
            )
        ]
        assert hits == [], f"坏 config 下不该有任何角色投影候选：{hits!r}"
        assert isinstance(body["config_error"], str)


def test_thread_status_without_config_file_unchanged(tmp_dir):
    """对照①：**没有** config.yaml —— 逐字维持现状（角色名兜底投影，且无错误信号）。

    "文件不存在"与"文件写坏了"是两种事实：前者没有任何声明与之矛盾，角色名兜底是
    resolve_effective_adapter 既有约定；后者才是本卡要堵的臆造。
    """
    assert not (tmp_dir / "config.yaml").exists()
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/status")

        assert code == 200, (code, body)
        assert "config_error" not in body, f"无 config 不是错误：{body!r}"
        by_role = {r["role"]: r for r in body["roles"]}
        assert sorted(by_role) == ["moderator", "pm"]
        assert by_role["pm"]["primary"] == "pm"
        assert by_role["pm"]["effective"] == "pm"
        assert by_role["pm"]["blocked"] is False


def test_thread_status_with_empty_config_file_unchanged(tmp_dir):
    """对照②：config.yaml 存在但**合法为空**（只有注释）—— 同样不算错误。"""
    (tmp_dir / "config.yaml").write_text("# 还没配任何东西\n", encoding="utf-8")
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)
        code, body = _req(base, f"/api/threads/{tid}/status")

        assert code == 200, (code, body)
        assert "config_error" not in body, f"空 config 属合法的尚未配置：{body!r}"
        assert {r["role"] for r in body["roles"]} == {"pm", "moderator"}


def test_thread_status_with_valid_config_yaml_unchanged(tmp_dir):
    """对照：合法 config.yaml 下 status 端点的 roles 投影逐字不变（主绑定/生效绑定都在）。"""
    (tmp_dir / "config.yaml").write_text(VALID_YAML, encoding="utf-8")
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)

        code, body = _req(base, f"/api/threads/{tid}/status")

        assert code == 200, (code, body)
        assert "config_error" not in body, f"合法 config 不该带错误信号：{body!r}"
        by_role = {r["role"]: r for r in body["roles"]}
        assert sorted(by_role) == ["moderator", "pm"]
        for row in by_role.values():
            # 投影行四键一个不动（键名冻结面，新增顶层键不得侵入行结构）。
            assert sorted(row) == ["blocked", "effective", "primary", "role"]
            assert row["primary"] == "claude"
            assert row["effective"] == "claude"
            assert row["blocked"] is False


# ——————————————————————————————————————————————————————————————
# ④ 前端：config_error → 一条人话警示（全仓无 JS 运行时，沿 tests/test_web.py
#    既有的"字符串存在性"断言风格打判据两侧）
# ——————————————————————————————————————————————————————————————

def test_app_js_renders_config_error_warning(tmp_dir):
    """app.js 必须消费 config_error 并渲染警示，且新增属性位走 escapeHtmlAttr。"""
    with _Serving(tmp_dir) as base:
        code, js = _req(base, "/app.js")
        assert code == 200, code

        assert "config_error" in js, "前端须读取 status 端点的 config_error 信号"
        assert "lastConfigError" in js, "须有承载该信号的状态位"
        # 警示复用既有警示条（#adapter-warn 的 text/icon 由 updateAdapterAlerts 写）。
        assert "adapter-warn" in js
        # 唯一进 HTML 属性位的是 title=配置错误人话 —— 必须转义（评审硬约束）。
        assert 'title="${escapeHtmlAttr(lastConfigError)}"' in js, (
            "config_error 进 title 属性位必须用 escapeHtmlAttr"
        )
        # 无信号时零渲染：警示文案由 lastConfigError 起头的三元式选出，空串即落回既有档位。
        assert "lastConfigError\n    ?" in js or "lastConfigError ?" in js
