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
