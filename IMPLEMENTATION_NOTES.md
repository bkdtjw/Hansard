# 实现笔记（IMPLEMENTATION_NOTES.md）

本文件记录：Lead 集成胶水清单、spec §17 开放决策点的取舍（决定 + 一行理由）、
以及施工过程中的关键事实。每条须可追溯。

## 环境事实（M0 冷启动实测）
- Python 3.14.0（D:\python\python.exe）；spec §14 要求 ≥3.11，满足。
- pytest 9.0.2；git 2.52。
- 白名单依赖实测可安装（全局 site-packages，Python 3.14）：
  jsonschema 4.26.0、pyyaml 6.0.3、typer 0.25.1、click 8.3.1。

## Lead 集成胶水清单
（每一处 Lead 亲手写的非实现代码——包骨架/__init__/装配——记于此，含文件与理由）

## 开放决策点（spec §17）取舍
（格式：决策项 → 选择 → 一行理由）

### 依赖选型（spec §14 白名单内的"或"选择，非新增依赖）
- schema 校验库 → **jsonschema 4.26**（而非 pydantic）→ 附录A本就是 JSON Schema
  draft-07，直接喂给 jsonschema 校验；纯 Python，规避 Python 3.14 下
  pydantic-core 需 Rust 编译/可能缺 wheel 的风险。
- CLI 框架 → **typer 0.25**（含 click 8.3）→ 声明式、类型友好，与 spec §12 命令表映射清晰。

## 里程碑进度台账
- M0：进行中。任务卡状态见下。
