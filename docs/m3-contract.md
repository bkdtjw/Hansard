# M3 冻结接口契约（跨任务卡边界）

> Lead 设计规格（非实现代码）。M0/M1/M2 已冻结接口继续有效。与 spec 冲突以 spec 为准。

## 0. M3 范围与边界（spec §15）
- **新增**：resume 热续增量（§6.5）、同目标聚合评估（§5.1，M0 已聚合、M3 补 batch_size 指标）、写域并行（§5.1）、本地 CI 回调真异步（§5.2/§7.5）、多线程并发（§9.3）、`orch attach/bench` CLI（§12）。
- **不做**：混沌完整化（M4）、真实 CLI 陪跑（属 Q1/Q2 陪跑，M2 遗留）。
- **验收标准**（spec §15）：`orch bench resume` 产出报告；双线程并发互不干扰测试。

## 1. 诚实边界
夜段可自动化：
- (a) §6.5 render 热续增量：黑板 diff + 新事件 + 指令尾；活会话（sessions.sid 非空）时替代冷启动全量。
- (b) §5.1 写域并行：per-target 并行 invoke（asyncio.TaskGroup），写域相交回退串行。
- (c) §5.2 长作业真异步：register_job → 后台 subprocess → 完成后 append system 事件回调。
- (d) §9.3 多线程并发：每线程独立事件循环 + per-adapter caps.max_concurrent 全局信号量。
- (e) `orch attach t-xxx role` 联跑打印 + `orch bench resume <fixture>` 对比开/关 resume 的 token 消耗。

## 2. `orch.render` 扩展（§6.5 热续增量）—— owner T2

```python
def render_delta(store, config, *, role, event_ids, last_evt,
                 instruction="") -> RenderedView:
    """§6.5 热续增量：新事件全文（第三人称、event_id > last_evt 的 B 类）+ 黑板 diff
    (last_evt 之后的 A 类事件，前缀"以下决策覆盖旧结论：")+ 指令尾（必发）。
    调度层在 sessions.sid 非空且 gen 未变 + 黑板 version 未大改（<1 step）时用它替代 cold render_view。
    契约版本变更≥1 或人类显式指示 → 主动作废 sid，回退冷启动（§6.5 规则2）。"""
```

## 3. `orch.scheduler` 扩展（并行+异步+多线程）—— owner T3

```python
async def run_thread_async(store, config, adapters) -> None:
    """§5.1 异步版核心环：写域不相交组 asyncio.TaskGroup 并行 invoke；写域相交组串行。"""

async def run_workspace(workspace_dir, config, adapters_factory) -> None:
    """§9.3 多线程：per-thread event loop + per-adapter caps.max_concurrent 信号量。"""

def register_async_job(store, corr, cmd, callback_to) -> None:
    """§5.2 长作业真异步：非阻塞启动 subprocess；完成时 append system 事件（jobs 表状态流转）。"""
```

## 4. CLI (§12) 扩展—— owner T4

- `orch bench resume <fixture>`：同 fixture 开/关 resume 各跑 ≥3 次，输出 tokens_in 均值差与百分比。
- `orch attach t-xxx role` 联跑：打印 `--resume {sid}`（依赖 M2 骨架），M3 补 tokens_in 对比。

## 5. 测试约定
- §6.5 render_delta 单测（黑板 diff 前缀、事件号连续、指令尾必发）。
- 写域并行单测：注入并发计数验证 max_concurrent 上限。
- 双线程并发互不干扰：两 Store 并跑，事件/黑板互不污染。
- 混沌注入停留 M4，M3 只做\"正常路径 + 显式并发场景\"。
- bench resume 用 pytest fixture 生成简化任务而非附录B（节省时间）。
