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
