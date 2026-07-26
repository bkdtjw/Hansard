# 异构多智能体编排系统 · 实现规格说明书

文档版本 v1.0 · 交付对象：负责实现本系统的编码 agent · 面向用户的 CLI 文案用中文

## 如何使用本文档

1. 通读全文后再动工。里程碑（§15）按序实现，每个里程碑先写验收测试再写实现。
2. 遇到本文未定义的细节：若属于 §17 开放决策点，自行决定并记录于 `IMPLEMENTATION_NOTES.md`；若不属于，停下向人类提问，禁止擅自扩展协议。
3. §16 反模式清单是硬约束，任何情况下不得违反。
4. 关键词约定：**必须** = MUST，**禁止** = MUST NOT，**应当** = SHOULD（偏离需记录理由），**可以** = MAY。

## 0. 定义、目标与范围

**一句话定义**：基于统一消息协议（信封）的事件驱动编排器（orchestrator），把异构执行后端——Claude Code / Codex / Kimi 等本地 CLI agent 与直连 API 模型——以可配置角色接入"群聊"式协作线程，端到端完成软件开发任务（PRD → 编码 → 测试 → 合并），支持人工审批门禁与任意时刻崩溃恢复。

**三条核心命题**（全部设计由此推出，实现中遇到取舍时以此裁决）：

1. agent 在两次调用之间不存在。"群聊里的成员"只是被调度器反复调用的无状态函数；触发某个角色 = 从事件队列取出消息，用该角色的定制视图调用一次其后端。
2. 协议是宪法，调度器是执法机构，agent 是可插拔公民。系统的核心资产是信封协议，不是任何一家后端。
3. 真相只在三处：事件日志（发生过什么）、黑板（定下了什么）、worktree（做到了哪）。内存中的一切与 CLI 原生会话都是缓存，**必须**满足：任一会话随时作废，冷启动能无损续上。

**范围内**：信封协议；事件调度（路由 / 聚合 / 并行 / 看门狗 / 终止）；异构适配层（CLI 型与 API 型）；四层上下文组装与 resume 热续；权限强制与系统侧验证；崩溃恢复与幂等；多线程群聊；人工门禁；CLI 用户界面；指标埋点；混沌测试。

**范围外（禁止实现）**：图形界面；多机分布式；远程 CI / webhook（以本地异步命令模拟）；向量检索 / 长期记忆；接入任何 agent 编排框架（langchain / langgraph / autogen / crewai 等一律禁止——本项目的意义就在于自建这一层）。

## 1. 术语表

| 术语 | 定义 |
|---|---|
| 信封 envelope | 系统内唯一通信单元，结构见 §3 |
| 事件 event | 已落盘的信封；事件号 = 线程内自增主键 |
| 线程 thread | 一个独立群聊 / 任务空间，独立命名空间（§9.3） |
| 角色 role | 一份配置：系统提示词片段 + 权限 + 后端绑定（§11） |
| 执行后端 backend | 真正产生回复的实体：CLI agent 或 API 模型 |
| 适配器 adapter | 把某类后端封装为统一 invoke 接口的组件（§7） |
| 黑板 blackboard | 决策类事件的投影：冻结契约 / 已定结论 / 任务状态（§4.6） |
| 派发 dispatch | （事件, 目标角色）二元组的处理生命周期（§4.4） |
| 写域 write_scope | 角色被允许写入的路径集合，能力申报的一部分 |
| 门禁 gate | 不可逆操作前的人工审批点（§10） |
| 看门狗 watchdog | 超时 / 环路 / 轮数三级防护（§5.3） |
| 会话 session | CLI 后端的原生对话（--resume 可续），仅作缓存（§7.5） |
| 可用性 availability | adapter 的 enabled/disabled 两态，全局（跨线程），落盘于适配器状态文件（§5.6.1） |
| 降级路由 fallback | 角色主绑定不可用时，按 fallback 顺序取首个可用 adapter（§5.6.2） |
| 跳闸 trip | 编排器依报错特征或连续失败自动置 disabled（§5.6.3） |

## 2. 总体架构（五层）

```
┌─ 协议层  信封 schema —— 唯一契约，不可变            (§3)
├─ 调度层  队列+路由+聚合+并行+看门狗+终止            (§5)
│          职责：决定"调用谁、给什么内容"（视图组装 §6）
├─ 适配层  invoke(视图)→信封，每类后端一个 adapter    (§7)
│          职责：只管"用什么格式给"
│          输入翻译 / 输出规范化 / 能力申报
├─ 执行层  claude / codex / kimi CLI │ 直连 API │ mock
└─ 状态层  事件日志+派发表+黑板+worktree+会话表       (§4)
```

分层铁律：视图组装（四层结构、第三人称渲染、尾部重锚定）属于调度层，与厂商无关；适配层**禁止**包含任何角色逻辑，只做格式转换与进程管理。

## 3. 协议层：信封

### 3.1 字段定义

字段分两组。**系统字段由编排器权威赋值，禁止信任模型输出中的同名字段**（模型若输出则覆盖）：

系统字段（编排器赋值）：

- `id` int：线程内自增事件号，落盘时由 sqlite 分配
- `thread_id` str
- `ts` float：UTC 时间戳
- `from` str：被调用的角色名 / `human` / `system`——以调度记录为准，不信模型自称
- `re` list[int]：本信封所回应的事件号集合 = 本次派发批次的全部事件号，由编排器按派发上下文赋值（聚合派发时含多个，见 §5.1）
- `meta` dict：tokens_in / tokens_out / duration_s / verify 结果等系统侧记录

作者字段（信封作者提供，经 schema 校验，附录 A）：

- `to` list[str]：目标角色，可空（空 → 兜底路由 §5.2）
- `type` enum：见 §3.2
- `body` str：markdown 正文
- `artifacts` list[str]：引用的文件路径（相对目标仓库根）
- `corr` str|null：门禁 / 长作业关联 ID
- `blackboard_ops` list[op]|null：见 §3.3

路由硬规则：

