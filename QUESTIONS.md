# 待决问题（QUESTIONS.md）

单条格式（手册 §6，一律遵守）：

```
Q{n} [{里程碑}/T{k}] {一句话问题}
  背景: spec 哪一节缺失或矛盾
  选项: A … / B …（必须附建议与理由）
  裁决: {人填} → 归档进 NOTES 或触发 spec 修订
```

---

（当前无待决问题）

Q1 [M2/T-realCLI] 三家真实 CLI（claude/codex/kimi）的 --help flag 与 session_id 提取
  背景: playbook §5 明示 M2 需现场实测 flag / session_id 提取，需你陪跑
  M2 夜段范围: 只实现 CliAdapter 骨架 + FakeCliAdapter 测试双；真实 CLI 联跑等你醒来
  建议: 醒来后依次 `claude --help / codex --help / kimi --help` 记 flag 到 IMPLEMENTATION_NOTES.md；
        提取 session_id 正则（每家不同）；用 orch attach 验证
  裁决: 2026-07-05 陪跑达成（kimi）。实测 flag：`kimi -p "<prompt>" --output-format stream-json`；
        回复在 `{"role":"assistant","content":...}`，session_id 在 `{"type":"session.resume_hint"}` 行；
        Windows 需 kimi.exe 完整路径 + UTF-8 解码。已接入 CliAdapter（wire_format=stream-json 解包
        + supports_resume 可配）+ config 真实装配（_build_adapters_from_config）。claude 因工具子进程
        OAuth 凭据隔离 401，待你终端修通认证后同法接入（其 --session-id 可自控 UUID、
        --output-format json 单结果，无需提取正则）。codex 未测。
        2026-07-25 追记：第 3/4 家接入达成（grok 0.2.112 / opencode 1.18.4，
        实测细节与接入胶水见 IMPLEMENTATION_NOTES"真实后端第 3/4 家接入"节；
        grok=json 双键名兼容、opencode=新增 opencode-stream 解包；热续均实测通过）。

Q2 [M2/T-realE2E] 真实后端小功能全流程 + 停机三小时验收
  背景: spec §15 M2 验收标准要求"≥2 家异构后端 + 1 次门禁 + 停机后重启续跑"
  M2 夜段范围: 用 FakeCliAdapter+FakeApiAdapter 组合验证控制流；真实后端联跑等你陪跑
  建议: 配好 Q1 flag 后，`orch new "小功能" --roles backend,tester,moderator`，
        走完 review→handoff→acceptance→gate_request→approve→terminate；
        中途 Ctrl+C 停机，间隔任意时长后重启 orch run 续跑
  裁决: 2026-07-05 陪跑达成（kimi 单家扮 backend/tester/moderator）。orch new 小任务 → 真实 kimi
        三方协作 backend→moderator→tester→moderator→human（全真实 LLM，信封/路由/语义正确）→ human
        send 指令 + reopen → moderator 真实输出 terminate → terminated；终止台账记录三角色真实
        session_id。全链路：render 视图→kimi.exe subprocess→stream-json 解包→信封入队→调度→session
        提取→终止清单。遗留：≥2 家异构厂商（claude 待认证）、"停机三小时"长时段（控制流已由 M2
        假适配器 停机-重启-approve 端到端验证；真实长时段可即时 Ctrl+C 重启演示，未跑满三小时）。
        2026-07-25 追记：遗留之一"≥2 家异构厂商"达成——grok+opencode 同线程混编联跑
        （hetero-ws t-db6a3fa7），且含 M5 降级实战：grok 半途被人工禁用，opencode
        冷启动接手上下文零丢失至收尾，降级审计事件/指标/门禁恢复全链路真实验证。

Q7 [render/§6.2×§10] A 类触发事件对目标角色不可见：gate_decision 后 moderator 收到"只针对 #5 回应"却看不到 #5 内容
  背景: 真实联跑铁证（calc 线程 E5 invoke 日志）：渲给 moderator 的 prompt 含指令尾
        "现在只针对 #5 回应"，但全文不含 approve/gate_decision 任何字样——kimi 回
        "未收到 #5 事件内容，无法处理"是诚实正确的模型行为。根因是 spec 内部张力：
        §3.2/§6.2 规定 A 类只投影黑板、焦点窗只渲 B 类，而无 bb_ops 的 gate_decision
        在黑板上无任何投影；§10 却要求 gate_decision 发回申请者"让申请者知道裁决并
        续走流程"。凡 A 类事件作为触发件（gate_decision/acceptance/decision→某角色）
        均受此影响。属 spec 内部矛盾，按 CLAUDE.md 停下报告，不擅自修改。
  选项: A 渲染层通则——本轮触发批次（view.event_ids）内的事件无论保留策略一律全文
        入焦点窗（"触发件必须可见"原则；最小改动，连带修复 acceptance/decision 触发
        同类盲区）——推荐；B gate_decision 专项——approve 时把裁决文本并进指令尾或
        伴随一条 B 类 system 事件；C 修订 spec §6.2 明确 A 类触发件渲染语义。
  裁决: 2026-07-06 用户裁决采 A（"你推荐方法不错可以"）：触发批次（view.event_ids）
        内的事件无论保留策略一律全文入焦点窗；批次外 A/C/D 语义不变。测试先行实现。

