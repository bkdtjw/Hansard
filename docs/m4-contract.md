# M4 冻结接口契约（跨任务卡边界）

> Lead 设计规格（非实现代码）。M0/M1/M2/M3 已冻结接口继续有效。与 spec 冲突以 spec 为准。

## 0. M4 范围与边界（spec §15）
- **新增**：故障注入钩子覆盖 §4.4 全部落盘间隙、≥50 轮 mock 层混沌 harness、§13 全部指标汇总、`orch metrics` / `orch replay` / `orch reopen` CLI 完善。
- **验收标准**（spec §15）：mock 层 **100% 通过率**（硬门槛，非 %-通过）；`orch metrics` 输出 §13 全表。
- **不做**：真实后端混沌（属陪跑）；真实 API 联跑（属 Q1/Q2）。

## 1. 故障注入钩子（§9.4 第一层）—— owner T2

`orch.store` 与 `orch.scheduler` 关键落盘点新增可注入的 `_fault_hook` 回调，覆盖 spec §4.4 五个事务边界：
- (1) `append_event` 事务提交后 / 前（模拟提交前崩溃、提交后崩溃两种）
- (2) `mark_dispatching` 提交后
- (3) `invoke` 前 / 中 / 后（模拟适配器崩溃）
- (4) `autocommit` 后（M2 权限层）
- (5) `reply_and_done` 事务前 / 提交后

```python
# orch.store 内新增
_CURRENT_FAULT: Optional[FaultInjector] = None  # 可注入的全局单例

class FaultInjector:
    """混沌 harness 的故障注入钩子。特定 (site, count) 时抛 SystemExit 或
    self._trigger('SIGKILL') 模拟 kill -9。"""
    def check(self, site: str) -> None: ...  # 抛 SystemExit(137) 模拟 kill -9
```

## 2. 混沌 harness（§9.4 mock 层）—— owner T3

```python
class ChaosHarness:
    """在附录B fixture 上跑 mock，注入 SIGKILL 于 §4.4 各步间隙，每轮重启续跑至终止。
    校验：ledger 无重复事件号 + 终态产物与不中断基准逐字节一致。
    ≥50 轮 100% 通过是 M4 硬门槛。"""
    def run(self, rounds: int = 50) -> ChaosReport: ...
```

## 3. §13 指标汇总（`orch metrics`）—— owner T4

```
| 指标 | 采集 | 计算 |
|---|---|---|
| 端到端任务数/平均轮数/成本 | events 表 + metrics 表 | 聚合 |
| 聚合节省 % | metrics.batch_size | Σ(batch_size-1)/总调用数 |
| 首次合法率 % | metrics.schema_retry | 1 - retry/total |
| 背景压缩比 | render 时 metrics 记 orig/summarized token | 均值 |
| resume 输入 token 节省 % | bench resume 结果 | 输入 token 均值差 |
| 混沌轮数与两层结果 | ChaosReport | mock 100%/真实 % |
| 新增供应商 adapter 行数 | cloc | 从第3家起算 |
```

## 4. `orch replay` + `orch reopen` 完善（§12）—— owner T4
- `orch replay t-xxx`：按事件日志逐事件重放渲染（第三人称群聊 markdown）。
- `orch reopen t-xxx`：已在 M2 骨架实现，M4 补测试。

## 5. 测试约定
- 混沌 harness 用附录B mock fixture，注入点覆盖 §4.4 所有 5 个间隙 + 纯随机。
- **50 轮硬门槛**：pytest 用 marker `@pytest.mark.chaos`，`--rounds 50` 参数控制；默认 CI 用 5 轮快跑（不覆盖硬门槛),M4 验收用 50 轮跑一次贴证据。
- metrics CLI 用 CliRunner 断言输出全 §13 表格所有列。

## 6. M4 简化清单（记 IMPLEMENTATION_NOTES.md）
1. 真实后端混沌不做（属陪跑）。
2. 50 轮硬门槛用 chaos_rounds 环境变量或 CLI 参数；默认 CI 跑 5 轮避免拖慢，M4 验收本地跑 50 轮贴证据。
3. 若 50 轮出现任一失败，harness 打印失败轮 seed + 注入点，回环故障点修复,不弱化门槛。
