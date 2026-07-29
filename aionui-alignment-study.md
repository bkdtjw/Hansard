# orch 控制台对齐 AionUi 团队界面——调研报告

> **产出**：workflow `wf_25c86736-f95`（10×opus：盘点 4 → 特性设计 4 → 综核 2）。「盘点:stream」因 API 断流失败，其结论由 F1 设计师亲读源码补位，并经核对员复验。
> **核对**：核对员亲验 30 条主张（25 确认 / 5 判有误，修正已吸收进正文，见 §6）。
> **Lead 复核**（本报告采信前逐条打开文件亲验，12/12 属实）：spec:24、spec:580、spec:603、spec:396；core.py:1152-1155；store/\_\_init\_\_.py:113-114、:256-260、:263-277；duration_s 全仓仅 server.py:122 注释、无写入点；app.js:845、:1198-1216、:1021；SPEC-AMENDMENT-M5-draft.md 存在。
> **立案说明**：正文 §4 候选问题 1（§0 矛盾）属 CLAUDE.md 强制报告项，已由 Lead 立案为 QUESTIONS.md **Q10**；候选 2–6 维持未立案，待立项时取用。
> **附带发现**：app.js 内含 NUL 字节（≈offset 30927），ripgrep 将其判为二进制文件、全部文本检索工具失效——已另立后台任务排查，不影响本报告结论。

## 1. 结论

**能对齐，且形似度可达 80%，代价出人意料地低——四个特性的推荐路径全部为「零触碰冻结面」，合计约 3 张任务卡。** 原因是本仓的能力缺口主要不在协议层，而在**读投影层与呈现层**：AionUi 界面所需的四类数据（执行态、成员名册、结构化待办、耗时统计）里，三类已在盘上或已在协议里（`dispatches.status` 五态、`bb_ops.set_task`、事件 `ts`/`sender`），只是被 `_ep_thread_status` 的 `WHERE status='pending'` 滤掉、被前端按字典序排了、或从未被派生。唯一真正缺数据的是「invoke 内部的工具调用步骤」，而它恰好卡在一处**既有 spec 合规缺口**上——`logs/` 今天落的不是 stdout 原文（`src/orch/scheduler/core.py:1152-1155`），补上这个债即拿到数据。

**但有两处必须先由人裁决、无法自行放行**：(a) spec §0:24「范围外（禁止实现）：图形界面」与 §12:580「控制台**必须**提供 enable/disable 开关按钮」正面冲突，M5 增补未触 §0——按 CLAUDE.md「发现 spec 内部矛盾：停下报告」，这条卡住全部四个特性（已立案 Q10）；(b) §7.1:396「调度器不知道、也不需要知道信封背后是一步还是一百步」的适用范围，决定 F1 能否显示 View Steps。

**推荐节奏**：先裁两个问题 → 落 F4+F2 合卡（数据源复活 + 通讯录，收益最大、风险最低）→ 落 F3（待办与统计，纯呈现+派生）→ F1 视 §7.1 裁决结果决定做全量还是退到「派发→回复」粒度。

## 2. 四特性总览

| 特性 | 现状 | 最小可行路径 | 动 spec | 工作量 | 优先级 |
|---|---|---|---|---|---|
| F4 角色状态泡 | 数据在盘上，读投影滤掉；3 处前端死代码 | 新增只读 `dispatches_snapshot()`，/status 全五态+deadline_ts | 否 | 0.5 卡 | P0 |
| F2 通讯录+单聊 | 定向发送已闭环；过滤只匹配 sender | app.js:845 判据加 `to` 匹配 + 渲染常驻名册 | 否 | 0.5 卡(与F4合) | P0 |
| F3 待办+统计 | set_task 已渲染但字典序；「轮」无定义 | 按声明序排+勾选态；服务端派生 round_stats | 否 | 1 卡 | P1 |
| F1 执行流可视化 | logs/ 无原文（合规缺口）；无耗时字段 | 补 stdout 原文落盘 + /steps 只读端点 | 否* | 1 卡 | P2 |