- 调度器**只**读 `to` 字段决定路由；**禁止**从 body 解析 @。界面显示的 @X 由 `to` 渲染而来，方向必须是信封 → 显示，不可反向。
- 人类消息与系统消息同样是信封，走同一队列，无特殊通道。

### 3.2 type 枚举、发送者约束与保留策略

| type | 语义 | 允许发送者 | 保留策略 |
|---|---|---|---|
| assign | 指派 / 开工 | 任意 | B |
| review | 评审请求 | 任意 | B |
| question / answer | 提问 / 回复 | 任意 | B |
| decision | 决策 / 裁决 / 契约冻结 | can_decide 角色、human | **A** |
| handoff | 交接（附产物） | 任意 | B |
| report | 进度报告 | 任意 | C |
| defect | 缺陷报告 | 任意 | B，计入环路计数 |
| acceptance | 验收（须附系统侧证据 §8.3） | 任意 | **A** |
| gate_request | 门禁申请 | 任意 | A |
| gate_decision | 门禁裁决 | 仅 human | A |
| system | 看门狗 / 回调 / 审计 | 仅编排器 | C |
| terminate | 终止信号 | moderator、tester、human | A |
| chat | 闲聊 | 任意 | D |

发送者约束违规的处理：调度器把该信封降级为 report 落盘，并追加一条 system 审计事件。

保留策略（视图组装 §6 引用）：

- **A** 永久：投影进黑板，所有角色黑板层可见。
- **B** 焦点/背景：与当前角色相关（to 含我 ∨ from 是我 ∨ re 与我的事件相交）→ 焦点窗全文；否则背景层一行摘要。
- **C** 摘要：一律一行摘要进背景层。
- **D** 丢弃：距今超过 chat_ttl（默认 10 个事件）后不再渲染。

### 3.3 blackboard_ops

仅当信封 type ∈ {decision, acceptance, gate_decision} 且 from 角色具有 can_decide 权限（§11.1）时，调度器才应用；否则忽略并追加 system 审计事件。三种 op：

```json
{"op": "set_decision",    "text": "昵称按码点计数, emoji=1"}
{"op": "freeze_contract", "name": "nickname-api", "path": "docs/api-contract.md", "version": 2}
{"op": "set_task",        "key": "backend.impl", "status": "done"}
```

## 4. 状态层：持久化

### 4.1 目录布局

```
orchestrator/                 # 本系统自身
  config.yaml
  adapter_state.json          # 适配器可用性（全局，跨线程；原子替换写，§5.6.1）
  prompts/                    # 各角色提示词模板
  threads/
    t-001/
      events.db               # 事件/派发/会话/作业/指标（单文件 sqlite, WAL）
      blackboard/
        state.json            # 结构化投影（可由日志重建）
        board.md              # 渲染稿，供 prompt 黑板层
      logs/                   # 每次 invoke 的完整输入输出原文（审计）
    t-002/ …
<target_repo>/                # 被开发的目标仓库，独立于本系统
<worktrees_root>/
  t001-backend/ t001-frontend/ …   # git worktree，分支 feat/t001-backend 等
```

### 4.2 真相 / 缓存分类（恢复设计的依据）

| 类别 | 内容 | 重启后处理 |
|---|---|---|
| 真相（落盘） | 事件日志；派发表（含绝对截止时间戳）；黑板文件；worktree（靠 autocommit）；线程元数据；作业表；适配器状态文件（§5.6.1） | 直接装载 |
| 可推导（不落盘） | 每角色四层视图；环路计数；轮数 | 由日志 + 黑板重算 |
| 半真半假 | 会话表 (sid, last_evt, gen) | 装载但一律视为"可能已死"：热续失败 → 冷启动 |

### 4.3 SQLite DDL

```sql
CREATE TABLE events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  sender TEXT NOT NULL,                  -- from 是关键字, 列名用 sender
  to_json TEXT NOT NULL DEFAULT '[]',
  type TEXT NOT NULL,
  body TEXT NOT NULL,
  re_json TEXT NOT NULL DEFAULT '[]',
  corr TEXT,
  artifacts_json TEXT NOT NULL DEFAULT '[]',
  bb_ops_json TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE dispatches(
  event_id INTEGER NOT NULL,
  target TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('pending','dispatching','done','gate_wait','failed')),
  deadline_ts REAL,
  attempts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(event_id, target)
);
CREATE TABLE sessions(
  role TEXT PRIMARY KEY,
  backend TEXT NOT NULL,
  sid TEXT,
  last_evt INTEGER NOT NULL DEFAULT 0,
  gen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE jobs(                        -- 长作业(本地CI等)
  corr TEXT PRIMARY KEY,
  kind TEXT, cmd TEXT,
  callback_to TEXT NOT NULL,
  started_evt INTEGER,
  status TEXT
);
CREATE TABLE thread_meta(key TEXT PRIMARY KEY, value TEXT);
  -- status: running|suspended|terminated; suspend_corr; created_ts …
CREATE TABLE metrics(ts REAL, key TEXT, value REAL, extra TEXT);
```

thread_id 不进表：一线程一个 db 文件，天然隔离。

### 4.4 派发生命周期与落盘顺序（崩溃语义的根）

一条事件 E_n 对目标 T 的成功路径，事务边界用 [ ] 标注：

```
(1) [事务] E_n 追加进 events
          + 为其每个目标插入 dispatches(pending)
          （to 为空的事件在此步直接生成 target=moderator 的派发行）
(2) [事务] 调度器选中 (E_n,T)：status→dispatching，写入绝对截止时间戳
(3) invoke 适配器 ……………………… ← 崩溃高发区，盘上无痕迹
(4) 编排器在 T 的 worktree 执行 autocommit "wip:{T}@E{n}"（§4.5），随后越权审计（§8.2）
(5) [同一事务] 回复信封落盘（系统字段赋值：from=T, re=[n,…]，
    并为回复的目标生成 pending 派发行）
    + (E_n,T) status→done + 更新 sessions(sid, last_evt)
```

与设计讨论稿的差异（有意修正）：讨论稿把"回复落盘"与"标 done"列为两步、靠恢复对账弥合；实现**必须**将两者合并为单事务，直接消除该崩溃窗口。恢复算法（§9.1）仍保留基于 re 的对账检查作为纵深防御。

