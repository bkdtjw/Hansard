"""orch.web —— 玻璃感 Web 控制台（spec 之外的补充交付）。

纯 HTTP↔现有 orch 函数的适配层 + 原生前端；不改任何 spec 实现语义。
零新增依赖：仅 Python 标准库 + 已有 orch 包 + 已装 pyyaml（config 校验）。

对外入口：
  make_server(workspace, host="127.0.0.1", port=8787) -> ThreadingHTTPServer
"""

from __future__ import annotations

from orch.web.server import make_server

__all__ = ["make_server"]