\* F1 不改 spec 正文，但需先裁 §7.1 边界；其第②项属「修既有合规缺口」而非新增能力。

## 3. 各特性详情

### F4 角色状态泡（P0，推荐先做）

**差距**：唯一缺口是「invoke 进行中」——`dispatches.status='dispatching'` 早在 DDL CHECK 里（`src/orch/store/__init__.py:113-114`，与 spec:176-177 逐字同形），`mark_dispatching` 早已落 status + deadline_ts 并 commit（`store/__init__.py:263-277`），但 web 唯一读路径 `_ep_thread_status`（`src/orch/web/server.py:171-185`）只调 `store.pending_dispatches()`，其 SQL 硬编码 `WHERE status='pending'`（`store/__init__.py:256-259`）。直接后果是三处前端死代码：`updateTypingBar`（`app.js:1199` 筛 `d.status === "dispatching"`）、`renderDispatchSummary`（`app.js:1215/1217` 统计 dispatching/failed），DOM 与 CSS 均已就位（`index.html:130`、`styles.css:396`）。

**方案对比**：A 扩投影（新增 `dispatches_snapshot()`，不改 `pending_dispatches`）／B 再加 workspace 级 `/api/roster` 跨线程聚合／C 纯前端保守版。

**推荐 A**。C 做不到核心诉求——绿点会退化成「有待办排队」，语义错误比没有更坏。B 的跨线程聚合每拍要扫全部 `t-*` 目录逐个开 sqlite（`cli/main.py:132-139`），叠在已有的 `/api/threads` N 次开 Store 之上，是当前最重的轮询热点，建议先量后决。

**三条硬约束（写进卡）**：① **禁改 `pending_dispatches()`**——它有 7 处调用点，其中 `core.py:866` 驱动主循环、`core.py:1064` 与 `async_core.py:292` 拿它做「本组尚未 mark_dispatching」的新鲜度判据；② **绿点必须判 deadline_ts**——进程崩溃后 dispatching 行会滞留在盘上，`watchdog.py:203-205` 注释明写「行留在盘上会被每一轮 check 重新枚举」，不判 deadline 就长亮假绿；③ **服务端零缓存**，每请求现查盘（§16.9；spec:306 已给同类判词「一律现查日志，禁止内存驻留」）。

**「当前 Leader」建议不造**——系统无当前发言人概念，pending target 是集合。落地为两枚有依据的徽标：moderator 挂「兜底路由」（spec:269、spec:549），`can_decide=true` 挂「可裁决」（spec:530-533，moderator 与 pm 均为 true，是集合非单人）。

### F2 通讯录与专人单聊（P0，与 F4 合卡）

**差距**：左栏是线程列表（`app.js:405-450`）不是通讯录；`renderRoleBindings`（`app.js:287-304`）明确「一切正常时一枚不渲染」，与「每人恒有一个状态点」正相反。核心缺陷是**过滤只匹配 sender**：`app.js:845` `!filterState.roles.has(ev.sender)`，不看 `ev.to`——选中 backend 时看不到「发给 backend」的消息，单聊只有半边。而 events 端点已回 `to`（`server.py:117`），故纯前端一行即可修正。

**已闭环可复用**：定向发送整链（`#send-to` 下拉 → `sendMessage` app.js:1546-1569 → `_ep_thread_send` server.py:188-197）；网页 send 与 CLI send 确为同一落盘路径（`server.py:196` 与 `cli/main.py:654-656` 调同一 `Store.append_event`），差异仅在线程不存在时 CLI 静默新建、web 返 404。