### 4.5 worktree 与 autocommit

- 线程内每个有写权限的角色一个 worktree：`git worktree add <root>/t001-backend -b feat/t001-backend <base>`。
- 每次 invoke 返回后（第 4 步），编排器在该 worktree 执行：有改动则 `git add -A && git commit -m "wip:{role}@E{n}"`，无改动跳过。commit message 格式固定，恢复与去重（§9.2）依赖它。
- 作用：把 agent 的隐性进度变成共享可见状态；会话死亡后损失的只剩推理过程。
- API 型角色（无 worktree）跳过第 4 步。

### 4.6 黑板 = 决策类事件的投影

- `state.json` 结构：`{contracts: {name: {version, path, frozen_at}}, decisions: [{evt, text}], tasks: {key: status}}`。
- 每次应用 blackboard_ops 后重渲染 `board.md`（渲染格式属 §17）。
- **必须**提供 `rebuild_blackboard(thread)`：清空后重放全部 A 类事件，结果必须与增量维护一致（以单测保证）。恢复时黑板文件缺失或损坏即调用它。

## 5. 调度层

### 5.1 核心循环

```
while thread.status == running:
    batch = 全部 pending 派发行, 按 event_id 升序
    if batch 为空 或 无可调度组: 等待(新事件 / 作业回调 / 门禁恢复 / 适配器状态变更)
    groups = group_by(batch, target)        # 聚合: 同目标多行 → 一次调用
    # 并行判定: 各 target 的 write_scope 两两不相交 → 并行调度;
    # 相交者按组内最小 event_id 串行
    # target=human 的行不 invoke：置 gate_wait 并挂起线程（§10）
    for g in schedule(groups):
        # 生效绑定解析(§5.6.2)；无可用 → 本组保持 pending，本轮跳过
        标 dispatching + deadline = now + timeout(g.target)     # 落盘
        view = 组装视图(g.target, g.event_ids)                  # §6
        env, sess = adapter.invoke(view, session)               # 超时→kill
        if 超时或进程失败:
            attempts += 1
            attempts ≤ 1 → 回到 pending 重派发
            否则 → failed + system 事件 to=[moderator] 报告
            continue
        autocommit + 越权审计(§8.2)   # 审计违规→拒收+reset+审计事件, continue
        schema 校验 env:
            失败 → 携带错误说明对同一批次原地重调一次(不换事件号)
            两次仍失败 → failed + system 事件转 moderator; continue
        [事务] 回复落盘(from=g.target, re=g.event_ids) + done + 会话表
        应用 blackboard_ops(§3.3) → 重渲染 board.md
        验证钩子(§8.3); 终止/门禁/看门狗检查(§5.3–5.5, §10)
```

聚合的动机：同批次同目标只调一次，既省调用，也避免同一角色割裂地回两遍、后一遍看不到前一遍。埋点：每次聚合记录 batch_size（§13）。

### 5.2 触发源（一切触发 = 一条事件入队，共五种）

1. **信封路由**：to 非空。
2. **兜底路由**：to 为空 → 派发给 moderator，它只回答"下一个谁说"（输出信封的 to 字段）。人类插话同样适用。
3. **工具与系统回调**：短工具在 invoke 内同步完成，不产生群事件；长作业（如本地 CI）发起时登记 jobs(corr, callback_to)，完成后编排器插入 system 事件 to=[callback_to]、corr 回填。本项目"CI" = 编排器异步执行配置命令（如全量测试），**禁止**实现远程 webhook。
4. **门禁**：type=gate_request, to=[human] → 线程 suspended（§10）。
5. **看门狗**：三级，见 §5.3。定时依据一律为落盘的绝对时间戳，**禁止**内存倒计时。

### 5.3 看门狗三级

| 级别 | 判定 | 动作 |
|---|---|---|
| 单次调用 | now > deadline_ts | kill 子进程；attempts+1；重试 1 次；再败 → failed + 转 moderator |
| 互@环路 | 同一有序对 (A→B) 的 defect 事件数 ≥ loop_limit（默认 3） | 自动 gate_request 升级人类 |
| 全局轮数 | 线程事件总数 ≥ max_rounds（默认 100） | 自动 gate_request |

环路与轮数每次从日志现数，不落盘。

### 5.4 终止

terminate 信封落盘时不生成派发行（它是信号，不是待办）。它触发终止清单：编排器汇总产物（黑板契约 + 全部 artifacts + 分支列表 + 会话台账）生成一条 system 总结事件；thread status → terminated；此后拒绝新派发。人类可 `orch reopen` 重开。

### 5.5 特权操作与系统执行器

merge 到主干、部署、删除分支等不可逆操作**禁止**由任何 agent 直接执行（工具白名单不含它们）。流程：agent 发 gate_request（corr=gate-xx，body 说明操作与参数）→ 人类批准 → 编排器内置的**系统执行器**按 config 中 gate_ops 的命令模板执行，结果作为 system 事件入队。凭据只存在于编排器环境，不进任何 agent 环境。

### 5.6 适配器可用性与降级路由

**5.6.1 状态与存储。** 每个 adapter 恰有一个可用性状态 `enabled | disabled`，连同 `{reason, by: human|auto, ts, fail_streak}` 存于全局文件 `orchestrator/adapter_state.json`——不进线程 db：额度是供应商级事实，跨线程共享。写入**必须**原子替换（临时文件 + rename）；写者有二：CLI／控制台（人工 enable/disable，§12）与调度器（跳闸与 streak 维护），最后写入者胜，竞态最坏后果是一次多余的人工重设。文件缺失 → 视为全部 enabled（冷启动默认）；文件损坏 → 启动报错，**禁止**猜测（§9 同一哲学）。调度器每轮调度前重读该文件（轮询间隔属 §17）；**禁止**只在启动时读一次。

**5.6.2 生效绑定解析（每次派发时现算，不落盘）。**

```
effective_adapter(role) = [roles[role].adapter] + roles[role].fallback 中首个 enabled 项
```

