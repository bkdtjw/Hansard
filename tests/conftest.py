"""M0 验收测试共享夹具（tests/conftest.py）。

只提供 pytest **夹具**；纯辅助工具与常量在 tests/helpers.py（供 `from tests.helpers import …`）。
本文件不实现、不占位、不 mock 任何被测逻辑——被测符号一律在各 test 函数体内引用，使未实现
表现为运行时红（fail/error）而非 collection 中断（契约 §7）。

临时目录说明：本机全局 %TEMP%\\pytest-of-* 可能因历史遗留而不可写（WinError 5），
故不用内置 tmp_path，改用项目本地、git 忽略（tests/.gitignore）的 tests/.pytmp/<uid>/ 作为
每个用例的独立可写临时根，用后即清。这只影响测试脚手架，不触碰被测代码与其真相落盘语义。

M4-T5 · chaos_50 marker + --chaos-50 flag（spec §15 M4 硬门槛）
   默认 pytest 不跑 `chaos_50` 标记用例（避免每次 CI 都吃 50 轮混沌）；
   仅当命令行显式传入 `--chaos-50` 时才收集并运行。

M5-T1 · chaos_m5 marker + --chaos-m5 flag（spec §15 M5 "切换间隙 kill -9 ≥20 轮"）
   同一惯例另立一个标志（docs/m5-contract.md §8 要求）：默认不跑 `chaos_m5`
   标记用例，仅当命令行显式传入 `--chaos-m5` 时才收集并运行。
   与 `--chaos-50` 彼此独立、互不影响。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from tests.helpers import load_like_feature_script


# ---------------------------------------------------------------------
# M4-T5 · chaos_50 marker（spec §15 M4 硬门槛：50 轮 100% 通过）
# ---------------------------------------------------------------------

def pytest_addoption(parser):
    """新增 `--chaos-50` 命令行开关：显式请求跑 50 轮混沌硬门槛。"""
    parser.addoption(
        "--chaos-50",
        action="store_true",
        default=False,
        help="Run M4 chaos 50-round hard-gate tests (spec §15 M4).",
    )
    # —— M5-T1 新增：适配器切换间隙混沌 opt-in（与 --chaos-50 独立）——
    parser.addoption(
        "--chaos-m5",
        action="store_true",
        default=False,
        help="Run M5 adapter-switch chaos tests, >=20 rounds (spec §15 M5).",
    )


def pytest_configure(config):
    """注册 chaos_50 marker，避免 `PytestUnknownMarkWarning`。"""
    config.addinivalue_line(
        "markers",
        "chaos_50: mark test as M4 chaos 50-round hard gate (skipped without --chaos-50).",
    )
    # —— M5-T1 新增：chaos_m5 marker ——
    config.addinivalue_line(
        "markers",
        "chaos_m5: mark test as M5 adapter-switch chaos gate (skipped without --chaos-m5).",
    )


def pytest_collection_modifyitems(config, items):
    """未指定 `--chaos-50` / `--chaos-m5` 时对相应标记用例贴 skip（不影响其它用例）。"""
    # —— M5-T1 新增：chaos_m5 的独立门控（先处理，不改动下方 chaos_50 既有逻辑）——
    if not config.getoption("--chaos-m5"):
        skip_m5 = pytest.mark.skip(
            reason="chaos_m5 gate skipped by default; pass --chaos-m5 to run.",
        )
        for item in items:
            if "chaos_m5" in item.keywords:
                item.add_marker(skip_m5)

    if config.getoption("--chaos-50"):
        return
    skip_chaos = pytest.mark.skip(
        reason="chaos_50 hard-gate skipped by default; pass --chaos-50 to run.",
    )
    for item in items:
        if "chaos_50" in item.keywords:
            item.add_marker(skip_chaos)

# 项目本地临时根（tests/.gitignore 已忽略 .pytmp/）。
_PYTMP_ROOT = Path(__file__).parent / ".pytmp"


@pytest.fixture
def tmp_dir() -> Path:
    """每个用例一个独立、可写、用后即清的临时目录（替代不可写的内置 tmp_path）。"""
    _PYTMP_ROOT.mkdir(parents=True, exist_ok=True)
    d = _PYTMP_ROOT / uuid.uuid4().hex
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def thread_dir(tmp_dir) -> Path:
    """一个干净的线程目录（契约 §5：<thread_dir>/{events.db, blackboard/, logs/}）。"""
    return tmp_dir / "t-001"


@pytest.fixture
def like_feature_script() -> dict:
    """附录B 完整 {role: {event_id: env}} mock 脚本（事件号转 int）。"""
    return load_like_feature_script()


@pytest.fixture
def role_script(like_feature_script):
    """便捷取某角色的 {event_id: env} 子表（= MockAdapter 的 script 入参）。"""
    def _get(role: str) -> dict:
        return like_feature_script.get(role, {})
    return _get