**推荐 A（纯前端）+ F4 的数据源改造**，拒绝「后端出单聊投影端点」方案——把 §6.2 焦点窗判据（`render/__init__.py:157-164`）复用到人类展示，会把 spec 的 MUST 面（spec:342-345）与 UI 审美绑死，埋下「改 UI 即改 spec」的雷。单聊过滤应另立具名展示判据 `isMemberRelated(ev, role)`，注释显式声明不是焦点窗、不得回流调度。

**唯一高危点**：@ 成员**必须**实现为「点击成员 → 写 `#send-to.value`」，**禁止**正文 regex 解析——§16 第 1 条（spec:622）+ spec:87「方向必须是信封 → 显示，不可反向」。仓内已有同向先例可抄：`app.js:522-523` 注释「首项文案不得出现广播式措辞」。

**须在 UI 诚实表达**：单聊发一句 = 给该成员排一条 pending 派发行（`store/__init__.py:238-245`），要等下一次 run 才真正 invoke（`core.py:866`）。不是 IM 语义。复用现成 `#send-hint`（`index.html:134`）。另：发送目标只能来自 thread roles 白名单——未知 target 会撞 `adapters[target]` KeyError（`core.py:1055`）或让该行永久 pending（`core.py:896-905`）。

### F3 结构化待办与本轮统计（P1）

**差距**：待办的结构化载体已存在且已渲染——spec §3.3:124 `set_task` → §4.6:230 `state.json.tasks` → `store/__init__.py:666` → 前端 `app.js:1009`。缺的只有三样呈现层能力：(a) 顺序——`app.js:1021` `Object.keys(tasks).sort()` 是字典序不是声明序；(b) 勾选字形与「第 N/M 步」进度头；(c) status 是自由字符串（`protocol/schema.py:57` 无枚举），无归一化。

统计侧「轮」在系统里零定义：`run_thread`（`core.py:836`，docstring 在 :841）跑到终态才返回，前端 `runOnce` 发的 `{once:true}`（`app.js:1584`）服务端根本不读（`server.py` 全函数 :200-226 不读 body）。

**推荐 A**：待办按「首次声明的事件号」排序 + 勾选字形 + 进度头；统计卡由服务端新增纯函数 `_round_stats(events)` 派生，窗口锚点取**最后一条 `sender=='human'` 的事件**（可从盘上重建、刷新不丢、语义即「自我上次说话以来」），耗时 = 窗口内 `last_ts - first_ts`，步骤 = 窗口事件数。派生逻辑放服务端是为了能用现有 pytest+HTTP 打真值——全仓无 JS 单测，`tests/test_web.py:116-120` 对 app.js 只做「含 `fetch(`」这类字符串断言。

**否决「prompt 固定 markdown checklist + 正文解析」**：关键不是字面违规，而是**同仓已有反例**——`server.py:125-128` 白纸黑字记录了黑板走结构化 bb_ops 而非正文解析的理由，`app.js:741-749` 已用「⚠ 无系统侧验证」红牌表达「模型自述不算数」的产品态度。再开一条正文解析通道，等于在同一界面同时表达两种真相观。

**统计卡第三栏需改口径**：对标界面的「工具 36」在本系统盘上**任何位置都无痕迹**——`logs/` 今天也没有（见 F1）。建议改为「invoke 次数」（窗口内 `sender ∉ {human, system}` 的事件数）并在 UI 明写口径。

### F1 执行流可视化（P2，待 §7.1 裁决）

**差距**：中间事件有四道闸。最致命的是第 4 道——**审计日志本身没有原文**：`core.py:1152-1155` 与 `async_core.py:372-375` 调 `store.write_invoke_log(..., output_text=str(raw_env))`，而 `raw_env` 来自 `adapters/__init__.py:539` 的 `_strip_to_author_fields`（:362-364 只留 6 个作者字段），落盘的是一段 Python dict repr。这与 spec §14:603「每次 invoke 的**完整输入 / 输出原文**落 `threads/t-xxx/logs/`」直接冲突，是一处既有合规缺口。

