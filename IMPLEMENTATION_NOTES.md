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
  - T2-T6 ✅ 实现（workflow 并行）；R1 修 §9.4 误读断言；R2 补 §3.2 降级（评审建议①）。
  - **M0 完成**：85 tests green，独立评审无阻塞，合入 main + tag m0-done，
    已推送 origin=bkdtjw/Hansard（commit 88ed2d7）。
- **M1：启动**（视图 §6 + 看门狗 §5.3 + 终止 §5.4 + moderator/角色提示词 §11.2/11.3）。分支 m1。
  - 明确不做（playbook §3.2）：真实后端、resume、聚合与并行。moderator 仍绑 mock。
  - 验收标准：视图渲染快照测试 + 环路/轮数上限触发路径测试。
  - T1 ✅ 测试先行 39 用例见红（test_render 21 + test_watchdog 10 + test_terminate_m1 8），M0 85 保持绿。
  - **审计警示**：M1-T1 期间 orchestrator-spec.md 被越权改动 1 空格（§5.3 表格），已 git checkout
    回滚归位（宪法只读红线）；tests/ 产出经亲验质量合格予以保留；后续 worker 卡须强化
    "绝对禁止 Edit/Write spec，只读=只 Read/Grep 查看"。
  - T1 提 3 歧义（均宽松、不阻塞）：终止清单用"分支/会话"关键词（§5.4 原词，T3 采纳）；
    check_watchdogs 的 suspended 落盘职责不绑内部分工；meta.dropped 元素须可区分层级+顺序，字段实现方自定。
  - T2-T4 ✅ 实现（render/scheduler/prompts，全量 124 绿）。
  - **M1 三维独立评审（wf_7c0182e8，A 反模式+忠实 ∥ B 测试+契约 ∥ C 完整性，均 opus 只读）**：
    无阻塞 spec 违反、无假绿、无 M0 回归、无越里程碑。三方共识 2 处需回环：
    · **§6.3 分层配比约束零实现**（常量仅回显 meta，_compress 只按总量压缩）——属 M1 必做半成品
      → 回环 T2/render 补强制+测试（R-a，wf_5ce5a1ea）。§17 裁决口径：焦点≥50%保底/黑板≤20%/背景≤20%，
      超配额层按 §6.3 压缩顺序裁剪。
    · **看门狗级别1 措辞"做一半称做完"**（只 bump_attempt，kill/重试属 M2）→ 回环 T3 改注释诚实化
      （R-b）+ Lead 改契约 §2 措辞（本次）。
  - 评审确认的其他 §17/退化（记录在案，非缺陷）：预算极端小窗"保首尾优先于硬达标"（符合 §6.3）；
    §16.7 落地为"第三人称标签统辖、正文保留全文"（符合 §6.2 字面）；箭头 →(spec散文)/->(实现+测试)
    属 §17 渲染格式开放决策，测试锚定 -> 避免脆性；§6.4 worktree 段对 mock 纯跳过（M2 真实 CLI 补）。

### M2 施工与评审记录
- M2 契约 docs/m2-contract.md（诚实边界：(a)-(f) 夜段自动化 / (g)-(i) 陪跑升级 QUESTIONS.md Q1/Q2）。
- T1-T5 workflow wf_d959416a：42 测试 → CliAdapter/ApiAdapter/FakeCli/FakeApi(§7.2/§7.3) → 权限三件套 permissions.py + core.py 接入(§8.1/§8.2/§4.5) → CLI §12 子集 typer 骨架 → E2E 装配 停机-重启-approve → 全量 169 绿。
- **M2 三维独立评审（wf_7dca491e，A/B/C 并行 opus 只读）** 结论：
  - A/B 判无阻塞可验收；C 找到 3 处夜段范围内真缺口需回环闭合：
    - **C-1**: CliAdapter 缺 --allowedTools 工具白名单参数注入（§8.1 三件套之二"骨架层空了一件"）→ R-a
    - **C-2**: orch run 命令缺失（stop 标志被写但无人读；stop 语义链条断）→ R-b
    - **C-4**: last_ok_commit 生产无更新回路，§8.2 审计生产恒 skip = fail open → R-b
  - B 建议 T2 补漏：FakeCli/FakeApi 缺 scripted_replies + inject_side_effect（T5 用 tests/adapters_helpers.py 包装补齐属透明权宜，但应回补 src 让 helpers 退役）→ R-c
- 回环 workflow wf_f3516501 并行 R-a ∥ R-b ∥ R-c 修上述 4 处。

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

### 2026-07-05 全面审核（用户指令：是否完全按 spec / 是否可正常运行）
- 方法：Lead 亲手验证（pytest 基线 + chaos-50 复跑 + spec git 历史零改动 + CLI 真实冒烟
  + import 白名单扫描 + 重点缺陷抽验）+ 8 维只读评审 workflow（wf_9f18b874）+ 逐条对抗核实。
- 结论：可运行；主体忠实；确认 9 簇真缺陷（2 blocking + 6 major + minor 若干），
  详见 docs/audit-20260705.md。M4 验收中"注入面覆盖 §4.4 全部间隙"与"§13 全表可复算"
  两条未真正达标，M4 完成宣告部分收回，待回环 R-A…R-J 修复后重新验收。
- 驳回 15 条误报/已豁免项（含 §5.5 凭据误读、render_delta 接线属 §17 已记录决策等），
  逐条裁决记录在审计报告 §三。

### M3/M4 施工台账补录（Lead 2026-07-05 补，原会话漏记，依 git log + 契约重建）
- M3：契约 5bfa4ff → T1 测试先行 17 用例（a40a384）→ T2 render_delta §6.5（484ec94）
  → T3 async 核心环+异步作业+多线程 §5.1/§5.2/§9.3（5628e94）→ T4 bench resume CLI（a968ec2）
  → 验收合入 8b26cfe，197 tests，tag m3-done。
- M4：契约 69a4a58 → T1+T5 测试先行+50轮门槛 opt-in（d1d11a1）→ T2 FaultInjector 3 site（730a53a）
  → T3 ChaosHarness（018618e）→ T4 metrics/replay CLI（c48a6a5）→ T5 R-a 恢复合并+门禁幂等（610b412）
  → 验收合入 3a6235e，tag m4-done。
- 教训：两个里程碑的台账未随卡落盘（违反完成定义第5项），2026-07-05 审计时发现并补录。

### 2026-07-05 审计回环 R-T1…R-T4 + R-J（用户指令"修复"）
串行派卡（写域在 core.py/chaos 交叠，依 CLAUDE.md 并行判据禁并行）；每卡 Lead 收尾三步齐备。
- **R-T1**（799eab0，闭 A1/A2/G）：invoke_post/autocommit_post 注入点补进两条核心环（store 暴露公共
  fault_check）；_resolve_site 删 None 降级、5 site 全真实注入；附录B 第四断言=mock ledger 字节
  + 黑板 state.json（sort_keys 确定化）与不中断基准逐字节比较；test_fault_inject 重写为生产路径
  真触发（5 site 各一）；EXPECTED_TYPE_SEQUENCE 移入 src/orch/chaos/expected.py，tests 反向导入。
  §17 裁决：①注入钩子按控制流位置触发（mock 无 worktree 时 autocommit 为 no-op 但位置仍在）；
  ②invoke↔reply 窗口天然至少一次，去重属 §9.2 层2/3 agent 幂等——harness 用 _IdempotentMockAdapter
  忠实模拟幂等 agent（查 ledger 已含 {role}:{event_id} 则补发信封不重做副作用），通用规则非特判；
  ③_handle_terminate 幂等可重入（总结事件带 re 血缘 + _finish_interrupted_terminate 恢复补完）。
- **R-T2**（7d182a9，闭 C/D/E/H）：①看门狗 level2/3 升级水位落 thread_meta
  （wd_l2:{s}:{t} / wd_l3_total），门限=水位+limit 窗口前移（§17 裁决），approve 后不复触发、
  新窗口仍升级；附带修 _raise_gate 把 corr 写入事件 corr 列（此前 meta-only，orch approve 够不到
  看门狗门禁）。②_view_with_retry_note 重调携带校验错误说明（§5.1 伪代码原文），同步/异步同源。
  ③_ensure_audit_baseline 首轮 invoke 前落 HEAD 为 last_ok_commit:{role}（§8.2 fail-open 闭合；
  permissions 增 head_sha）。④终止不再清扫既有 pending（§5.4 字面"拒绝新派发"）。
  worker 曾越权碰 systemexec.py，自行 git checkout 回滚改走 in-scope 方案（Lead 复核确认零改动）。