Q6 [gate/§10] "非正式门禁"不可恢复：非 gate_request 信封发往 human 挂起后 approve KeyError
  背景: 真实联跑（calc 线程 t-934119b0）moderator 以 handoff→[human] 收尾 → §5.1 置
        gate_wait+suspended；但 apply_gate_decision 只按 corr 查 gate_request 事件，
        此类信封无 corr → KeyError，线程永久卡死（发言只入队不解挂、run 跳过 suspended）。
  判定: 非开放决策，属实现缺口——spec §10 明文"调度器遇到 target=human 的 pending 行时
        置 gate_wait…corr 缺省时由编排器生成 `gate-{事件号}`"，§5.1 将一切 to=human 行
        送入该机制；"corr 缺省生成"条款未实现。
  裁决: 按 §10 条款补实现（用户在场指示"收尾吧，这个bug怎么处理"）：apply_gate_decision
        对生成形 corr `gate-{事件号}` 只查表反解（事件存在且 to 含 human），其余流程
        （gate_decision 回填/标 done/resume/幂等）复用原路径；UI 门禁 banner 对无
        gate_request 的挂起派生同形 corr。测试先行 3 连（test_e2e informal gate）红→绿，
        283 全绿；真实卡死线程经 `orch approve gate-4` 恢复验证。

Q4 [UI/R5] showModalInStream（回放/接入/派发明细注入流内假气泡）与 D6 轮询重渲染冲突
  背景: console-layout-revision.md D6 要求 ≤2s 轮询自动跟新；现实现 renderStream() 每次全量重建
        #chat-stream，会把 showModalInStream 注入的非事件气泡吞掉——文档未覆盖此交互冲突。
  选项: A 回放/接入/派发明细改为真正浮层 overlay（不再塞进流；D5 溢出菜单的自然配套）——推荐，
        最小干预且消除"轮询吞弹层"；B 轮询时保留非事件节点做 DOM diff（复杂度高，易碎）。
  裁决: 采 A（实施 D6 的必要前置，非功能扩展）。

Q5 [UI/R5] 线程列表的手动刷新按钮（#btn-refresh-threads）文档未点名
  背景: D6/坑3 只点名删除事件流刷新按钮；线程列表刷新按钮同样宣告"这东西不会自己动"，
        且列表数据（状态/事件数/最后活动）在轮询时代应自动更新。
  选项: A 删除按钮，列表随轮询周期低频自动刷新（选中线程 2s 节奏内每 5 拍捎带刷一次列表）——推荐；
        B 保留按钮（与 D6 精神矛盾）。
  裁决: 采 A（同坑3 判据：为"不会自己动"的宣告付常驻像素即臃肿）。

Q3 [UI/web] web events 端点需扩展返回 re/ts/meta/artifacts（console-ui-revision.md D12/D13/D14 渲染数据必需）
  背景: 文档 §A 限后端只改 D1(b)+D2；但 D12(回复链 re)、D13(时间戳 ts/meta/verify)、D14(artifacts)
        要渲染的字段虽在 events 表 DDL（re_json/ts/meta_json/artifacts_json）内，现有 _ep_thread_events
        未投影返回，前端拿不到。C1 经查库判为纯前端分支(a)（库 body 干净），故 D1 不需后端。
  选项: A 扩展 events 端点返回这些已有列（只读投影，不改 DDL/协议/调度、不加功能）——推荐，
        符合"UI 是持久层只读投影"(§E1)精神；B 不做 D12-D14（信息损失，违 spec §8.3 verify 可视化）。
  裁决: 采 A（第三处后端改动，仅投影既有库字段，属 §7 输出规范化/投影职责）；记录备查。

Q6 [M5后/adapters] start_cmd 新增 `{cwd}` 占位是 spec 未定义的配置面，是否入宪、用哪种方言
  背景: opencode 无视进程 cwd 自寻项目根（§8.1 围栏被绕）。修法需在 argv 里显式注入工作目录，
        而 start_cmd 是静态串，动态值只能靠占位。§11.1 未定义 start_cmd 任何占位；§17 十项
        开放决策均不覆盖。实现已落地（commit 2cbffad，_start_cmd_argv：split 后逐 token 替换，
        含空格路径恒单 argv 元素；无占位逐字节回归；不作用于 tools_args 与视图正文）。
        另 spec 已有两套占位方言：§8.3 用 {worktree:role}/{target_repo}，gate_ops 用
        {target_repo}/{branch}；本增补若用 {cwd} 即第三种。
  选项: A 采纳 {cwd}，一句话入 §11.1（拟稿见 NOTES"意外问题修复"节）——推荐（start_cmd 场景
        恒指本角色自己的工作目录，无需 role 参数化，与 Popen cwd 同名直白）；
        B 复用 §8.3 方言写 {worktree:self}（少一种方言，多一层解释）；
        C 不入宪，回退占位、改为各 adapter 硬编码 --dir（把供应商细节焊死进编排器，最差）。
  裁决: 2026-07-26 用户裁决 → 采 A。{cwd} 条款已入 §11.1（adapters 示例块后正文一段，
        按 NOTES 拟稿逐字）；实现（commit 2cbffad）与新条款一致，无需改动。

