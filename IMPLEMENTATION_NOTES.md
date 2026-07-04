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

- **T0 包骨架**（commit `m0/T0`）：`src/orch/` 顶层包 + 7 子包
  （protocol/store/scheduler/adapters/render/verify/cli）的 `__init__.py`；
  `pyproject.toml`（setuptools 后端；依赖 jsonschema+pyyaml；
  pytest pythonpath=src、testpaths=tests）；`tests/__init__.py`。
  render/verify/cli 仅占位 docstring（M1/M2 归属，M0 不实现其内容）。
  验证：`PYTHONPATH=src python -c "import orch,..."` 通过；`pytest` 收集 0 项。
- **M0 接口契约**（commit `m0/T0`）：`docs/m0-contract.md`——冻结跨任务卡边界的
  模块/符号/签名/返回约定（设计规格，非实现代码），供 T1 写测试与 T2–T5 实现共享，
  确保测试先行与实现对齐。内部逻辑仍由各 worker 依 spec 自主实现。

## 开放决策点（spec §17）取舍
（格式：决策项 → 选择 → 一行理由）

### 依赖选型（spec §14 白名单内的"或"选择，非新增依赖）
- schema 校验库 → **jsonschema 4.26**（而非 pydantic）→ 附录A本就是 JSON Schema
  draft-07，直接喂给 jsonschema 校验；纯 Python，规避 Python 3.14 下
  pydantic-core 需 Rust 编译/可能缺 wheel 的风险。
- CLI 框架 → **typer 0.25**（含 click 8.3）→ 声明式、类型友好，与 spec §12 命令表映射清晰。

## 里程碑进度台账
- M0：进行中。
  - T0 ✅ 包骨架 + 接口契约（4cc4b84）
  - T1 ✅ 验收测试先行 82 用例全红（143d97e）
  - T2/T3/T4 派发中；T5/T6 待。

### T1 反馈裁决（跨卡边界缺口，Lead 自决，已并入 docs/m0-contract.md §8）
- ① human approve 入口 → 冻结 `scheduler.apply_gate_decision(...)`（owner T5）
- ② 系统执行器触发点 → 由 apply_gate_decision 驱动，M0 同步退化（owner T5）
- ③ ledger 父目录自动创建 → MockAdapter mkdir（owner T4）
- ④ Store 线程目录属性 → 冻结 `Store.thread_dir`（owner T3）
- ⑤ 通用标 done 原语 → 冻结 `Store.mark_done(event_id,target)`（owner T3）

### 整晚自主推进授权（2026-07-04 夜）
用户授权：睡觉期间 Lead 自主、高强度用 Workflow 派发 opus worker，推进
orchestrator-spec 全部里程碑（M0→M4）。执行框架不减步：分解表 → 测试先行见红
→ workflow 并行实现 → Lead 收尾三步（写域审计/全量 pytest/逐卡 commit）→
独立只读评审 → 完成定义五项 → 合入 main 打 tag → 推送 bkdtjw/Hansard。
诚实边界：人工验收（playbook §4）无法代做，做到 agent 侧完成定义、证据留盘供
醒来终审；M2 真实 CLI 后端需人陪跑，遇自动化极限在 QUESTIONS.md 升级、禁止伪造。

### M0 实现进度（workflow wf_221aa457-910 产出）
- T2 protocol(rules.py+schema.py) / T3 store / T4 adapters / T5 scheduler
  (core+recover+systemexec+_dispatch) / T6 E2E 装配 —— 均已落盘。
- 全量 pytest：81 passed, 1 failed。写域审计：全部在各卡白名单内，无越权。

### 裁决：§9.4 "无重复事件号" 的正确语义（回环修正 R1，workflow wf_c3a4fd65-97a）
- exactly-once = mock ledger **整行**(role:event_id)唯一，即 len(lines)==len(set(lines))。
- "事件号全局唯一"是**误读**：附录B E3(review→backend,frontend)、E7(assign→backend,frontend)
  是聚合/多目标派发，同一事件号被多个角色各处理一次是合法设计。
- 处置：派 worker 删除 test_adapters 该误读断言（保留整行唯一断言）。

### M0 独立评审结论（wf_965a4c9e，只读 Explore/opus，41 工具调用/186k tokens）
**无阻塞问题，M0 可验收。** §16 十三条全通过或不涉及（M0 退化项已注明：视图 M1、
权限三件套 M2）；附录A schema 一字不差、§4.3 DDL 六表逐字、§4.4 落盘顺序事务(1)(2)(5)
严格对应、§9.1 恢复 a/b/c+挂起保持正确、测试无假绿（无 skip/xfail/mock-SUT，R1 删除
合理，附录B 四项断言真实）、契约符合。
3 条非阻塞建议逐条裁决：
- ①§3.2 发送者约束降级在调度层未接线（allowed_sender 已实现，run_thread 未调用）
  → **回环 T5 补齐**（R2，wf_c66aa597）：spec §3.2 明确职责、属 M0 §3+§5.1 范围，须忠实。
- ②终止后遗留 1 行惰性 pending 派发行（终止总结 system 事件所致）→ 评审确认无害
  （terminated 拒新派发、recover 不处理 pending、不违反 §5.4/exactly-once）→ **留 M1 清理**。
- ③_dispatch.py 短生命周期只读 sqlite 旁路 Store 公开面 → 合规（读盘即弃、契约 §7 授权）
  → **留后续内聚，不改**。