- **R-T3**（8855009，闭 F/I）：render_delta 按 m3-contract §2 接入两条核心环。三门控
  （sid 非空+supports_resume / gen 未变 / 黑板 version 未推进）→ render_delta → needs_cold_start
  （契约 version 变更）→ 作废 sid 回退冷启动。§17 归档：thread_meta 键 resume_last_evt/{bb_version}/
  {gen}:{role}；黑板 version 标量=Σ契约version+决策条数；决策顺序=门控(1)(2)→delta→规则2→门控(3)
  （契约大改必须走 sid 作废而非被门控3 拦成普通冷启动）；sid 作废用 system 事件承载 session upsert
  （DDL 冻结不加方法）；gen 取 max() 单调不减。人类显式作废 sid 无既有挂点，未新增命令（记此备查）。
- **R-T4**（b4a10e6，闭 B）：§13 采集点随代码交付——每次 invoke 落 tokens 行（in=视图 token_est，
  out=回复 estimate_tokens），cost 仅 adapter 暴露真实用量才落（Mock/Fake 无→N/A，不编造 0）；
  schema 校验失败每次落 schema_retry；render meta 出背景层压缩前后 token、调度层落盘；
  ChaosHarness.run(metrics_store=) 落 chaos_rounds/chaos_mock_pass_pct。metrics CLI 复算口径：
  首次合法率=1−retry行数÷tokens行数。test_metrics_cli 重写为"CLI 输出 vs 手工查表复算"对照断言。
- **R-J**（33ea349，Lead 胶水）：pyproject 增 [project.scripts] orch=orch.cli.main:main、
  typer 转核心依赖；pip install -e . 后 orch 命令全局可用（亲测项目外目录）。
- 每卡后 Lead 亲跑全量 pytest + --chaos-50：221→229→237→246 passed（+chaos50 各卡均复跑通过）。

### 终局独立评审与 R-T5(2026-07-05)
- 终局评审(3 维只读,closure/antipattern-regression/test-honesty):11 簇全部真闭合,
  幂等 mock(§9.2 层2/3 语义)与看门狗水位(§16.9 要求可从盘重建)均裁定合规非放水。
- R-T5 闭合评审新发现:①async_core.run_thread_async 入口同源调用
  _finish_interrupted_terminate(major,崩溃洞 sync/async 对等);②Store 新增公开
  upsert_session(role,sid,gen)单事务直写 sessions(冻结入接口面,DDL 未动),
  _invalidate_sid 弃用合成 system 事件——§17 裁决:会话簿记属工作状态,不经事件日志,
  维持 §3.2 type=system 枚举语义纯度。
- 评审 2 条 info 留档:A2 字节比较范围=ledger+state.json(事件因附录B 允许事件号偏移
  只能类型级比较,board.md 是 state.json 确定性投影);orch/chaos/expected.py 携带
  E9→tester(fixture 自决)与 E20 终止总结(§5.4 忠实产物)两处对附录B 字面的偏移,
  文件抬头已自陈。

### W1 玻璃感 Web 控制台（spec 之外补充交付，2026-07-05）
- 性质：spec 之外的补充工具，不改任何 spec 实现语义；HTTP↔现有 orch 函数适配层 + 原生玻璃感前端。
  零新增依赖（stdlib http.server + 已有 orch 包 + 白名单 pyyaml；前端纯原生无 CDN/npm，离线可用）。
- 落点：src/orch/web/{server.py,__init__.py,static/{index.html,app.js,styles.css}}；orch serve 命令
  （cli/main.py 薄入口 +33 行，逻辑在 web.make_server）；tests/test_web.py 26 用例。
- Lead 胶水：上一 commit 已加 orch 入口([project.scripts])；本次 .gitignore 加 .claude/（launch.json 含本机路径不入库）。
- 派工与纪律：workflow 单卡 W1（opus 全栈）。审计发现 worker 越权改本文件（W1 白名单未含 NOTES），
  已 git checkout 回滚，本段由 Lead 依"亲验结果"重写——worker 自述（含其替 Lead 声称的"亲验"）一律不采信。
- Lead 亲验（真起 make_server + urllib 打真实 HTTP + 查 sqlite，非采信 worker）：
  · 15 端点全部真副作用坐实：health / POST threads（磁盘真出 t-*/events.db + E1）/ run{once}→terminated /
    events（第三人称 4 条）/ status / send / gate approve→gate_decision(approve) 真落盘 / stop / reopen /
    attach（真实接入命令）/ replay（markdown）/ config（非法 yaml 拒写盘·合法写盘·读回一致）/
    metrics（§13 全 11 字段）/ bench（对比报告）/ 非法路径 404。
  · 玻璃感真实：styles.css 14 处玻璃属性 + backdrop-filter blur(14px) + 深色渐变，index.html 外链之。
  · 布局：桌面 1280 视口 layout/topbar 实测 1237px 撑满（两栏，≤900px 单列）。
  · tab 是真按钮：DOM .click() 令 view-config 显示 / view-threads 隐藏 / activeTab=config / textarea 存在。
  · 前端零 console 错误、后端零异常。Chrome 扩展未连，改用 Claude Preview 起服务截图（玻璃感主界面清晰）；
    配置/指标视图截图因 preview 渲染器对 backdrop-filter 重绘超时（工具兼容问题，非产品缺陷，已用 DOM/HTTP 证据替代）。
- 全量回归：pytest -q → 275 passed, 1 skipped（249 基线 + 26 web）；未改 chaos/scheduler，硬门槛不受影响。

### 真实 CLI 后端接入（Q1/Q2 陪跑，2026-07-05，Lead 亲做）
- 触发：用户在场，要求用真实 kimi/claude CLI 作 orchestrator 后端联跑。属诚实边界内"需人陪跑"，Lead 亲做（真实凭据/计费/输出即时把关）。
- 实测（Lead 亲跑，非采信）：
  · kimi 0.19.2：`kimi -p "<prompt>" --output-format stream-json` 非交互可用；回复在 `{"role":"assistant","content":...}`，session_id 在 `{"type":"session.resume_hint","session_id":...}` 行，resume `kimi -r <sid>`。Windows 可执行 `C:\Users\nirvana\.kimi-code\bin\kimi.exe`，subprocess 需完整路径 + UTF-8 解码（默认 gbk 乱码）。
  · claude 2.1.201：`claude -p --output-format json`（result=回复文本、session_id 顶层字段）、`--session-id <uuid>` 可编排器自控、`-r/--resume`。但工具子进程 401（环境注入 ANTHROPIC_AUTH_TOKEN/BASE_URL 是本运行时的智谱代理凭据，unset 后回退 OAuth 亦过期）——claude TUI（OAuth 活在交互终端）能用、子进程读不到。凭据隔离，需用户终端修通，登记 QUESTIONS Q1。
- 接入改动（均向后兼容，278 全绿）：
  · adapters：新增 `_unwrap_agent_output(stdout, config)` 按 config.wire_format 解包（"text" 默认=M2 既有；"stream-json"=kimi 逐行 JSON 拼 assistant content + resume_hint sid；"json"=claude result/session_id）；CliAdapter.invoke 用它 + sid_hint 优先兜底 `_extract_sid`；Popen 加 encoding=utf-8/errors=replace；caps.supports_resume 读 config（kimi 设 false 走全冷启动，避免增量视图喂无记忆新 session）。
  · cli/main.py：新增 `_build_adapters_from_config`（据 §11.1 装真实 CliAdapter，暂只 kind=cli，未知 kind 显式报错不臆造）；orch run 在 config 有 adapters+roles 时用真实装配，否则 Fake。
  · tests/test_cli_adapter.py：+3 解包单测（kimi/claude/text），样例取自实测、json.dumps 构造，零测试计费。
