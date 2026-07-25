# spec 增补草案：M5 适配器可用性与降级路由

> 状态：**已批准并合入**（2026-07-25 用户批准，含修订：控制台必须提供
> enable/disable 开关按钮）。本文保留为设计记录；生效文本以 orchestrator-spec.md 为准。

## 0. 动机与设计不变量

现状缺口：系统没有"某家供应商现在不可用"的概念。额度耗尽的 adapter 会被反复选中——
§5.1 路径每次都是：派发 → 失败 → 原地重试 1 次 → failed → 转 moderator → 下轮再撞。
人工唯一出路是手改 config 换绑并重启（§11.1 热替换），无自动化、无可见性。

本增补引入三件事：

1. **可用性标记**：每个 adapter 一个 enabled/disabled 两态开关。人工可设
   （中转站看不到余额，手动标记是第一公民），落盘，重启不丢。
2. **降级路由**：角色配置备选 adapter 顺序（fallback）；派发时主绑定不可用
   则自动取首个可用备选冷启动接手；全部不可用则该派发行保持等待并通告，
   **禁止**空转重试。
3. **自动跳闸**：invoke 报错命中额度/限流特征，或连续传输级失败达到阈值，
   编排器自动将该 adapter 置 disabled。恢复**一律人工** `orch adapter enable`
   （裁决：防止对已耗尽的额度反复撞墙；"限流类冷却后自动恢复"列为将来扩展，
   本里程碑明确不做）。

为什么是"顺纹"的：会话是缓存、事件日志是真相（§4.2）；换 adapter = 冷启动
全量组装（§6.1–6.4），与崩溃恢复共用同一条已被混沌验证的路径。因此：
**零 DDL 变更、零落盘顺序（§4.4）变更、零信封协议变更**——sessions 表既有
backend 列足以承载换绑语义；审计复用既有 system 类型。

---

## 逐条增补

### A1 · §1 术语表追加三行

| 术语 | 定义 |
|---|---|
| 可用性 availability | adapter 的 enabled/disabled 两态，全局（跨线程），落盘于适配器状态文件（§4.1） |
| 降级路由 fallback | 角色主绑定不可用时，按 fallback 顺序取首个可用 adapter（§5.6） |
| 跳闸 trip | 编排器依报错特征或连续失败自动置 disabled（§5.6.3） |

### A2 · §4.1 目录布局，orchestrator/ 下追加一行

```
  adapter_state.json          # 适配器可用性（全局，跨线程；原子替换写）
```

### A3 · §4.2 真相/缓存分类，"真相（落盘）"行内容清单追加

「适配器状态文件」→ 重启后处理：直接装载。

### A4 · 新增 §5.6 适配器可用性与降级路由

#### 5.6.1 状态与存储

- 每个 adapter 恰有一个可用性状态 `enabled | disabled`，连同
  `{reason, by: human|auto, ts, fail_streak}` 存于全局文件
  `orchestrator/adapter_state.json`。不进线程 db：额度是供应商级事实，跨线程共享。
- 写入**必须**原子替换（临时文件 + rename）。写者有二：CLI（人工 enable/disable）
  与调度器（自动跳闸、streak 维护）。最后写入者胜；竞态最坏后果是一次多余的
  人工重设，可接受。
- 文件缺失 → 视为全部 enabled（冷启动默认）；文件损坏 → 启动报错，
  **禁止**猜测（§9 同一哲学）。
- 调度器每轮调度前重读该文件（轮询间隔属 §17）；**禁止**只在启动时读一次。

#### 5.6.2 生效绑定解析（每次派发时现算，不落盘）

```
effective_adapter(role) = [roles[role].adapter] + roles[role].fallback 中首个 enabled 项
```

- 解析发生在 §5.1 标 dispatching 之前。聚合与并行判定不变——它们是角色级概念，
  与绑定无关。§5.1 代码块相应插一行注释：
  `for g in schedule(groups):` 之后 →
  `# 生效绑定解析(§5.6.2)；无可用 → 本组保持 pending，本轮跳过`
- effective ≠ sessions.backend → 视为会话死亡：sid 置空、gen += 1、backend 更新，
  走冷启动全量组装。原主恢复 enabled 后，下一次派发自然回归主绑定（同样冷启动）。
- **换绑重派时该派发行 attempts 归零**（新后端享有完整重试预算；链长有限且跳闸
  单向，不存在无限循环）。
- 每次 effective ≠ 主绑定，追加一条 system 审计事件（body 含角色、原绑定、
  生效绑定、原因），**比照 terminate（§5.4）：落盘但不生成派发行**——是通告，
  不是待办。同一（role，生效绑定）连续派发只在首次记录；"首次"判定一律现查
  日志（该角色最近一条切换审计事件），**禁止**内存驻留去重状态（§16 第 9 条）。
- 全部不可用 → 该派发行**保持 pending**，本轮跳过；进入此状态的首次追加一条
  system 通告事件（同上不生成派发行），CLI 与控制台**必须**显著呈现（§12）。
  **禁止**对无可用 adapter 的角色空转重试或消耗 attempts。其余角色照常调度，
  线程不挂起。人工 enable 后 pending 行被主循环自然接手——与 §9.1
  "pending 行不需处理"同一机制，零新增派发状态。
- §5.1 等待条件相应扩展：batch 非空但无可调度组时同样进入等待；唤醒源增加
  "适配器状态变更"（实现可用轮询）。**禁止**忙等。

#### 5.6.3 自动跳闸

满足其一即把该 adapter 置 disabled（by=auto）：