- 解析发生在 §5.1 标 dispatching 之前；聚合与并行判定不变（它们是角色级概念，与绑定无关）。
- effective ≠ sessions.backend → 视为会话死亡：sid 置空、gen += 1、backend 更新，走冷启动全量组装（§6.1–6.4）。原主恢复 enabled 后，下一次派发自然回归主绑定（同样冷启动）。
- 换绑重派时该派发行 attempts 归零（新后端享有完整重试预算；跳闸单向 + 链长有限，不存在无限循环）。
- 每次 effective ≠ 主绑定，追加一条 system 审计事件（body 含角色、原绑定、生效绑定、原因），**比照 terminate（§5.4）：落盘但不生成派发行**——是通告不是待办。同一（role，生效绑定）连续派发只在首次记录；"首次"判定一律现查日志，**禁止**内存驻留去重状态（§16 第 9 条）。
- 全部不可用 → 该派发行**保持 pending**，本轮跳过；进入此状态的首次追加一条 system 通告事件（同上不生成派发行），CLI 与控制台**必须**显著呈现（§12）。**禁止**对无可用 adapter 的角色空转重试或消耗 attempts；其余角色照常调度，线程不挂起。人工 enable 后 pending 行被主循环自然接手——与 §9.1 "pending 行不需处理"同一机制，零新增派发状态。等待与唤醒见 §5.1 伪代码，**禁止**忙等。

**5.6.3 自动跳闸。** 满足其一即置 disabled（by=auto）：

1. **特征命中**：invoke 传输级报错文本（stderr / 进程退出信息 / 无输出错误）命中该 adapter 的 unavailable_patterns（大小写不敏感子串，默认清单属 §17）→ 立即跳闸。该次失败**不计** attempts；派发行回 pending 并立即按 §5.6.2 重解析（通常由 fallback 接手）。
2. **连续失败**：传输级失败（超时 / 进程失败 / 无法解析出信封）使该 adapter 的 fail_streak += 1，成功 invoke 清零；fail_streak ≥ trip_after（默认 3，可配）→ 跳闸。schema 校验失败**不计入** streak——那是输出质量问题不是可用性问题（§5.1 原地重调路径与 attempts 语义不变，跳闸只是叠加副作用）。

跳闸时追加 system 审计事件（不生成派发行），body 含 adapter、触发条件、原始报错摘要。恢复**仅限**人工 `orch adapter enable`（同时清零 fail_streak）；**禁止**任何形式的自动恢复或冷却重试。

**5.6.4 与既有机制的边界。** 看门狗（§5.3）语义不变，超时既走看门狗路径也计入 fail_streak。崩溃恢复（§9.1）唯一新增：启动时装载 adapter_state.json（真相类，直接装载）；恢复出的行经主循环自然走 §5.6.2 解析，无新对账分支。切换前失败 invoke 留下的脏 worktree，处理与既有重试路径完全一致（§9.2 第 3 层：git status 如实呈现，从现场继续），无新规则。

## 6. 上下文组装（视图渲染）——调度层职责

### 6.1 结构与排布（位置效应：两端强，中间弱）

```
[系统层]   角色身份 / 权限申报 / 输出格式 / 幂等指令     ← 最前
[黑板层]   board.md：冻结契约 + 已定决策 + 任务状态
[背景层]   非焦点消息的一行摘要（过老丢弃）              ← 中部
[焦点窗]   与我相关消息全文，事件号升序
[指令尾]   重锚定 + 本轮指令                             ← 最后
```

### 6.2 各层内容

**系统层**（每角色模板；冷启动全文，热续省略）：

- 身份与职责（来自角色配置的 prompt 文件）；
- 权限申报原文："可写: {write_scope}；可用工具: {tools}；越权写入会被系统整体拒收"；
- 身份声明："以下历史中标注 [{role}] 的发言是你自己说过的话"；
- 输出格式："回复必须以一个 ```json 代码块结束，内容为信封对象（字段 to / type / body / artifacts / corr / blackboard_ops），其余字段由系统赋值"，附一个最小示例；
- 幂等指令："输入事件均带 # 编号；若某编号你已处理过，直接重发当次信封，不要重复执行任何操作"。

**黑板层**：board.md 全文。

**焦点窗**：满足 (to 含我) ∨ (from = 我) ∨ (re 与我的事件相交) 的 B 类事件，全文，事件号升序，渲染格式：

```
#12 [tester→@backend] (defect): 全空格昵称通过校验, 详见 reports/r1.md
```

统一第三人称角色标签，**禁止**保留第一人称原文流——五个角色的"我"混在一起会导致模型认错自己。

**背景层**：其余 B / C 类一行摘要 `#3 [pm→@backend,@frontend] review: PRD v1 发起评审`；D 类超过 chat_ttl 丢弃；超预算按最旧丢弃。

**指令尾**：`你是 {role}。现在只针对 #{ids} 回应：{本轮指令}`。长对话抗角色漂移主要靠这一句，热续时**必须**照发，不得省略。

### 6.3 预算

以能力申报的 context_window 为上限，token 估算方法属 §17（可用近似，但全系统一致）。分配约束：焦点窗 ≥ 50%，黑板 ≤ 20%，背景 ≤ 20%，其余归系统层与指令尾。超预算压缩顺序：先丢背景最旧摘要 → 再截断焦点窗最旧事件正文（保首尾各一段）。

### 6.4 冷启动附加段（仅 CLI 型）

worktree 现场摘要，插在黑板层之后：

```
[现场] git log --oneline -10 ; git status --short ; git diff --stat（截断）
```

作用：让重建的会话知道活干到哪了——若已有 `wip:{role}@E{n}` 提交，说明该事件的工作已完成，只需补发信封（§9.2 去重第三层）。

### 6.5 热续增量提示（仅 CLI 型，会话存活时）

内容 = 新事件全文（带 # 号，第三人称）＋ 黑板 diff ＋ 指令尾。三条规则：