- 真实联跑（Lead 亲跑，线程 t-91bb9e71）：config 三角色全 kimi_cli → orch new 小任务 → 真实三方协作 E1..E7 全真实 kimi（backend 给方案→moderator 派 tester→tester 确认→moderator 汇报 human→human 指令→moderator terminate）→ terminated；E8 终止台账记录三角色真实 session_id。全链路：render 视图→kimi.exe subprocess→stream-json 解包→信封入队→调度→session 提取→终止清单。
- 真实发现（记录，未即时修）：真实 agent 会自主 handoff 给未配置角色（moderator→tester，初次 roles 无 tester 时 run 抛 `KeyError('tester')`）。即时以"加 tester 角色"绕过；健壮性缺口=装配/调度对未知 target 应优雅拒收+审计而非崩环，属 spec 边界外增强，留后续（真实场景配全角色规避）。

### web 控制台接真实后端（2026-07-05 续，Lead 亲做）
- src/orch/web/server.py `_ep_thread_run`：与 orch run 同一判断——config 有 adapters+roles 时用
  `_build_adapters_from_config`（真实 CLI），否则 Fake。玻璃控制台由此可真驱动 kimi。
- Lead 亲验（make_server 指向 orch-real-ws，经 HTTP run 端点，线程 t-3b262e97）：E2 backend(kimi)
  真实输出 `return s[::-1]`（非 Fake ack）；E5 展示真实 LLM 输出非法 type=ack 时 §5.1 schema 校验
  + 携带错误说明重调（R-T2 的 D）+ 降级 system 审计正确生效。web 端点真驱动真实 kimi 坐实。
- 边界：真实后端下 run 端点同步跑真实 CLI，HTTP 请求耗时较长（分钟级）；异步化（后台跑 + 轮询）
  属后续优化，未做。test_cli_adapter 补 2 装配单测（cli 型/非 cli 报错，纯逻辑不调 CLI）；280 全绿。

### 真实 CLI 落代码联跑（Q1/Q2 深水区达成，2026-07-05，Lead 亲做）
- 用户要"用系统完成一个会落代码的项目"。先最小验证、再做完整项目。
- 关键实测（Lead 亲跑）：kimi `-p` 非交互默认自动执行工具（`-y`/`--auto` 与 `-p` 冲突，无需它们），
  一次调用既 Write 文件又输出信封（stream-json：assistant tool_calls 行 + tool 结果行 + assistant content 含信封）；
  CliAdapter._unwrap_agent_output 天然兼容（tool_calls 行无 content 字符串被跳过，只拼信封 content）。
- 装配接线（cli/main.py _build_adapters_from_config）：config 有 target_repo 时，为 write_scope 非空角色
  调 ensure_worktrees 建 git worktree，路径写回 config['worktrees'][role] 供 core.py §8.2 审计；
  CliAdapter cwd=worktree（kimi 在隔离沙箱写代码）。无 target_repo 回退现状（280 绿，既有测试不破坏）。
- 最小验证（Lead 亲跑）：真实 kimi 在隔离 worktree 写 src/add.py → autocommit wip:backend@E1 → §8.2 审计合规；
  手动越权 tests/hack.py（超 src/）→ 审计正确判违规。
- 完整项目（线程 t-b3e3336a，TODO CLI，pm/backend/moderator 真实 kimi）：pm 写规格 blackboard/todo-spec.md →
  backend 写 src/todo.py(129行)+tests/test_todo.py(15测试)真跑 pytest → moderator 审查找 3 处不符 →
  §8.2 审计拦截 backend 越权写根 tasks.json（git reset）→ 多角色协商 pm 改 spec 路径 → backend 返工修正。
  成品合并 target_repo main：15 测试通过、CLI 实跑可用（add/list）。真实"权限冲突→审计拦截→返工"场景自然涌现。
- 边界：隔离 worktree 下跨角色依赖（tester 看不到 backend 代码）未解，本次 backend 主力落代码
  （write_scope=[src/,tests/]），pm/moderator 协调层分工；多角色各写+顺序合并 pipeline 留后续。

### §10 corr 缺省生成条款补实现（2026-07-06，真实联跑发现，Lead 亲做）
- 缺口：apply_gate_decision 只认 gate_request 事件的 corr；任意非 gate_request 信封
  发往 human（如 moderator handoff→human 收尾）同样按 §5.1 挂起，却无 corr 可批 →
  线程永久卡死。spec §10 明文 corr 缺省时编排器生成 `gate-{事件号}`，此条款未实现。
- 修复：systemexec 增 `_find_informal_gate`（生成形 corr 只查表反解：事件存在且 to 含
  human），apply_gate_decision 找不到 gate_request 时回退反解；后续 gate_decision 回填/
  mark_done/resume/幂等全部复用原路径（零 DDL/协议改动）。app.js 门禁 banner 对无
  gate_request 的挂起派生同形 corr，批准/拒绝按钮直接可用。
- 证据：测试先行 3 连（test_e2e informal gate）红→绿；283 passed 全量零回归；
  真实卡死线程 t-934119b0 经 `orch approve gate-4` 恢复（suspended→running，
  E5 gate_decision corr=gate-4 → moderator）。记 QUESTIONS.md Q6（判定=实现缺口非开放决策）。

### 可用性审视快赢五连（2026-07-06，docs/usability-review-20260706.md §五，Lead 亲做）
- ① run 过程日志：core 经 logging("orch.run") 发派发/回复落盘/挂起事件（零 print 零签名改动），
  cmd_run 挂 stderr handler（幂等重绑）+ 长驻一次性横幅；② 入口 _force_utf8_stdio 根治 GBK
  乱码（无 reconfigure 流静默容忍）；③ 无 adapters/roles 的 run 显式警告 Fake 演示适配器；
  ④ approve/reject 的 KeyError → 一行人话 + exit 1（不喷 Traceback）；⑤ docs/USAGE.md
  五分钟上手（操作者向）。测试先行 5 连（test_cli_ux_quickwins）红→绿，291 passed 零回归。

### 审视遗留 P2×2 + P3 三连（2026-07-06，用户点名"全部"，Lead 亲做，测试先行）
- ① CLI 语法统一（实为 spec §12 回归）：replay/metrics 的 thread 改位置参数（spec 原文
  `orch replay t-001` / `orch metrics [t-001]`），--thread 保留兼容别名；approve/reject
  按 spec `<corr>` 单参——缺省 --thread 时 _resolve_gate_thread 按 corr 扫描唯一定位
  （0/多命中一行人话拒绝）。统一语法规则：必需目标=位置参数，可选过滤/消歧=选项。
- ③ 迟到回复标记（展示层，零协议/DDL 改动）：_late_after_id + _render_replay_lines
  （CLI 与 web replay 共用），终止号后非 system 事件行加 ⏱ 标记；控制台卡片头同款
  late-pin 徽章（fluency 线程真实 E10 preview 亲验恰一枚，终止前零误标）。
- ② 多工作区单控制台：make_server 接受 list（签名向后兼容），每请求 ?ws= 选择
  （缺省第一个），/api/workspaces 新端点，同名目录去重 -2/-3；serve -w 可重复；
  前端 api() 自动携带 currentWs + 顶栏下拉（>1 才显示，localStorage 记忆），
  切换时清线程态/停轮询。
- 证据：test_cli_grammar 6 连 + test_late_reply_marker 2 连 + test_web_multiws 3 连
  红→绿；全量 302 passed 零回归；preview 8796 三工作区实切 + 真实 E10 徽章亲验。

### M5 立项：适配器可用性与降级路由（2026-07-25，用户批准的 spec 修订）
- 动机：kimi 额度耗尽暴露"系统无可用性概念"缺口（§5.1 对断粮 adapter 反复撞墙）；
  中转站额度不可见 → 手动标记为第一公民，自动跳闸为兜底。
- 用户裁决：增补稿过目后批准；跳闸恢复=一律手动；追加"控制台必须有 enable/disable
  开关按钮"。环境事实（用户口述）：grok/opencode/claude(中转站) 可用，kimi 断粮。