1. **特征命中**：invoke 传输级报错文本（stderr / 进程退出信息 / 无输出错误）
   命中该 adapter 的 `unavailable_patterns`（大小写不敏感子串；默认清单属 §17）
   → 立即跳闸。该次失败**不计** attempts：跳闸保证同一 adapter 不会再被选中，
   派发行回 pending 并立即按 §5.6.2 重解析（通常由 fallback 接手）。
2. **连续失败**：传输级失败（超时 / 进程失败 / 无法解析出信封）使该 adapter 的
   fail_streak += 1，成功 invoke 清零；fail_streak ≥ trip_after（默认 3，可配）
   → 跳闸。schema 校验失败**不计入** streak——那是输出质量问题不是可用性问题
   （§5.1 原地重调路径不变）。此类失败照常消耗 attempts（既有语义不动），
   跳闸只是叠加的副作用。

- 跳闸时追加 system 审计事件（不生成派发行），body 含 adapter、触发条件、
  原始报错摘要。
- 恢复**仅限**人工 `orch adapter enable`（同时清零 fail_streak）。
  **禁止**任何形式的自动恢复或冷却重试（本里程碑裁决）。

#### 5.6.4 与既有机制的边界

- 看门狗（§5.3）语义不变；超时既走看门狗路径，也计入 fail_streak。
- 崩溃恢复（§9.1）唯一新增：启动时装载 adapter_state.json（真相类，直接装载）。
  恢复出的行经主循环自然走 §5.6.2 解析，无新对账分支。
- 切换前失败的 invoke 可能留下脏 worktree——处理与既有重试路径完全一致
  （§9.2 第 3 层：git status 如实呈现，从现场继续），无新规则。

### A5 · §7.6 适配器职责追加一句

输出规范化职责扩展：invoke 的错误报告**必须**区分传输级失败与额度类失败
（依 unavailable_patterns 识别，识别责任在适配层）；调度器只消费分类结果，
**禁止**在调度层散布各家报错文案的字符串匹配。

### A6 · §11.1 config.yaml 增补字段

```yaml
adapters:
  kimi_cli: {kind: cli, …,
             unavailable_patterns: ["quota", "insufficient", "rate limit", "429", "额度"],
             trip_after: 3}
roles:
  pm: {adapter: kimi_cli, fallback: [claude_cli], …}
```

- `fallback`：有序列表，缺省 `[]`（无备胎：不可用即等待人工处理）。
- 装载时校验，违者启动报错：fallback 项必须是已声明的 adapter；
  `tools` 或 `write_scope` 非空的角色，其主绑定与 fallback 项**必须**为 cli 型
  （API 型不带工具循环，§7.3）。moderator 建议配置非空 fallback
  （它不可用会阻塞兜底路由）。
- `unavailable_patterns`、`trip_after` 为 adapter 级可选字段，缺省值属 §17。

### A7 · §12 CLI 表追加三行 + 显示要求

| 命令 | 行为 |
|---|---|
| `orch adapters` | 列全部 adapter：状态 ✅/⛔、reason、by（手动/自动）、ts、fail_streak |
| `orch adapter disable <name> [--reason …]` | 人工标记不可用（调度器停止选中） |
| `orch adapter enable <name>` | 人工恢复，清零 fail_streak |

`orch status` 与控制台：角色行显示当前生效绑定；主绑定被禁用时显示 ⛔ 与
生效备胎；存在"无可用 adapter"的阻塞角色时**必须**显著警示。控制台**必须**
为每个 adapter 提供 enable/disable 开关按钮（disable 可填 reason），行为与
CLI 两命令等价（同一原子替换写路径）。（2026-07-25 批准时用户追加）

### A8 · §13 指标表追加两行

| 指标 | 采集点 | 计算 |
|---|---|---|
| 降级切换次数 | 每次 effective ≠ 主绑定的派发记一条 | 计数，按 (role, adapter) 分组 |
| 自动跳闸次数 | 每次 auto 跳闸记一条 | 计数，按触发条件分类 |

### A9 · §15 里程碑表追加 M5 行

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M5** 适配器可用性 + 降级路由 | 全局状态文件；手动 enable/disable；生效绑定解析与换绑冷启动；自动跳闸（特征 + 连续失败）；阻塞等待与通告；mock 适配器支持脚本化额度故障；CLI 三命令与控制台呈现；指标两项 | mock 下：disable 主绑定 → fallback 接手完成附录 B 任务，终态与不中断基准一致；无 fallback 角色阻塞等待、enable 后续跑完成；注入额度报错 → 自动跳闸 + 降级接手 E2E；连续失败跳闸路径单测；切换间隙 kill -9 混沌 ≥ 20 轮 100% 通过（§9.4 第一层扩展场景）；`orch adapters` 输出与状态文件一致；两项指标可复算 |

### A10 · §17 开放决策点追加

适配器状态文件轮询间隔；unavailable_patterns 默认清单；adapter_state.json
的具体字段编排（须含 status/reason/by/ts/fail_streak）。

---

## 明确不改的地方（审查时请重点确认）

1. **DDL 零变更**：sessions 表既有 backend 列承载换绑；适配器状态是全局事实，
   不进线程 db。
2. **落盘顺序（§4.4）零变更**；恢复算法（§9.1）仅新增装载一个状态文件。
3. **信封协议零变更**：审计复用既有 system 类型（保留策略 C），
   无新 type、无新字段。
4. 看门狗、聚合、并行判定、门禁、终止语义均不动。