1. 黑板 diff **必须**显式：会话的旧记忆不可编辑，只能覆盖——渲染 last_evt 以来的 A 类变化，前缀"以下决策覆盖旧结论："。
2. 小改增量，大改弃会话：需求被推翻级别的变化 → 主动作废 sid 走冷启动。判定标准：契约 version 变更 ≥ 1，或人类显式指示。
3. 增量中事件号照带，配合系统层幂等指令实现会话端去重。

## 7. 适配层

### 7.1 统一接口

```python
class Caps(TypedDict):
    context_window: int
    tools: list[str]
    write_scope: list[str]
    cost_tier: str            # cheap | mid | expensive
    supports_resume: bool
    timeout_s: int
    max_concurrent: int

class Adapter(Protocol):
    caps: Caps
    def invoke(self, view: RenderedView, sess: Session | None
              ) -> tuple[ModelEnvelope, Session | None]: ...
```

invoke 语义：阻塞至完成或超时；后端内部步数不限（内循环型可自行多步调工具、跑测试）；返回**恰好一个**作者字段信封。调度器不知道、也不需要知道信封背后是一步还是一百步——这是全系统最重要的抽象边界。

### 7.2 CLI 型（内循环 agent：claude / codex / kimi）

- 子进程执行，cwd = 该角色 worktree；权限经 CLI 参数注入（如 --allowedTools）。隔离依赖三件套（§8.1）共同成立，**禁止**假设操作系统级文件沙箱存在。
- 冷启动：`start_cmd + 全量视图（§6.1–6.4）` → 从输出取 session_id，gen += 1。
- 热续：`resume_cmd(sid) + 增量（§6.5）`。resume 报错（如 session not found）→ 视为会话死亡，立即转冷启动；**禁止**对同一 sid 重试 resume 超过 1 次。
- 输出解析：取标准输出中**最后一个** ```json 块解析为作者字段信封；无块或解析失败 → §5.1 的原地重调路径。
- 命令形态示例（flag 以各家 `--help` 实测为准，差异正是适配层要吸收的）：

```bash
# claude 冷启动（取 session_id）
cd <wt> && claude -p --output-format json \
  --allowedTools "Edit" "Write" "Bash(pytest:*)" "$VIEW"
# claude 热续
claude -p --resume <sid> --output-format json "$DELTA"
# codex:  codex exec …   /  codex exec resume <sid> …
# kimi:   形态相同，实测为准
```

### 7.3 API 型（单步）

直连 messages 接口；无会话概念，**永远全量组装**，supports_resume = false。本项目中 API 型角色（moderator）不配工具，保持单步；"适配器自补工具循环"作为扩展点**可以**留空实现。

### 7.4 mock 型（测试用）

脚本化确定性 agent：按 (role, 事件号) 查表返回预置信封；维护副作用台账 ledger（每处理一个事件号追加一行到落盘文件），供混沌测试校验 exactly-once（§9.4）。

### 7.5 会话状态

sessions 表 {sid, last_evt, gen}：last_evt = 已通过增量送达该会话的最大事件号；gen = 冷启动代数（审计用）。更新时机：§4.4 第 (5) 步事务内。重启后一律视为"可能已死"。

### 7.6 适配器三件职责（回顾）

输入翻译（视图 → 该家原生格式）／输出规范化（强制合法信封，失败退回）／能力申报（Caps，静态配置于 config.yaml）。新增一家供应商 = 新增一个 adapter 文件 + 一段配置，调度层零改动（§13 有对应埋点）。

输出规范化职责扩展（§5.6）：invoke 的错误报告**必须**区分传输级失败与额度类失败（依 unavailable_patterns 识别，识别责任在适配层）；调度器只消费分类结果，**禁止**在调度层散布各家报错文案的字符串匹配。

## 8. 权限与验证（不信汇报，只信系统侧）

### 8.1 权限强制三件套（全部在 prompt 之外）

1. worktree 隔离：每角色只挂载自己的 worktree；
2. 工具白名单：适配器配置注入 CLI 参数——tester 的 write_scope 仅 tests/ 与 reports/，不含 src；
3. 系统侧 diff 审计：见下。

prompt 中的权限申报（§6.2）只是告知，不是防线。

### 8.2 diff 越权审计（每次 CLI invoke 后必跑）

`git diff --stat {上个合法commit}..HEAD` 的触及路径**必须** ⊆ write_scope。违规处理（简化决策，有意为之）：整体拒收该信封，`git reset --hard` 回上个合法 commit，追加 system 审计事件转 moderator。不做部分裁剪——部分保留需要语义级判断，复杂度不值。

### 8.3 验证钩子（acceptance 的硬证据）

- config 可为角色配置 post-invoke verify：`{cmd, cwd}`（cwd 支持 `{worktree:role}` 与 `{target_repo}` 占位）。
- 当该角色发出 acceptance 信封时，编排器**亲自执行** verify 命令，把退出码与输出摘要写入该信封 meta.verify。
- 对配置了 verify 的角色，**meta.verify.exit_code == 0 是 acceptance 生效的必要条件**；否则调度器将其降级为 report 并追加 system 提示。未配置 verify 的角色 acceptance 原样放行——验收强度由配置层决定，关键验收角色应配置 verify。
- 原则：agent 的"我测过了"只作路由信号；验收以编排器执行的可复现检查为准。

## 9. 崩溃恢复

### 9.1 恢复算法（启动时对每线程机械执行）

```
装载 events / dispatches / thread_meta；黑板缺失或损坏 → rebuild_blackboard
若 thread status = suspended → 保持挂起（gate_wait 行不动），只等 gate_decision
for 每行 status = dispatching 的 (E_n, T):
    a) events 中存在 sender=T 且 n ∈ re 的回复 → 补标 done      # 纵深防御
    b) now > deadline_ts                        → 走看门狗路径（计一次 attempt）
    c) 其余                                     → status → pending，重派发