- Lead 胶水：SPEC-AMENDMENT-M5-draft.md（设计记录，已标注合入状态）；spec 合入
  A1–A10（新 §5.6 + §1/§4.1/§4.2/§5.1 伪代码/§7.6/§11.1/§12/§13/§15/§17 增补，
  用户授权的宪法修订，非擅改）；MILESTONE→M5；docs/m5-contract.md 冻结跨卡接口
  （AdapterAvailability/resolve_effective_adapter/AdapterUnavailableError/
  Store.reset_attempts/CLI 三命令/web 端点/metrics 键/事件 meta.kind）。
- 接手基线：302 passed, 1 skipped（63s，动工前亲跑）。

### M5 施工台账
- T1 ✅ 验收测试先行 46 用例见红（43 availability + 3 chaos opt-in `--chaos-m5`），
  302 基线零回归（Lead 亲跑三条完成标准）。conftest 仅追加 chaos_m5 门控块，
  chaos_50 逻辑逐字未动（Lead diff 亲核）。
- T1 九条契约歧义 Lead 裁决（均为 spec 之下契约粒度，无 spec 冲突，不入 QUESTIONS）：
  ① 状态文件路径经 `config['adapter_state_path']`（装配层回写绝对路径，缺失 →
     可用性逻辑整体退化为全 enabled，既有 302 测试零改动的兼容前提）；
  ② adapters 映射按 adapter 名取实例，保留"role 无 adapter 声明回落角色名"分支；
  ③ 附录B 事件号偏移 → MockAdapter 增 `key_by="call"`（契约 §2 扩展，T4 落实；
     fixture 文件不动）；
  ④ "终态逐字节一致"的 M5 口径 = 剔除 M5 审计事件后把事件号映射为名次再逐字节
     比较 + 过滤后类型序列 == 附录B（R-T1 口径的延伸，其余维度不放松）；
  ⑤ chaos 符号冻结采 T1 自决：`AdapterChaosHarness(workspace, script, seed,
     unavailable_after).run(rounds) -> ChaosReport`（复用 M4 五字段）+
     `ADAPTER_INJECTION_SITES = {adapter_trip_post, fallback_switch_post,
     rebind_dispatch_post, random_mix}`；
  ⑥ 同步环全阻塞语义 = 无可调度组立即返回（M0"无待办即返回"同款退化），轮询节奏
     归 orch run 外层；async 环沿既有等待机制 + 轮询（缺省 ≤2s，属 §17）；
  ⑦ 审计事件免派发行 = `Store.append_event` 增缺省关键字 `make_dispatches=True`
     （缺省行为逐字不变）。**禁止** to=[] 特判（那是兜底路由语义）、**禁止**
     追加后删行（不忠实）。T3 落实；
  ⑧ `orch status` 的 --config 与 --workspace 并存，互为补充；
  ⑨ metrics extra 具体格式 T3 自定，键名 `fallback_switch` / `adapter_trip` 锁死。
- T2 ✅ adapters/state.py（22 绿）：原子替换/缺失全 enabled/损坏拒猜/RLock；
  record_success 不改 status（恢复只认人工 enable）。追认：validate 不要求主绑定
  已声明（角色名兜底分支的前提，加此规则会炸既有配置）。
- T4 ✅ 适配层（卡内先红后绿 30 测试）：AdapterUnavailableError(adapter_name,detail)；
  Cli 超时/无块两分支送 pattern 分类（复用 state.DEFAULT 常量），未命中原路径逐字
  不变；Api 包 message_fn；Mock 增 unavailable_after/key_by="call"。追认三条：
  JSON 解码失败不做特征分类但**计入 streak**（§5.6.3"无法解析出信封"字面）；
  显式 `unavailable_patterns: []` = 关闭特征分类；超时无文本不分类（不注入合成串）。
  移交提醒：跳闸落盘名用调度器解析的生效绑定名（exc.adapter_name 仅审计线索）；
  chaos 的 _IdempotentMockAdapter 重发分支不走父类，T6 需同步适配。
- T3 ✅ 调度双环接线（D/G 组 11 绿，含 R1 回环）：新增 scheduler/availability.py
  共用模块，同步/异步同源（make_availability/resolve_binding/note_* /rebind/on_*）；
  store 仅两处（append_event 增 make_dispatches=True 缺省关键字、新增 reset_attempts）。
  **R1（spec 冲突裁决）**：§13 字面 = fallback_switch 指标**逐次降级派发**各记一条
  （附录B 实测 16 条），审计事件保持 §5.6.2 首次一条（5 条）；T1 侧 G 断言改三层
  双向对账（备胎实例调用数推导+硬构成校验+extra 分组）。chaos-50 硬门槛复跑保持。
  记录在案：①同步环原本无传输级失败处理（异常穿出）、异步环无 attempts 重试——
  §5.1 与实现的**既有偏差**（M0 起），本卡仅在 availability 启用时补齐 attempts
  语义以保 332 基线，无条件启用属后续裁决；②"传输级"型别判据（TimeoutError/
  ValueError/OSError）在调度层，pattern 分类在适配层（§7.6 分工不破）；③无 sessions
  行/backend 空 → 不换绑不 reset（无活会话可作废）；④审计事件 to=[] 渲染显示
  @moderator 与 terminate 同口径（无派发行，仅显示层）。
- T5 ✅ CLI+web+控制台按钮（E/F 组 10 绿 → M5 availability 43 全绿；全量 375）：
  三命令 orch adapters / adapter disable / adapter enable；status --config 与
  --workspace 并存；web 三端点 + 前端适配器页签（徽章+开关按钮+reason 输入+警示条
  +页签红点，D6 轮询捎带，重渲染保输入焦点）；生产接线补 T3 缺口：装配建
  "adapter 名→实例"映射、写回 config['adapter_state_path']、装载校验一行人话报错。
  §17 决策（T5）：run 轮询间隔沿 --interval 缺省 1s；三命令 --config 缺省=./config.yaml；
  接线仅在 config 有 adapters+roles 的真实装配分支启用（Fake 演示路径逐字不变）；
  ts 显示本地时间 ts=0 显示 "-"；enable/disable 名字校验集=config∪状态文件,
  双空不校验；面板轮询沿 D6 1.5s。
  记录在案（留后续,不阻 M5）：①同一 adapter 名被多角色引用且 invoke 工况不同时
  不建共享实例——降级触发显式 KeyError,宁可响亮失败不静默串 worktree（与 M2 期
  "未知 target"健壮性缺口同族）；②api/mock 型 fallback 在真实装配无实例（既有
  "真实装配仅 cli"边界的延伸）；③装载校验只在两处 run 装配触发,只读命令不阻断。
- T5 控制台按钮 Lead 亲验（8795/todo-ws 真实配置,DOM .click() + HTTP + 磁盘三方
  对账,浏览器窗格无法合成画面故沿 W1 先例用 DOM/HTTP 证据）：点「⛔ 停用」（填
  reason）→ /api/adapters 变 disabled/by=human/ts 打点 → adapter_state.json 磁盘
  同一事实 → `orch adapters` CLI 读到同一行 ⛔；面板警示条/页签红点同步出现；
  点「✅ 恢复」→ enabled + streak 清零；未知 name POST → 400；console 零报错。
- T6 ✅ 适配器切换混沌（chaos/__init__.py 单文件 +641 行）：_AdapterProbeStore 在
  store 公开边界外挂 fault_check（不动调度层）,六个 M5 真实注入点（跳闸/切换审计/
  换绑重派 各前后）+ random_mix（池并入 M4 §4.4 五间隙,§9.4"任意时刻"字面）;
  断粮计数=以 ledger 已落行数推导（盘上事实,跨 kill 成立,主备接力）;幂等重发按
  ledger 标记位置取脚本项;轮内保留状态文件（真相层,跳闸延续正是被验语义）、
  轮间全重置防串轮;20 轮 100%、seed 入 ChaosReport（加第六字段,M4 五字段未动）。
  追认四条：六 site 超集兼容 T1 的 in 断言；random_mix 并 M4 池合法；ChaosReport
  加 seed 字段合法；mock 返回 {sid,gen} 使换绑分支在 mock 语境可达（副作用不在
  比较产物内）。负对照证实假绿测不出:去幂等→ledger-duplicate,内存计数→
  primary-not-auto-tripped。