Q7 [M5后/verify] §8.3 行450 写 `cwd_template`、§11.1 行541 示例写 `cwd`——同一字段两种拼写（spec 内部矛盾）
  背景: 本轮修 verify 钩子 cwd 占位渲染（此前完全缺失：按 §11.1 示例配置恒 NotADirectoryError
        →acceptance 永降级；按 §8.3 拼写则键名不被读→静默在编排器自身目录跑出假绿）。
        实现临时双键都认（core.py _run_verify），配置面比 spec 宽，哪个拼写正统无落盘记录。
  选项: A 统一为 cwd，修 §8.3 那一行文字——推荐（全部既有配置与 §11.1 示例都用 cwd，
        无任何存量用 cwd_template）；B 统一为 cwd_template，修 §11.1 示例；
        C 双键都认写进 spec（把兜底转正，代价是永久双拼写）。
  裁决: 2026-07-26 用户裁决 → 采 A。§8.3 行450 已改 `{cmd, cwd}`；正统拼写确定后，
        core.py _run_verify 的 cwd_template 兜底键成为 spec 外配置面，已挂卡撤除
        （测试先行：cwd_template 键应不再被读）。

Q8 [M5后/verify] 未配置 verify 的角色发 acceptance 是否原样放行
  背景: §8.3 行450"**可为**角色配置 verify"（可选）与行452"meta.verify.exit_code==0 是
        acceptance 生效的**必要条件**"（无条件措辞）互相矛盾。现实现：未配 verify → acceptance
        原样放行且 meta 无 verify 键（等于放行"我测过了"，§16.5 反模式方向）；但按字面收紧会
        打红 tests/test_m2_e2e.py（其 tester 未配 verify 而断言 acceptance 必现，源自 M2 验收）。
        演示床已在配置层堵口（oc-ws/code-ws 给所有可发 acceptance 的角色补 verify）。
  选项: A 维持放行 + spec 澄清措辞（452 的必要条件限定于"配置了 verify 的角色"）——推荐
        （与 M2 验收标准自洽；验收强度交给配置，部署侧给关键角色配 verify 即可）；
        B 收紧：未配 verify 一律降级 report（最安全，但须改 spec 行450 与 M2 测试，波及大）；
        C 加全局开关 require_verify_for_acceptance（多一个配置面，§16 臃肿方向）。
  裁决: 2026-07-26 用户裁决 → 采 A。§8.3 行452 已改"对配置了 verify 的角色…必要条件"，
        并补一句"未配置 verify 的角色 acceptance 原样放行——验收强度由配置层决定"；
        实现即现状，无需改动；tests/test_m2_e2e.py 保持绿。

Q9 [M5后/adapters] §5.6.3"大小写不敏感子串"口径对十六进制串的固有撞击风险，是否修订匹配口径
  背景: code-ws 误跳闸（adapter_state ts=1785037196：grok_chat 被记"命中特征 '429'"，
        实为 stopReason=Cancelled 正常 stdout 里 sessionId UUIDv7 尾 "…0758bd76e429"）
        已修——根因是实现把 stdout 正文送进分类器，超出 §5.6.3 第 1 条字面列举
        （stderr/进程退出信息/无输出错误），收窄回列举即愈，不触 spec。但残留：
        列举**之内**的文本（stderr、异常消息）若含 sessionId/commit hash 等十六进制串，
        '429' ⊂ "…e429" 的撞击依然成立——CLI 把会话日志刷到 stderr 即复现。
        "子串"是 spec 明文口径（§5.6.3 第 1 条 + §17 默认清单裁决），改之属修宪。
  选项: A 维持子串口径不动（扫描范围已收窄，stderr 撞击属理论风险；§5.6.3 本就偏
        "宁可早降级"，误跳闸可 `orch adapter enable` 一令恢复）——推荐，另有硬论据：
        词边界正则会打破中文 pattern——实测 `\b额度\b` 不命中"本月额度已用尽"
        （汉字属 \w，中文文本内无词边界），即 B 必打红既有中文用例；
        B 修订 §5.6.3 为词边界匹配（`\b429\b` 不命中 UUID、命中"HTTP 429"，
        误判面大减；但须为中文 pattern 另立边界规则，spec 措辞复杂化）；
        C 允许 pattern 显式写正则（配置面膨胀，§16 反模式方向，最差）。
  裁决: {人填}