耗时数据同样不存在：全仓 `grep duration_s` 仅命中 `server.py:122` 一处注释，无任何写入点；事件 meta 唯一写入是 `core.py:607` `meta["verify"]`。故 `buildMetaTip`（`app.js:752-760`）读的三项全是死代码，`server.py:122` 注释失实。

**推荐 A**：① `CliAdapter` 存 `last_raw_output`；② `write_invoke_log` 落真原文（**修既有合规缺口**，不走 §17 自决通道）；③ /status 并入 dispatching 行 + 新增只读 `/steps` 端点；④ 前端接活「正在处理中（mm:ss）」胶囊 + View Steps 折叠组。计时 t0 无需新字段——`t0 = deadline_ts − timeout_s`。

**B（真流式旁路，改按行读 stdout）降级为后续卡**：增量收益只是把步骤延迟从「invoke 结束后」压到约 1.5s，代价是重写全系统唯一的真实后端进程读取路径——管道死锁（必须同时消费 stderr）、超时后 kill 与排空次序、Windows 编码三处都有既往伤（`adapters/__init__.py:497` 注释「Windows 默认 gbk 会乱码」+ `tests/test_subprocess_encoding.py`）。**C（落 events.db）明确否决**：三个冻结面全中，且 §4.4 事务(3)「崩溃高发区，盘上无痕迹」（spec:212）是 §9.1 全部恢复分支的前提。

**限制（核对员补充）**：View Steps 的逐行事件素材仅对 `wire_format ∈ {stream-json, opencode-stream}` 存在；`"json"` 分支（`adapters/__init__.py:341-343`）整段 stdout 是单个 JSON 对象、`"text"` 分支（:359）直出——本机可用后端 grok/claude 用 `"json"`，**View Steps 对 3 个后端里的 2 个会是空折叠组**，UI 须为此设计空态。

**跨拓扑通道只有一条**：控制台点「运行一轮」时 `_ep_thread_run` 在 serve 进程的 HTTP 工作线程里同步跑（`server.py:225`，ThreadingHTTPServer `:728` 保证轮询仍被服务），但 CLI `orch run` 是独立进程。故唯一同时覆盖两种拓扑的是**盘上文件 + 既有 1.5s 轮询**；仓内已有两处同款先例：`server.py:211` 自陈「无需任何推送通道」、`orch.stop` 标志（`:280-285`）。全仓无 SSE/WebSocket。

## 4. spec 修订清单与 QUESTIONS.md 候选

> **候选 1 已立案为 Q10（CLAUDE.md 强制报告项）；候选 2–6 未立项前不写入 QUESTIONS.md，仅供裁决时取用。**

**修订清单（仅 F1-C / F3-a 等完整路才需要，推荐路径均不需要）**：
- 若落 events.db：§4.3 DDL(:157-201) 加表 或 §3.2 枚举(:92-106)+保留策略(:110-115)+附录 A(:640-677) 加型，且 §4.4(:203-219) 与 §9.1 前提需同步修订，§6.2 背景层须显式排除新型。
- 若给 set_task 加 order/label：§3.3(:119-125) + 附录 A(:640-677) 两处必须同改（`protocol/schema.py:3-5` 要求与附录 A 逐字一致）。

**候选问题（按裁决紧迫度排序）**：

1. **【已立案 Q10·卡住全批】** spec:24「范围外（禁止实现）：图形界面」vs spec:580「控制台**必须**提供 enable/disable 开关按钮」正面冲突。SPEC-AMENDMENT-M5-draft.md 的 A1–A10 改了 §1/§4.1/§4.2/§5.6/§7.6/§11.1/§12/§13/§15/§17，确无 §0；QUESTIONS.md 检索「图形界面/范围外」零命中。选项：(a) §0:24 图形界面后加「（本地运维控制台除外，见 §12）」，一行、不牵动其他章节；(b) 维持原文，沿用 W1「spec 之外补充交付」先例（`IMPLEMENTATION_NOTES.md:175-176`），代价是 §12/§15 的「必须」永久悬空。