- **R2（spec 冲突裁决,T6 跨 seed 取证发现）**：契约 §3 初稿"跳闸后本轮 continue
  下轮接手"与 spec §5.6.3"**立即**按 §5.6.2 重解析"冲突——kill 把双角色跳闸错开
  时,同批下游组先产出回复,交错偏离基准（≈1.5% 轮次,types-mismatch: report/defect
  相邻互换）。裁决:spec 字面胜,契约文本已订正（R1 频次口径同步补记）。落地:
  _dispatch_group(_async) 改薄外层重试循环+_TRIP_RETRY 哨兵,同轮就地重解析→
  当场换绑重跑本组（组间最小事件号先后不变;异步组内串行重试,不加跨组屏障;
  终止性=跳闸单向+链长有限,零计数器）。因果取证:monkeypatch 关掉修复,三锚点
  seed(7/107/20260704) 精确复现同一失败签名;打开后全 20/20。
- **M5 独立评审(只读 opus,probe 级取证)**:13 条发现(6 major/4 minor/3 info),
  总裁决"需回环"。Lead 逐条裁决:
  · major-2/评审挑战⑤(§11.1 范例配置降级即 KeyError):**收回 T5 记录在案①,
    升级 blocking**——评审论证成立:装配已为每 owner 算出 (sig,role,merged,wt),
    按 (role,name) 各建实例既保隔离又不丢功能;KeyError 实为静默停滞(穿出
    run_thread 断全轮+cmd_run 吞成刷屏)非"响亮失败"。冻结修复契约:装配注册
    复合键 f"{role}::{name}"(每角色×主绑定+各 fallback 一实例,绑该角色
    worktree/tools);调度 adapter_instance 先查 f"{target}::{effective}" 再查
    effective 再查 target(兜底链保既有绿)。→R3(调度侧)+R4(装配侧)
  · major-1(§13 两项指标无汇总出口):orch metrics 与 /api/metrics 补两行,
    标签用 §13 行名。→R4
  · major-3(20 轮门槛对 R2 盲):chaos 硬门槛测试参数化 seed [20260725, 7]
    (7 对 R2 缺陷敏感,负对照已证)。→R5(测试)
  · major-4/5(切换与阻塞通告跨"恢复→再进入"漏记):spec"连续"字面=中断后
    重新首次。冻结:新增第四种通告 meta.kind="adapter_recovered"(role 回归
    主绑定时,免派发行,现查去重同款)作为盘上断链锚;switch/blocked 去重以
    "最近一条同类事件之后无 recovered/成功回复"为连续判据(全盘上现查,
    零内存态)。→R3
  · major-6(attempts 归零被 sessions 行有无 gate):spec 原文无限定。冻结:
    prev-binding 从审计链盘上推导(无活跃 switch 记录=primary),effective≠prev
    即归零,与去重共用推导。→R3
  · minor-1(裸 status 无呈现):--workspace 模式自动从 ws/config.yaml 派生。→R4
  · minor-2(控制台角色行无生效绑定/警示不点名):status 端点补角色投影
    (primary/effective/blocked,只读投影,Q3-A 同例),前端角色行显示绑定,
    警示条点名阻塞角色;"有 disabled 但备胎兜住"降为提示。→R4
  · minor-3(状态文件损坏非启动报错):装配启动时探载,损坏一行人话 exit 1。→R4
  · minor-4(看门狗超时不计 streak):活循环看门狗 kill 路径接 record_failure;
    recover 路径 b) 若 availability 不可达则报告说明。→R3
  · info-1(异步环测试覆盖薄):补 async 跳闸/阻塞取证。→R5
  · info-2(可用性开关双失败语义):维持记录,M5 不动(无条件启用属后续裁决)。
  · info-3(宪法修订授权凭据):用户 2026-07-25 会话原话批准增补稿("你的方案
    我同意,但是注意:前端要有按钮开启关闭哈"),批准即含按钮修订;终审时
    用户可复核 git show 3831ef8。
  回环序:R5(T1,测试先红)→R3(T3)→R4(T5)→Lead 全套亲验→评审 closure。
- **closure 复核(同一评审,probe 级亲跑)**:13 条中 11 条实证 closed,info-2(有意
  保留)/info-3(须人类亲核 git show 3831ef8)合理挂起;R4b(心跳解耦)后 Lead 真机
  复验 chip/警示条两拍内出现。**但整改引入 1 blocking + 4 minor + 3 info 新问题**:
  · **N1 blocking(minor-4 整改的回归,Lead 裁决失误自记)**:watchdog level-1 自
    M1 起只 bump 不改 status(半成品,模块自陈),滞留 dispatching 行每轮重扫;
    R3 按我的裁决把 record_failure 挂上后,probe7 实测 6 轮内主+备胎全链
    auto-disable(全局状态文件,跨线程,仅人工可恢复)。评审批评成立:我把
    "实际影响小"的限定当耳旁风,且 test_r5_minor4 的取证面(单轮+trip_after=5)
    恰好挡住放大。**修正裁决:watchdog 计 streak 仅在该行 attempts 0→1 的首次
    观察记一次**(盘上判据/零新状态/杀死放大;invoke 路径计数不变)。→R7a
  · N2 minor(非 cli 备胎运行期 KeyError 崩环):真实装配对"无 tools 角色配 api
    型 fallback"(§11.1 允许,本装配做不出)启动即一行人话报错,不放行到运行期。
    →R7c
  · N3 minor(prev-binding 崩溃窗口丢归零):次序改为 reset_attempts 先于切换
    审计落盘(reset 幂等,窗口自愈)。→R7a
  · N4 minor(/api/adapters 双线重复拉取):摘掉 pollLoop 旧捎带,心跳单源。→R7c
  · N5 minor(终态线程心跳每拍全量 applyStatusPayload+两次盘读永不停止):
    终态只喂 roles 渲染,thread-status 降频,盘读有界。→R7c
  · info 三条记录不动:阻塞断链"失败无回复"窄漏(契约已备案);每派发 2-3 次全量
    events 扫描 O(N²)(max_rounds=100 缺省有界,留性能项);裸名键指向首个主绑定
    者实例的隐雷(被复合键优先屏蔽,留档警示后人)。
  · M5 之外既有缺口(评审顺带确认):cmd_run 从不调 §9.1 recover、level-1 完整
    动作序列未实现——已挂独立后台任务卡(task_548ffdae),不混入 M5。
  回环序2:R7b(T1 红)→R7a(T3)→R7c(T5)→Lead 亲验→评审终局 closure。
- R7b ✅ 3 红(滞留行 6 轮放大/写序倒置/api 备胎运行期才炸退出码 0——三条全红在
  缺口本身)。裁决:N3 次序断言写法唯一,历史盘面不下自愈单;N1 保留计数仅首观察;
  N2 落装配层不动 §11.1 校验(收紧宪法允许的配置=违禁改进)。
- R7a ✅ 调度侧两条:watchdog 计 streak 仅 attempts 0→1 首观察(非首次连状态文件
  都不读;bump/不改 status 既有语义一字不动);换绑写序对调=reset_attempts 先于
  fallback_switch 审计(读序不变,reset 幂等窗口自愈)。备案:attempts 列双用语义
  恰好合理(invoke 已计过的行不再重计);chaos fallback_switch_pre 注释与新写序
  轻微漂移(外观,不影响锚点与 4 用例)。
- R7c ✅ 装配/前端三条:_assembly_feasibility_errors 单点(§11.1 校验→装配可行性
  →状态文件探载),api 型备胎启动一行人话 exit 1/web 转 400 JSON;pollLoop 摘掉
  adapters 捎带(实测 9.2s 窗口 13 次→6 次单线;loadAdapters 保留为显式动作即时
  刷新,非轮询线);终态线程心跳降频 ROLE_STATUS_EVERY=3 且只喂 roles 渲染(实测
  status 2 次/9.2s、events 0 次,D6 终态语义保持;blocked 点名时延≤4.5s 明示取舍)。
- R7 后 Lead 终验:60+30+392 全绿、chaos-m5 双 seed 4 passed、chaos-50 保持;
  真机(仅桩 document.hidden):adapters 恰单线 1.5s 节奏,disable→chip×3+红条点名
  →enable 消隐全周期正常。
- **终局 closure(第三轮,同一评审亲跑 probe7/10/11/12 复验)**:N1-N5 全 closed
  (probe7 滞留行 7 轮 streak 恒 1 零跳闸零传染;probe12 写序不变式"通告存在⇒reset
  已提交"自愈成立且 fallback_switch_pre 锚点覆盖不降反升;probe11 装配期 exit 1/
  web 400 不退进程;N4/N5 读码+Lead 真机计数自洽)。**总裁决:可验收**。
  三轮共 18 条发现,blocking/major 全数实证 closed;残留逐条裁决:
  · minor(缺 kind 键的 fallback 仍漏到运行期 KeyError,`if kind and`→`if kind`
    一行可闭)+ info(KeyError 抛点爆炸半径未收敛)→ 已挂独立后台任务卡,
    列交付后清单第一项(评审原话),不触及 §15 M5 任一验收标准;
  · info(attempts 列双用致真实超时落在 attempts>0 行时漏计 streak——方向安全,
    少跳不误跳)、info(record_metric 与审计间崩溃窗口致指标多计一条——§13 逐次
    口径下可辩)、chaos 注释漂移(外观)→ 记录挂账;
  · 宪法修订授权凭据(info-3):评审性质上无法采信工具内引文,已明示"请人类
    git show 3831ef8 亲核"——转达用户终审。
  评审终版 §16 十三条逐条表(文件:行号)在其 closure 报告内,完成定义第 3 项直引。

### 真实后端第 3/4 家接入:grok + opencode(2026-07-25 陪跑,Lead 亲做)
- 用户指令"都测试一下"。实测(Lead 亲跑,全部免费档零成本;拓片留 scratchpad):
  · grok 0.2.112(C:\Users\nirvana\.grok\bin\grok.exe):`-p <prompt> --output-format
    json` 单 JSON,回复在 text、会话在 sessionId(与 claude result/session_id 同构
    异名);`--resume <sid>` 热续实测通过(记得上轮内容);`--allow` 官方兼容别名
    --allowedTools,权限注入 claude 同形;`--cwd/--max-turns/--no-subagents` 齐备。
  · opencode 1.18.4(npm .cmd 垫片不可直启,真身
    AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe):`run <msg>
    --format json` JSON 行事件流,type=="text" 的 part.text 拼接为回复,sessionID
    行顶层;`-s <sid>` 热续实测通过。
- 接入胶水(测试先行 3 红→绿,样本取自实测拓片零测试计费):_unwrap_agent_output
  "json" 分支双键名兼容(result|text / session_id|sessionId,claude 向后兼容),
  新增 "opencode-stream" 分支;test_cli_adapter +3;全量 395 passed。
- E2E 冒烟(orch-demos 三工作区,config 均含 unavailable_patterns/trip_after):
  · grok-ws 一把过:pm(grok)提"众智成事"→moderator(grok)terminate,两会话独立,
    全部首次合法。
  · oc-ws 第一轮 pm 两次 JSON decode 失败(模型轮盘)→ failed→moderator(opencode)
    诚实汇报并终止——重试/升级链路按 spec;第二轮全流程过("众智成城")。
    顺带暴露 §14 缺口:失败 invoke 不落审计日志(挂 task_ef1a8021)。
    streak 2 后被同 adapter 成功调用清零,§5.6.3 语义正确。
  · **hetero-ws 降级实战(M5 首次真实后端全链路)**:pm/moderator 主 grok、备
    opencode;grok 完成第一轮(三候选+选定「智联协同」)→ 人工 disable grok →
    E4/E6 降级审计事件入群聊 → opencode 冷启动读全量历史接棒,论证精准衔接
    grok 的选择 → handoff@human 挂起(Q6 非正式门禁)→ approve gate-7 复活。
    指标:[8] 降级切换 3(pm2+mod1)、[9] 自动跳闸 0(手动禁用不混计,诚实)。
- 意义:①第 3/4 家供应商接入成本实测=配置一段+解包分支几十行(§13"从第 3 家
  起算"口径的实证);②Q2 遗留"≥2 家异构厂商"达成(grok+opencode 同线程混编);
  ③M5 降级路由首次在真实后端验证,跨厂商半途接手上下文零丢失。

### 落代码演示(code-ws)联跑:两个真实缺口确诊(2026-07-26,Lead 亲做)
- 目标:grok 当 coder 在隔离 worktree 写罗马数字转换 + tester 系统侧验收。四轮尝试
  全部未产出可验收代码,但确诊两处真缺口(均已挂后台任务卡,不混入 M5):
- **⚠ 更正(2026-07-26 稍后,用户告知 grok 走第三方中转站后复查)**:缺口A 的
  "argv vs prompt-file"归因**证据不足,降级为待验**。查 ~/.grok/config.toml:
  default 模型 grok-4.5 指向中转站(base_url=new-sub2api.…,api_backend=responses,
  context_window 500000);而实测发现 —— `-m grok-4.5`(走中转站)当场
  `rate_limit_error: Upstream rate limit exceeded` exit 1;不带 -m 时用量字段是
  **grok-4.5-build-free**(内置免费档)且可用。即本轮全部"成功"探针走的都是免费档,
  失败样本可能是中转站限流/免费档退避所致,与 argv 传参未做到单变量隔离。
  白名单参数被取值型 flag(-p)顶掉那条**仍然成立**(纯参数拼接,与后端无关)。
  待中转站额度恢复后按单变量重测(同一 prompt × argv/prompt-file × 同一模型档)
  再定论;task_bbff7655 卡内已有的两个修复方向(prompt_via=file / 参数位置)
  在任一归因下都是有益的健壮性改进。
- **缺口A(task_bbff7655,归因待验,见上):长 CJK prompt 走 argv 喂 grok 失败**。同一份编排器
  渲染视图(3103 字符),经 `--prompt-file` 直喂 grok → 4 轮写出 src/roman.py +
  tests/test_roman.py 并吐合法信封;经编排器 argv(`-p <TEXT>`)→ 恒"no json
  block"、两次耗尽 attempts、触发 M5 streak 跳闸。另发现同族问题:白名单参数
  被追加在 start_cmd **之后**,若 start_cmd 以取值型 flag(-p)结尾,实际变成
  `-p --allow Edit …`,提示词被顶掉。修复方向:adapter config 增 prompt_via=file
  或 tools_args_position/prompt_flag。
- **缺口B(task_ef1a8021 的实战证据 + 新卡):opencode 不认进程 cwd**。CliAdapter
  已设 cwd=worktree(实测 worktree 内容正确),opencode 却把 src/roman.py 写到
  工作区根目录(线程目录上两级)。后果:§8.1 隔离失效、§8.2 审计审的是 worktree
  故看不到改动=fail-open、agent 汇报"8 个测试全通过"且 tester 附和"pytest 9.0.2
  全通过"——**全链路幻觉未被拆穿**。opencode 有 `--dir`,需适配器支持注入。
- 连带印证:失败 invoke 不落日志(task_ef1a8021)在本次排障中直接致盲——只能靠
  手工复现定位,这条从"卫生项"升级为"排障必需"。
- 演示配置暂以"grok_coder 不声明 tools、靠 --permission-mode acceptEdits"绕过
  缺口A的白名单部分;缺口A的 argv 部分与缺口B 未绕过,故 code-ws **尚不可用于
  对外演示**,已如实告知用户。

### 意外问题修复:{cwd} 占位 + verify 钩子 cwd 渲染(2026-07-26,workflow wf_e274b8b8,opus 工人×7)

用户指令"派 opus 解决意外问题"。两个 commit:2cbffad(start_cmd {cwd} 占位+verify
cwd 渲染)、6a10825(评审回环:verify 走 to_thread+降级追加 system 提示)。全量
417 passed, 5 skipped(Lead 亲跑,含混沌门 --chaos-m5 14 passed)。

**比预想深一层的雷(裁决agent只读体检挖出)**:§8.3 verify 钩子的 cwd 占位渲染
**从未实现过**——core.py 原 `cwd = verify.get("cwd") or "."`,docstring 却自称
"M2 落地"。后果两态:按 §11.1 示例配 `{worktree:backend}` → NotADirectoryError
→ exit_code=1 → acceptance 永降级;按 §8.3 拼写 cwd_template → 键不被读 → 静默
在编排器自身目录跑出 exit_code=0 = **假绿**。即上一场演示就算 tester 发了
acceptance 也过不了钩子——两层故障叠着。

修复要点(工人实现,Lead 复核):
- `_start_cmd_argv`:split 后逐 token replace('{cwd}', worktree)。先分词后替换
  → 含空格路径恒单 argv 元素;无占位逐字节回归;不作用于 tools_args 与视图正文
  (正文参与替换=给 agent 开模板注入面)。真身与 Fake 孪生共用同一 helper。
- verify cwd 渲染:{worktree:role}/{target_repo} 按 config[worktrees]/[target_repo]
  渲染;cwd/cwd_template 双键都认(临时兜底,正统拼写待 Q7);占位解析不出 →
  fail-closed 不执行命令+可诊断报错(原静默 '.' 是假绿方向)。
- 回环修复:async_core 的 _finalize_envelope 原裸跑在事件循环上(实测停摆 1.58s),
  改 await asyncio.to_thread;verify 降级原静默,现双环 append_system_event
  给 moderator,正文带 exit_code+输出尾(meta 不进视图,不搬进 body 下游看不见)。
- 待用户裁决三条已录 QUESTIONS.md Q6({cwd} 入宪+方言)/Q7(cwd 拼写)/Q8(未配
  verify 的 acceptance 放行否)。Q6 拟稿:"start_cmd/resume_cmd 支持 {cwd} 占位:
  argv 分词之后逐 token 字面替换为该角色 worktree 绝对路径;含空格路径恒单
  argv 元素;未出现占位时 argv 逐字节不变;不作用于自动注入的工具参数与视图正文"。
  → 2026-07-26 三条均获用户裁决采 A,spec 修正案同日落地(§11.1 增 {cwd} 条款、
  §8.3 行450 统一 cwd 拼写、行452 必要条件限定于配置了 verify 的角色);
  遗留一件实现尾巴:core.py cwd_template 兜底键待撤(已挂卡,测试先行)。

**联跑取证(oc-ws 主床 t-19eefb7c,不依赖中转站;Lead 逐项亲查 events.db/文件系统)**:
- E3 acceptance meta.verify.exit_code=0("12 passed");E6 人为负控:往测试里插错断言
  → tester 发 acceptance 落盘成 **report**、exit_code=1 带 FAILED 明细(§8.3 降级
  链路实证);E9 修复后 acceptance exit_code=0("13 passed",与 Lead 在 worktree
  亲跑逐字一致)。围栏三点全过:worktree 内两文件+wip 提交只碰 write_scope;
  工作区根/线程目录零泄漏 .py;目标仓 master 未被写。code-ws 加餐 t-086313f0:
  E4 acceptance exit=0,且 tester/moderator 真实降级到 opencode 接力,异构接力下
  钩子照常盖章。注意:oc-ws 联跑发生在回环修复**之前**,故该线程降级无 system
  提示行属预期;修复后语义由 6 条先红后绿用例钉死。
- **重要经验(第一次联跑失败换来的)**:把协议措辞写进 assign 正文,对非收件角色
  会被 §6.3 背景层压缩吃掉(t-8ab284bd 实证:tester 视图里协议整段没了→又发
  handoff 假绿)。**协议必须写 roles[*].prompt 文件**(进系统层,每轮全文必发)。
  oc-ws/code-ws 已建 prompts/{backend,tester,moderator}.md。
- 演示床配置加固:opencode start_cmd 追加 --dir {cwd}(oc/code/hetero 三份);
  给所有可发 acceptance 的角色补 verify(编排器侧通用口子待 Q8)。

**联跑捎出的新缺陷(各挂卡)**:
- unavailable_patterns 朴素子串匹配误判:code-ws 旧跳闸 reason"命中 '429'"实为
  sessionId(UUIDv7 …e429)尾巴;真因是 stopReason=Cancelled(max-turns 用尽)。
  且 pattern 命中=立即停用不走 trip_after 连败计数,语义差异建议 §5.6.3 挑明。
- permissions._git 缺 encoding:text=True 在 Windows 退 cp936,中文提交信息→
  UnicodeDecodeError 死在 reader 线程,调用点拿 rc=0+stdout=''(不抛错)。已验
  §8.2 审计方向 fail-closed(quotepath 转义,非空串)不漏放,但每轮 stderr 刷
  traceback+静默空结果是真缺陷。
  → 已修(2026-07-26,销卡):全仓排查 src 下 subprocess 文本模式共 5 处,
  除 adapters Q1 样板原带 encoding 外,同病四处——permissions._git /
  core._run_verify / systemexec._run_gate_op / async_core.register_async_job
  (Popen)——均补 encoding='utf-8'+errors='replace'(与样板同款;replace 保
  rc 与 ASCII 诊断不丢,cp936 子进程输出最多乱码不再断读)。真实每轮炸点是
  `worktree list --porcelain` 输出的中文绝对路径(本仓路径即中文)→复用判定
  失明。tests/test_subprocess_encoding.py 五用例钉死(cp936 环境修前 5 红/
  修后全绿 422 passed;UTF-8 locale 机器天然绿,文件头有注)。tests 里三个
  _git setup helper 不动(输出不解析或 quotepath 转义后纯 ASCII)。
- 遗留 minor(评审报告在案未修):test_cli_adapter.py 的 fail-closed 用例用进程
  cwd 标记文件断言(回归时污染+粘滞);无写权角色的 {cwd} 解析为线程状态目录
  (纵深防御口子,建议指空沙箱);8 条调度语义用例寄存 test_cli_adapter.py 待迁;
  code-ws 注释"隔离仍由 worktree+审计兜底"说满(审计看不见 worktree 外写入)。
- 销卡:task_1584e720(opencode --dir 注入)由本轮修复达成。task_bbff7655(tools
  注入位置)仍在用户另一会话,本轮明令工人未碰其领地。

### Q7 落地收尾:撤 _run_verify 的 cwd_template 兜底键(2026-07-26,测试先行)

- 决定:配置写了废键 cwd_template → **执行期 fail-closed**(exit_code=1、输出点名
  废键与正统拼写 cwd、不执行命令),不选"按未配 cwd 走缺省语义"。理由:静默忽略
  会让旧拼写存量配置退回兜底 '.',在编排器自身目录跑出假绿验收证据(§16.5 方向),
  与 _render_verify_cwd 对未解析占位的取舍同构;且检查落在 _run_verify 执行期,
  不越 core.py 触碰面(装载期报错须碰配置装载层)。
- 先红后绿:test_run_verify_rejects_removed_cwd_template_key(红于旧实现 exit=0);
  3 条既有承载用例({target_repo}/fail-closed/降级)同步改写为 cwd 键。全仓 grep
  cwd_template 配置读点归零(余者皆为拒收实现、拒收测试与 NOTES/QUESTIONS 史料)。

### unavailable_patterns 误判修复(2026-07-26,缺陷修复会话)

- 实证:code-ws adapter_state ts=1785037196,grok_chat 被记"命中特征 '429'",
  reason 摘要就是 stdout 全文——stopReason=Cancelled 正常输出里 sessionId
  (UUIDv7 尾 "…0758bd76e429")撞 '429' 子串。
- 根因定性:**实现越界,非 spec 缺陷**。spec §5.6.3 第 1 条与 m5-contract §2
  列举的分类输入只有 stderr / 进程退出信息 / 无输出错误;实现却把 stdout 正文
  塞进了两个调用点(无 json 块路径 `stderr, _exit_info, stdout`;超时排空路径
  `exc.stderr, exc.output, drained_err, drained_out`)。修法=收窄回字面列举,
  移除两处 stdout 侧输入(adapters/__init__.py);ApiAdapter 路径传的异常消息
  属"错误文本",不动。测试先行 3 用例(2 红 1 守卫,test_m5_adapters.py ⑨ 区块):
  stdout UUID 不跳闸 ×2、stderr 真 429 仍跳闸 ×1;既有超时用例的特征文本本就在
  stderr 侧,零回归。
- 澄清(更正上节"语义差异建议 §5.6.3 挑明"):经查 spec 原文**已明文挑明**——
  §5.6.3"满足其一即置 disabled",第 1 条特征命中"→ 立即跳闸"且"不计 attempts",
  第 2 条才是 fail_streak ≥ trip_after。pattern 命中绕过连败计数是 spec 字面
  语义,现状实现与 spec 一致,无矛盾无缺口,不入 QUESTIONS。
- 残留升级:列举之内的文本(stderr/异常消息)含十六进制串仍可撞子串;"子串"是
  spec 明文口径,修订属修宪 → QUESTIONS.md Q9(推荐 A 维持:实测 `\b额度\b`
  不命中"本月额度已用尽",词边界方案必打红中文 pattern 用例)。
- Cancelled 类输出修后走"无 json 块"ValueError → §5.1 原地重调 + §5.6.3 第 2 条
  连败计数,连续 3 次照样跳闸——分层恰好是 spec 想要的:取消/截断是质量或瞬时
  问题,重试;真额度报错(stderr)即时跳闸。

### 三缺陷卡集成(2026-07-26,Lead 收卡)

三张 chip 卡各自独立 worktree 施工,Lead 逐卡三步收卡(diff 白名单审计零越权/
各自 worktree 亲跑 pytest 全绿 420/422/417/commit 已在卡内),按依赖序 --no-ff
合入 main:编码修复(e6dd286)→ cwd_template 撤除(6a908f3)→ 429 误判修复
(0fe011c)。Lead 集成胶水仅两处:①NOTES 三家同点追加的合并冲突,保序缝合
(Q7 收尾在前、429 修复在后),并把 429 节标题级别从 ## 归一为本文件惯例 ###;
②core.py _run_verify 被编码卡与撤键卡同函数改动,ort 自动缝合,Lead 亲读确认
废键拒收与 encoding 参数共存无损。集成门:全量 425 passed, 5 skipped +
混沌门 --chaos-m5 429 passed, 1 skipped,均 exit 0。
新增待决:Q9(§5.6.3 子串口径对十六进制串撞击,429 卡升级,推荐 A 维持)。
→ Q9 同日获用户裁决采 A:维持子串口径,spec 与实现零改动;残留 stderr 撞击
风险接受(有 enable 恢复路径),中文 pattern 兼容性为决定性论据。当前待决归零。

### 控制台对齐 AionUi:T1–T3+三回环(2026-07-29~30,Lead 施工台账)

- 立项链:调研报告 aionui-alignment-study.md(workflow wf_25c86736,10×opus,b19bf04)
  → Q10 用户裁决采 A 修 §0 例外(spec b941897)→ 用户批准 T1–T3 先行、T4 待前置
  → Q11 用户 07-30 指示"每条记录看到具体细节"解除 T4 前置(f537d87)。
- T1(5ddba24)成员名册+状态灯+单聊:store 新增只读 dispatches_snapshot(禁改
  pending_dispatches,7 调用点依赖"只回 pending");/status 全五态+deadline_ts;
  四态灯 live 判据防崩溃滞留假绿;isMemberRelated=sender∨to,注释显式声明非 §6.2
  焦点窗、不回流调度;docs/m0-contract §2 增补。收卡:白名单零越权,91 绿+全量
  430,exit 0。工人两处报备均属卡内授权分支:「可裁决」徽标缓做(/api/config 只回
  yaml 原文无解析 roles,硬做会挂错人——留作后续候选卡);滞留 dispatching 归灰
  +如实 title。
- T2(998fd53)待办+统计:声明序 firstSeen+勾选字形(纯展示映射不写回,schema
  不动)+「第N/M步」;_round_stats 纯函数挂 /events **同级键**(事件元素 12 键
  冻结面零触碰)。Lead 决策①「轮」口径=最后一条 sender=='human' 事件起的窗口
  (spec 零定义,serve 属实现自由;可盘上重建,§16.9 合规);②统计卡不设「工具
  数」——当时盘上无痕迹不编造,第三栏=invoke 次数。收卡:98 绿+全量 437。
- T3(4bd04df)config 黑屏缺陷:yaml.YAMLError 非 ValueError 子类(MRO 亲验)
  穿透修复;红转绿 10 用例含反向守卫(monkeypatch RuntimeError 断言穿透,钉死
  未退化裸 except)。收卡:48 绿+全量 447。
- 首轮独立评审(b941897..4bd04df 全 diff):**阻断 0/应修 4/建议 7**;§16 十三条
  逐条核销,冻结面零触碰确认,新增转义逐处过,测试抽查 4 强 1 弱。
- 回环闭环(评审与 Lead 均未代改;应修 4 条全清):
  · T1回环(3fd8dc1):取消单聊解锁 #send-to——解锁收在 readFilters,统一覆盖
    再点取消/清除全部/chips ✕/手动取消勾选四条退出路径;dispatching chip 改
    live 判据(与胶囊/绿点同判);灰档 title 分 failed/gate_wait/stale 如实;
    populateFilterRoles 属性位 escapeHtmlAttr 补洞;DISPATCH_CLOCK_SKEW_S=3s
    展示容差;探测器 docstring 补 deadline_ts。收卡:102 绿+全量 451。
  · T3回环(2c67e77):_read_config_file_checked 三态区分(缺失/空文件=合法无配置;
    语法错/OSError/顶层非映射=一句人话),宽松版委托保契约(mock 证同一装载);
    坏 config → 角色投影空表+/status 顶层 config_error(仅错误时出现;字符串型
    不撞 _role_projection 结构探测),前端"⚠ 配置读取失败·绑定未知"警示;
    import yaml 移出 try 的导入期语义变化注释说明(不可测原因写明)。工人进程
    中断未交报告,Lead 亲验逐条代收:120 绿+全量 459。
  · T2回环(d5cd619):黑板三节改吃**权威** state.json——新增 GET /board 用既有
    公开 board_state(store 零改动);sort_keys=True 落盘抹平插入序(store:643-647
    亲验),故服务端扫事件补 task_order/task_evt 溯源键、只对权威已存 key 生效;
    projectBoard 整体退役**无兜底**(兜底=保留一条采信被拒自述的路径),端点
    失败渲染显式红字。原 owner 档案丢失,接替工人完成。收卡:135 绿+全量 464。
- Q12(02ebe55,待人裁):T2回环实盘复现 rebuild_blackboard 重放只按 type 不判
  can_decide,§9.1 恢复会把曾被拒的 bb_ops 灌回权威黑板(违 §4.6"重放=增量")。
  展示层已防(吃 state.json 而非自算),但恢复一旦发生污染的是权威状态本身;
  修法四选项见 QUESTIONS.md,根因在 store/scheduler 侧,不入本轮控制台卡。
- 评审建议处置:纳入 6 条(见各回环 commit);记录不改 3 条——①JS 四个新纯函数
  无行为断言(仓库无 JS 运行时,§14 单测要求不含 web 层,字符串断言为既有惯例);
  ②workspace 级花名册缓议(每拍扫全部 t-* 开 sqlite 是最重轮询热点,先量后决);
  ③单聊发送不自动 run(POST /run 阻塞到终态,自动触发把"发一句"变"同步跑到
  线程终止")。checklist 维护定 pm 不动(prompts/moderator.md 明写黑板不代劳,
  §11.2 禁 moderator 产出实体工作)。
- Q11 落地口径(Lead 裁量记录):steps 暴露=解析摘要(工具名+命令摘要截断+计数
  +耗时),stdout 原文不经 HTTP 直出(§12 缺省 127.0.0.1 无鉴权+Q9 实证原文含
  sessionId;用户若自行反代扩网面,风险自担并另议鉴权);acceptance/decision
  气泡上的 steps 折叠组带「后端自述·非系统验证」标注、不与 meta.verify 徽章
  争位——由评审"禁挂"建议放宽而来,依据 Q11 用户"细节全览"指示,verify 徽章
  仍是唯一系统侧证据。
- 工人事故簿:本轮三次进程中断收尸——T3回环(活已完,亲验代收);T4 首跑(API
  流 600s 卡死,遗 975 行半成品);T4 续跑(SendMessage 复活后零进展再中断)。
  T4 交收尾工人接手(重派 2/2,现场断点:styles.css 仅 2 行+唯一红=位置断言
  用 str.index 首次出现定位、疑把函数定义误当调用点)。app.js 4 个 NUL 字节:
  用户已在独立会话跑 chip 排查,施工全程各卡自证字节计数守恒。