pending 行不需处理——恢复后的主循环自然接手
计数器（环路 / 轮数）由日志重数；恢复主循环
```

注意：门禁行的状态是 gate_wait 而非 dispatching（§10），因此不会落入上面的循环——这正是"挂起可整体停机"能成立的原因。

**禁止**在恢复逻辑中出现任何"猜测"：每一步只允许查表与数日志。

### 9.2 重派发安全（幂等分层）

队列语义为至少一次投递。去重责任分三层：

1. **编排器**：§9.1 的 a) 覆盖"回复已落盘但派发行未标 done"的情形（合并事务后理论上不出现，保留作防御）；
2. **活会话**：增量带事件号 + 系统层幂等指令 → 见重复编号原样重发上次信封，不重做操作；
3. **死会话**：冷启动现场摘要中 git log 若已有 `wip:{T}@E{n}` → 视图中显式指示"该事件工作已完成，只需补发信封"；最坏情况（干了活未 commit）worktree 是脏的，git status 如实呈现，从现场继续。

日志 / 黑板 / worktree 三处冗余保证：会话死亡只损失推理过程，不损失事实。

### 9.3 多线程命名空间

- 一线程一目录一 db（§4.1）；"选择群聊" = 渲染哪个 thread 的日志；调度器多路复用多条独立队列，线程间**可以**并发。
- 同一角色在两个线程 = sessions 表各自独立一行（表在各自线程的 db 内，天然隔离）；分支名带线程前缀防撞。
- **可以**实现 per-adapter 全局并发信号量（caps.max_concurrent），限制同一 CLI 的并发进程数。

### 9.4 不变量与混沌测试（两层验证，有意区分）

不变量：任意时刻 kill -9，重启后每个 dispatching 行必落入 §9.1 的 a–c 之一（gate_wait 行随线程挂起保持），且每个事件的副作用恰好生效一次。

- **第一层（必须，mock 后端）**：混沌 harness 对脚本化任务（附录 B）注入 SIGKILL——两种模式：故障注入钩子精确覆盖 §4.4 各步间隙 + 纯随机时刻。每轮重启续跑至终止。校验：mock ledger 无重复事件号 + 终态产物与不中断基准逐字节一致。**≥ 50 轮，通过率必须 100%**。
- **第二层（真实后端，可选跑）**：真实模型输出非确定，终态不可能逐字节一致；只统计"崩溃恢复后任务仍完成且验收通过"的完成率，作为指标上报（§13）。

## 10. 门禁与人类参与

- 人类是一等参与者：`orch send` 产生 from=human 的信封，走普通队列，无特殊通道。
- gate_request（to=[human]）：调度器遇到 target=human 的 pending 行时置 status=gate_wait，thread status=suspended 并落盘；corr 缺省时由编排器生成 `gate-{事件号}`。此时**整个进程可以退出**，挂起不消耗任何资源。
- `orch approve|reject <corr>` → 产生 gate_decision 事件（from=human，corr 回填，to = 原 gate_request 的 sender，让申请者知道裁决并续走流程），同时把对应 gate_wait 行标 done → 线程 resume。approve 且该 gate 关联特权操作 → 系统执行器执行（§5.5），结果以 system 事件入队。
- 验收场景之一（M2）："停机三小时"——gate_wait 期间 kill 进程，任意时长后重启 + approve，流程无损继续。

## 11. 角色与配置

### 11.1 config.yaml 完整示例

```yaml
target_repo: /path/to/project
worktrees_root: /path/to/worktrees
thread_defaults:
  max_rounds: 100
  loop_limit: 3
  chat_ttl: 10
gate_ops:                       # 特权操作命令模板（系统执行器专用）
  merge_main: "git -C {target_repo} merge --no-ff {branch}"
  run_ci: {cmd: "pytest -q", cwd: "{target_repo}", async: true}   # 本地CI模拟
adapters:
  claude_cli: {kind: cli, start_cmd: "claude -p --output-format json …",
               resume_cmd: "claude -p --resume {sid} …",
               timeout_s: 600, max_concurrent: 2}
  codex_cli:  {kind: cli, start_cmd: "codex exec …",
               resume_cmd: "codex exec resume {sid} …", timeout_s: 600}
  kimi_cli:   {kind: cli, start_cmd: "kimi …", resume_cmd: "…", timeout_s: 600,
               unavailable_patterns: ["quota", "insufficient", "rate limit", "429", "额度"],
               trip_after: 3}
  cheap_api:  {kind: api, model: "<低成本模型>", timeout_s: 120}
  mock:       {kind: mock, script: tests/fixtures/like_feature.yaml}
roles:
  moderator: {adapter: cheap_api, can_decide: true,  write_scope: [], tools: [],
              prompt: prompts/moderator.md}
  pm:        {adapter: kimi_cli,  fallback: [claude_cli], can_decide: true,
              write_scope: [docs/], tools: [Edit, Write], prompt: prompts/pm.md}
  backend:   {adapter: claude_cli, can_decide: false, write_scope: [server/],
              tools: [Edit, Write, "Bash(pytest:*)"], prompt: prompts/backend.md}
  frontend:  {adapter: codex_cli, can_decide: false, write_scope: [web/],
              tools: [Edit, Write], prompt: prompts/frontend.md}
  tester:    {adapter: codex_cli, can_decide: false,
              write_scope: [tests/, reports/],
              tools: [Edit, Write, "Bash(pytest:*)"],
              verify: {cmd: "pytest -q", cwd: "{worktree:backend}"},
              prompt: prompts/tester.md}