2. **【§7.1 抽象边界适用范围·决定 F1 形态】** spec:396 主语是**调度器**。控制台把 invoke 内部工具调用**只作人类展示**（不回流任何调度判定）是否越界？若判越界，F1 退到「派发→回复」粒度，A 的 ①②④ 须撤下。

3. **【logs/ 原文经 HTTP 暴露的安全边界】** `orch serve` 缺省绑 127.0.0.1（`server.py:707`）但无鉴权，真实 stdout 已实证含 sessionId（QUESTIONS.md Q9 档案）。三选一：(a) 原文直出；(b) 端点侧脱敏白名单（只出工具名与计数）；(c) 加开关，缺省关闭。

4. **【通讯录口径】** 当前线程内角色（零成本）vs workspace 级花名册（更贴对标，但每拍扫全部 `t-*` 开 sqlite，是最重轮询热点）。

5. **【checklist 由谁维护】** `set_task` 门槛是 `type ∈ {decision, acceptance, gate_decision} ∧ can_decide`（spec:119、`rules.py:64-70`），当前仅 `prompts/pm.md:22` 提到 set_task，而 `prompts/moderator.md:19,31` 明写「黑板不由你代劳」。若让 moderator 维护待办，会与 §11.2:553「**禁止**其产出任何实体工作」产生张力。建议维持 pm 维护。

6. **【单聊发送后是否自动 run】** 建议否——`POST /api/run` 是阻塞到终态的，自动触发会把「发一句话」变成「同步跑到线程终止」。

## 5. 建议施工切片（若立项）

**前置**：Q10（§0 矛盾）必须先裁；问题 2 决定 T4 是否派发。

**T1 · 复活 dispatching 数据源 + 通讯录单聊**（依赖：无）
- 目标：/status 投影全五态派发行与 deadline_ts，左栏出常驻成员名册，单聊过滤兼顾 sender 与 to。
- 可写路径：`src/orch/store/__init__.py`（**仅新增** `dispatches_snapshot()`，禁改 `pending_dispatches`）、`src/orch/web/server.py`、`src/orch/web/static/{app.js,index.html,styles.css}`、`tests/test_web.py`、`docs/m0-contract.md`（§2 追加只读原语一条，先例见 `docs/m5-contract.md:105`）。
- 完成标准：`pytest tests/test_web.py tests/test_m5_availability.py -q` 全绿；新增用例——`store.mark_dispatching` 后打 /status 断言返回该行含 deadline_ts。
- 卡内红线：禁改 `pending_dispatches` SQL；派发行键保持 `target` 而非 `role`（否则误触 `tests/test_m5_availability.py:1791-1802` 的 `_role_projection` 结构探测）；绿点判 deadline；@ 只许写 `#send-to.value`；带属性节点一律 `escapeHtmlAttr`（`app.js:632`）。

**T2 · 待办排序勾选 + 本轮统计**（依赖：T1 的通讯录布局稳定后再改右栏）
- 目标：任务节按首次声明事件号排序、出勾选字形与「第 N/M 步」；新增服务端 `_round_stats` 并在 /events 响应加同级键。
- 可写路径：`src/orch/web/server.py`、`src/orch/web/static/{app.js,styles.css}`、`tests/test_web.py`。
- 完成标准：新增 3–5 个 HTTP 用例覆盖零 human / 多条 human / 只有 system 事件三种锚点边界。

**T3 · config 解析异常缺陷卡**（独立，可并行）
- 目标：`cli/main.py:100` 的 `except (OSError, ValueError)` 无法捕获 `yaml.YAMLError`（核对员已实机验证 MRO 不含 ValueError），导致 config.yaml 语法错时 /status 被顶层吞成 500（`server.py:685-690`），整个状态面板变黑。
- 可写路径：`src/orch/cli/main.py`、`tests/`。完成标准：写一份语法错 config.yaml 后 /status 仍 200 且降级返回。

