"""用户界面 CLI（spec §12）。

M2-T4 里程碑：typer app 在 `orch.cli.main` 定义，`orch.cli.app` 对外暴露供 CliRunner 使用
（M2 契约 §5：命令与 spec §12 表一致）。
"""

from __future__ import annotations

from orch.cli.main import app  # noqa: F401 (re-export)

__all__ = ["app"]