```

start_cmd/resume_cmd 支持 `{cwd}` 占位：argv 分词之后逐 token 字面替换为该角色 worktree 绝对路径；含空格路径恒单 argv 元素；未出现占位时 argv 逐字节不变；不作用于自动注入的工具参数与视图正文。

可用性与降级字段（§5.6）：`fallback` 为有序列表，缺省 `[]`（无备胎：不可用即等待人工处理）；`unavailable_patterns` / `trip_after` 为 adapter 级可选字段（缺省值属 §17）。装载时校验，违者启动报错：fallback 项必须是已声明的 adapter；tools 或 write_scope 非空的角色，其主绑定与 fallback 项**必须**为 cli 型（API 型不带工具循环，§7.3）。moderator **建议**配置非空 fallback（它不可用会阻塞兜底路由）。

角色 = 配置。内置五角色只是预置文件；自定义角色即新增一段配置 + 提示词文件。**禁止**把任何角色逻辑写死进代码（moderator 的兜底地位除外——它是调度层机制的一部分）。同一角色可通过改一行 adapter 绑定在供应商之间热替换。

### 11.2 moderator 提示词要点

只做一件事：读黑板与最近事件，输出信封，to 填"下一个该谁"，body 一句话理由。**禁止**其产出任何实体工作（代码 / 文档 / 评审意见）。绑定 cheap API，无会话、无工具、无 worktree。

### 11.3 角色提示词模板骨架（prompts/*.md）

三段固定结构：职责边界 ／ 交接产物与格式 ／ 何时向谁交接。具体文案属 §17 开放决策点，但**必须**遵守三段结构。

## 12. 用户界面（CLI）

| 命令 | 行为 |
|---|---|
| `orch run` | 启动调度进程（常驻，可随时 kill） |
| `orch new "任务" [--roles …]` | 建线程：目录 / db / worktrees，E1 入队 |
| `orch send t-001 "…" [--to role]` | 人类发言入队 |
| `orch chat t-001 [--follow]` | 把事件日志渲染成群聊（气泡 = 信封投影；@ 由 to 渲染） |
| `orch threads` / `orch status t-001` | 列线程 / 看单线程状态与派发表 |
| `orch approve\|reject <corr>` | 门禁裁决 |
| `orch attach t-001 backend` | 打印该角色原生会话的交互接入命令（如 `claude --resume <sid>`），供人工现场勘查 |
| `orch replay t-001` | 按日志逐事件重放渲染（审计） |
| `orch metrics [t-001]` | 输出 §13 全部指标 |
| `orch bench resume <fixture>` | 同任务开 / 关 resume 各跑 N 次的 token 对比实验 |
| `orch reopen t-001` / `orch stop` | 重开已终止线程 / 优雅停机 |
| `orch adapters` | 列全部 adapter：状态 ✅/⛔、reason、by（手动/自动）、ts、fail_streak（§5.6） |
| `orch adapter disable <name> [--reason …]` | 人工标记不可用（调度器停止选中） |
| `orch adapter enable <name>` | 人工恢复，清零 fail_streak |

运维分工（必须体现在实现里）：事件日志用于回放审计，原生会话用于现场勘查——attach 是纯 API 方案给不了的红利，**必须**实现。

可用性呈现（§5.6）：`orch status` 与控制台的角色行显示当前生效绑定，主绑定被禁用时显示 ⛔ 与生效备胎；存在"无可用 adapter"的阻塞角色时**必须**显著警示。控制台**必须**为每个 adapter 提供 enable/disable 开关按钮（disable 可填 reason），行为与 CLI 两命令等价（同一原子替换写路径）。

## 13. 指标埋点（采集点随代码一起交付，禁止事后补测不可复算的数字）

| 指标 | 采集点 | 计算 |
|---|---|---|
| 端到端任务数 / 平均轮数 / 成本 | events 表 + 每次 invoke 记 tokens 与费用（metrics 表） | 汇总 |
| 聚合节省调用 % | 每次聚合派发记 batch_size | Σ(batch_size − 1) ÷ 总调用数 |
| 首次合法率 % | 每次 schema 校验失败重调记一条 | 1 − 退回次数 ÷ 总调用数 |
| 背景层压缩比 | 渲染时记 原文 token / 摘要 token | 均值 |
| resume 输入 token 节省 % | `orch bench resume`：同一 fixture 任务开 / 关 resume 各跑 ≥ 3 次 | 输入 token 均值差（单次无意义，模型输出非确定） |
| 混沌轮数与两层结果 | harness 输出 | mock 层通过率（须 100%）／真实层完成率 |
| 新增供应商适配器行数 | cloc 单文件 | 从第 3 家起算（前两家的成本花在打磨抽象上） |
| 降级切换次数 | 每次 effective ≠ 主绑定的派发记一条 | 计数，按 (role, adapter) 分组 |
| 自动跳闸次数 | 每次 auto 跳闸记一条 | 计数，按触发条件分类 |

所有原始量落 metrics 表；`orch metrics` 汇总输出。**禁止**输出任何无法从原始量复算的数字。

## 14. 技术选型与工程约束

- Python ≥ 3.11。依赖白名单：pyyaml、pydantic（或 jsonschema）、typer（或 click）、pytest。**禁止** ORM（直接用 sqlite3），**禁止**一切 agent 编排框架（§0）。
- 并发：asyncio；子进程用 asyncio.subprocess；超时统一由绝对截止时间戳换算，不用相对计时器。
- sqlite：WAL 模式；凡多写操作显式事务（§4.4 的事务边界必须严格对应代码）。
- 每次 invoke 的完整输入 / 输出原文落 `threads/t-xxx/logs/`（文件名含事件号与角色），审计是一等公民。
- 模块划分建议：`protocol/ store/ scheduler/ render/ adapters/ verify/ cli/`。
- 全程类型标注；store / scheduler / render / protocol 四个核心模块**必须**有单元测试。

## 15. 里程碑（严格按序；每个里程碑先写验收测试）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M0** 协议 + 存储 + 核心环 | 信封 schema 与校验；全部 DDL；落盘顺序；mock 适配器；最小调度环（单线程串行） | 附录 B 脚本任务在 mock 下 E2E 跑通；恢复对账全情形（§9.1：挂起保持 + a–c）单测全绿 |
| **M1** 视图 + 看门狗 + 终止 | 四层组装 / 第三人称渲染 / 尾部重锚定；moderator（先 mock）；三级看门狗；终止清单 | 视图渲染快照测试；环路与轮数上限的触发路径测试 |
| **M2** 真实后端冷启动 + 权限 + 门禁 | claude / codex / kimi / API 适配器（仅冷启动）；权限三件套；门禁与系统执行器 | ≥ 2 家异构后端（以本机实际安装为准）完成 1 个小功能全流程，含 1 次人工门禁与"停机后重启续跑"；越权写入注入测试被拦截 |
| **M3** resume + 聚合 + 并行 + 多线程 | 热续增量；同目标聚合；写域并行；本地 CI 回调；多线程并发 | `orch bench resume` 产出对比报告；双线程并发互不干扰测试 |
| **M4** 混沌加固 + 指标 | 故障注入钩子覆盖 §4.4 全部间隙；≥ 50 轮混沌；metrics 汇总 | mock 层 100% 通过；`orch metrics` 能输出 §13 全表 |
| **M5** 适配器可用性 + 降级路由 | 全局状态文件；手动 enable/disable；生效绑定解析与换绑冷启动；自动跳闸（特征 + 连续失败）；阻塞等待与通告；mock 适配器支持脚本化额度故障；CLI 三命令与控制台开关按钮；指标两项（§5.6） | mock 下：disable 主绑定 → fallback 接手完成附录 B 任务，终态与不中断基准一致；无 fallback 角色阻塞等待、enable 后续跑完成；注入额度报错 → 自动跳闸 + 降级接手 E2E；连续失败跳闸路径单测；切换间隙 kill -9 混沌 ≥ 20 轮 100% 通过（§9.4 第一层扩展场景）；`orch adapters` 输出与状态文件一致；两项指标可复算 |

实现顺序铁律：全量组装（M0–M2）先于 resume 增量（M3）——API 型、冷启动、换供应商三条路径都只依赖全量路径，它是地基；resume 只是省 token 的优化，不是可依赖的存储。

## 16. 反模式清单（硬约束，违反即缺陷）

1. 从正文解析 @ 做路由（必须只认 to 字段）
2. 看门狗用内存倒计时（必须用落盘的绝对时间戳）
3. 任何形式的全员广播派发
4. 把权限写进 prompt 当强制手段（prompt 只是告知）
5. 采信 agent 自述作为验收依据（必须系统侧 verify）
6. 向多个角色发送同一份对称的全量历史（必须不对称渲染）
7. 历史保留第一人称原文流（必须第三人称角色标签）
8. 把 CLI 会话当真相存储（必须随时可作废、冷启动无损）
9. 编排器在内存中持有任何不可从盘上重建的状态
10. 恢复逻辑包含猜测（只允许查表与数日志）
11. 由模型自报 from / re / id 等系统字段（必须编排器权威赋值）
12. 任何 agent 直接执行 merge / 部署等不可逆操作（必须走门禁 + 系统执行器）
13. 引入 langchain / langgraph / autogen / crewai 等编排框架

## 17. 开放决策点（实现者自决，记录于 IMPLEMENTATION_NOTES.md）

board.md 的具体渲染格式；token 估算方法（tiktoken 近似或字符系数皆可，但全系统必须一致）；CLI 输出中定位最后一个 json 块的具体解析策略；并行调度的 asyncio 结构（TaskGroup 组织方式）；五个内置角色提示词的完整文案（须遵守 §11.3 三段结构）；bench 用 fixture 任务的选择；per-adapter 并发信号量默认值；适配器状态文件轮询间隔；unavailable_patterns 默认清单；adapter_state.json 的具体字段编排（须含 status/reason/by/ts/fail_streak）。

## 附录 A：信封作者字段 JSON Schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "additionalProperties": false,
  "required": ["to", "type", "body"],
  "properties": {
    "to": {"type": "array", "items": {"type": "string"}},
    "type": {"enum": ["assign", "review", "question", "answer",
                      "decision", "handoff", "report", "defect",
                      "acceptance", "gate_request", "gate_decision",
                      "system", "terminate", "chat"]},
    "body": {"type": "string", "minLength": 1},
    "artifacts": {"type": "array", "items": {"type": "string"}},
    "corr": {"type": ["string", "null"]},
    "blackboard_ops": {
      "type": ["array", "null"],
      "items": {
        "type": "object",
        "required": ["op"],
        "properties": {
          "op": {"enum": ["set_decision", "freeze_contract", "set_task"]},
          "text": {"type": "string"},
          "name": {"type": "string"},
          "path": {"type": "string"},
          "version": {"type": "integer"},
          "key": {"type": "string"},
          "status": {"type": "string"}
        }
      }
    }
  }
}
```

