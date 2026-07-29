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

    该端点经 `_role_binding_projection` → `clim._read_config_file` 读 config；
    修复前 YAMLError 穿透到 server 顶层兜底 → 500 + 非 JSON 体，前端状态面板全黑。
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
        # 键名冻结不变：roles 投影仍在（配置读不出来时退化为空表，不臆造绑定）。
        assert isinstance(body.get("roles"), list)


def test_thread_status_with_valid_config_yaml_unchanged(tmp_dir):
    """对照：合法 config.yaml 下 status 端点的 roles 投影逐字不变（主绑定/生效绑定都在）。"""
    (tmp_dir / "config.yaml").write_text(VALID_YAML, encoding="utf-8")
    with _Serving(tmp_dir) as base:
        tid = _new_thread(base)

        code, body = _req(base, f"/api/threads/{tid}/status")

        assert code == 200, (code, body)
        by_role = {r["role"]: r for r in body["roles"]}
        assert sorted(by_role) == ["moderator", "pm"]
        for row in by_role.values():
            assert row["primary"] == "claude"
            assert row["effective"] == "claude"
            assert row["blocked"] is False
