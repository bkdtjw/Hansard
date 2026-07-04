"""M0 验收测试共享夹具（tests/conftest.py）。

只提供 pytest **夹具**；纯辅助工具与常量在 tests/helpers.py（供 `from tests.helpers import …`）。
本文件不实现、不占位、不 mock 任何被测逻辑——被测符号一律在各 test 函数体内引用，使未实现
表现为运行时红（fail/error）而非 collection 中断（契约 §7）。

临时目录说明：本机全局 %TEMP%\\pytest-of-* 可能因历史遗留而不可写（WinError 5），
故不用内置 tmp_path，改用项目本地、git 忽略（tests/.gitignore）的 tests/.pytmp/<uid>/ 作为
每个用例的独立可写临时根，用后即清。这只影响测试脚手架，不触碰被测代码与其真相落盘语义。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from tests.helpers import load_like_feature_script

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