系统字段（id / thread_id / ts / from / re / meta）不在此 schema 内：编排器赋值，模型输出中的同名字段一律丢弃。发送者约束（§3.2）在 schema 校验之后单独执行。

## 附录 B：mock 验收任务的期望事件序列（"点赞功能"fixture）

确定性脚本，供 M0 验收与混沌基准。期望序列（类型层面，允许事件号因实现细节偏移，但顺序与类型必须一致）：

```
E1  human → ∅           (assign)      "帖子支持点赞/取消赞"
E2  moderator → pm      (assign)      兜底路由生效
E3  pm → backend,frontend (review)    PRD + 契约v1  [bb: freeze_contract v1]
E4  backend → pm        (question)    "重复点赞语义?"     ← 与 E5 同批聚合测试点
E5  frontend → pm       (answer)      "无异议"
E6  pm → moderator      (decision)    幂等语义裁决  [bb: freeze_contract v2]
E7  moderator → backend,frontend (assign)  写域不相交 → 并行测试点
E8  backend → tester    (handoff)     附 artifacts
E9  frontend → moderator (report)     "mock 完成待联调"
E10 tester → backend    (defect)      已删帖子返回500 → 环路计数 1
E11 backend → tester    (handoff)     修复
E12 tester → moderator  (acceptance)  meta.verify.exit_code=0 必须存在
E13 moderator → human   (gate_request corr=gate-01)  → gate_wait + 线程 suspended
E14 human → moderator   (gate_decision corr=gate-01, approve)
    → 系统执行器执行 merge_main，并发起 run_ci（jobs 表登记 corr=job-01）
E15 system → moderator  (system corr=job-01)  CI 回调
E16 moderator → frontend (assign)     rebase + 切真实接口
E17 frontend → tester   (handoff)
E18 tester → moderator  (acceptance)  meta.verify.exit_code=0
E19 moderator → ∅       (terminate)   → 不生成派发行，触发终止清单 system 总结事件
```

harness 断言四项：事件类型序列一致；黑板终态一致（contracts v2 冻结、tasks 全 done）；mock ledger 无重复事件号；混沌模式下终态与不中断基准逐字节一致。

——全文完——
