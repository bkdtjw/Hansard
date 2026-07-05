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
  裁决: 待陪跑

Q2 [M2/T-realE2E] 真实后端小功能全流程 + 停机三小时验收
  背景: spec §15 M2 验收标准要求"≥2 家异构后端 + 1 次门禁 + 停机后重启续跑"
  M2 夜段范围: 用 FakeCliAdapter+FakeApiAdapter 组合验证控制流；真实后端联跑等你陪跑
  建议: 配好 Q1 flag 后，`orch new "小功能" --roles backend,tester,moderator`，
        走完 review→handoff→acceptance→gate_request→approve→terminate；
        中途 Ctrl+C 停机，间隔任意时长后重启 orch run 续跑
  裁决: 待陪跑