**T4 · logs/ 原文合规修复 + View Steps**（依赖：问题 2 裁决 + 问题 3 裁决）
- 目标：`write_invoke_log` 落真 stdout 原文，新增只读 `/steps` 端点与前端折叠组。
- 可写路径：`src/orch/adapters/__init__.py`、`src/orch/scheduler/{core,async_core}.py`（两处必须同改，孪生漂移教训见 `adapters/__init__.py:429-431`）、`src/orch/web/server.py`、`src/orch/web/static/*`、`tests/`。
- 卡内红线：steps 只喂 HTTP 只读端点与前端，**禁止**进入 `orch.render` 任何一层视图、**禁止**参与路由/重试/聚合/超时判定；**禁止**在 acceptance/decision 气泡上挂 steps（避免与 `app.js:741-749` 的「⚠ 无系统侧验证」红牌争夺证据地位）。

## 6. 附：核对修正记录

核对 30 条主张，25 条确认、5 条判有误。以下逐条说明吸收方式，正文已按修正后事实撰写：

1. **F3 称「工具调用痕迹在 logs/ 的 invoke 原文里(store:592-611)」——有误**。事实在 F1 一侧：`logs/` 今天落的是 `str(raw_env)`，盘上**任何位置**都无工具调用痕迹。正文 §3-F3 已改为「本系统盘上任何位置都无痕迹——`logs/` 今天也没有」，并据此保留「改口径为 invoke 次数」的结论。

2. **F3 施工指令「删 app.js:1039 的 taskKeys.sort()」——有误**，该行是空行，实际在 `app.js:1021`。正文与 T2 卡已改用 :1021。

3. **F4 称 `pending_dispatches()` 有「9 处调度侧调用点」——有误**，全仓 7 处调用点、调度层仅 4 处。但点名的 `core.py:866` / `core.py:1064` / `async_core.py:292` 逐字属实，故「禁改」结论保留，正文已改为「7 处调用点」。

4. **F2-B 声明 /status 投影「四个键一字不改」不带 deadline_ts——有误**（缺必要条件）。dispatching 行崩溃后会滞留（`watchdog.py:203-205` 注释 + :209/:212 代码实证），不投影 deadline_ts 则绿点长亮假绿。正文与 T1 卡已强制补入 deadline_ts，并列为三条硬约束之一。

5. **F1 称「工具调用素材就在被 continue 掉的行里」——有误**（结论过宽）。仅对 `wire_format ∈ {stream-json, opencode-stream}` 成立；`adapters/__init__.py:341-343` 的 `"json"` 分支整段 stdout 是单个 JSON 对象、`"text"` 分支 :359 直出，无逐行事件。按 MEMORY 记录本机可用后端 grok/claude 用 `"json"`，故 **View Steps 对 3 个后端里的 2 个会是空折叠组**——此限制已写进正文 F1 段与 T4 卡的风险面，原盘点未列。

**另吸收三条核对新增的施工机关（原四份设计均未完整覆盖）**：
- `tests/test_m5_availability.py:1791-1802` 的 `_role_projection` 按结构探测「首个每项含 role 键的顶层列表」，改 /status 时派发行键必须保持 `target`——已写入 T1 红线。
- 从 web 层导入 `orch.scheduler._dispatch`（下划线私有模块，`:32` 每次调用新开 sqlite 连接）会被 1.5s 轮询反复打——T1 改走 Store 新方法复用 `self._con`，比 F1-A③ 原方案更省。
- 新增 Store 公开方法须同步 `docs/m0-contract.md` §2 增补（该文件 :56 是冻结公开面）——已写入 T1 可写路径。
- `renderRoleBindings`（`app.js:295-302`）现在就在 `title="…"` 里用只转 `&<>` 的 `escapeHtml`（值来自 config.yaml，风险低），说明该红线仓内已有先例违反，评审时宜一并收口。
