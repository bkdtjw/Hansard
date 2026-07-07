# 可用性 × 使用质量 × 协作流畅性 全面审视（2026-07-06）

> 方法：绿基线（286 passed）→ CLI 15 命令逐个实跑（含错误路径/首次使用路径）→
> Web 控制台 11 项全流程 DOM 亲验 → 全新真实 kimi 协作线程端到端实测（fluency-ws
> t-5ce5f7e8，验证 Q6/Q7 修复栈）→ 三条真实线程横向时延分析。全部证据可复算。

## 一、总判定

系统**真实可用**：CLI 与控制台两条入口全流程走通，两次落代码项目产物亲验可运行，
协作机械层（路由/聚合/挂起/恢复/终止）经修复后全链路顺畅。主要短板集中在
**新手首次上手体验**与**运行过程可观测性**；协作层剩余摩擦已从"机制缺陷"降级为
"LLM 行为波动"（可由提示词工程继续压缩）。

## 二、可用性（CLI + 控制台）

### 好的
- CLI 15 命令全谱（new/run/serve/send/chat/approve/reject/attach/replay/reopen/
  stop/status/threads/metrics/bench），replay 输出可读性佳，metrics 真数，
  attach 给出可复制的真实 resume 命令。
- 控制台 11 项全流程走查全绿：建线程弹层/流渲染/回放/接入浮层/配置自动载入/
  指标 11 卡/基准页真跑/Ctrl+K 切换器/门禁一键批准（Q6 后含非正式门禁）。

### 问题清单
| 级 | 问题 | 证据 | 建议 |
|---|---|---|---|
| P1 | 默认 GBK 控制台下 CLI 全乱码 | `orch --help` 不设 PYTHONIOENCODING 时输出 `����` | 入口 `sys.stdout.reconfigure(encoding='utf-8')` |
| P1 | `orch run` 全程零过程日志 | 空配置工作区 4 事件默默跑完，长驻无一字输出 | 每派发/落盘一行进度日志 |
| P1 | 无 config 时静默用 Fake 适配器"假跑" | audit-empty-ws：pm 回 "run-once ack" 即终止，新手会误以为真跑 | 无 adapters 显式警告或拒跑 |
| P2 | CLI 错误 = 完整 traceback 直喷 | `approve gate-99` 吐全栈（文案本身是对的） | CLI 层捕 KeyError → 一行人话 |
| P2 | 命令语法不一致 | approve 用 `--thread` 选项、attach 用位置参数 | 统一 |
| P2 | 一工作区一端口 | 多工作区需起多个 serve，控制台无切换器 | serve 支持多 workspace 或 UI 切换 |
| P3 | 无使用者 README | spec 面向实现者；操作者无 5 分钟上手文档 | 写 docs/USAGE.md |

## 三、使用质量

- 基线：**286 passed + 50 轮混沌 100%**；错误文案中文、准确带上下文。
- 产出质量：todo（129 行 + 15 测试）、calc（109 行递归下降 + 17 测试）均亲验可运行。
- P3 观察①：背景压缩比在短消息线程 <1（calc 0.73 / fluency 0.91）——一行摘要
  格式开销大于短正文，属采集口径特性非缺陷（长历史场景 2.0:1~8.7:1）。
- P3 观察②：终止后飞行中的迟到回复照常入账（fluency E10 晚 terminate 16s 落盘）
  ——符合"日志=真相"+§5.4 只拒新派发，但初见意外；可在 UI 标"终止后到达"。

## 四、协作流畅性（核心实测）

fluency-ws t-5ce5f7e8（全新线程，带 Q6+Q7 修复栈，真实 kimi）：

```
E1 human assign → E2 backend 方案(31s) → E3 moderator 请示 human(28s,挂起)
→ approve gate-3 → E5 moderator 引用批准内容但偏航派活 backend(8s)
→ E6 backend 引用 #1 原始约定纠偏(14s,再挂起) → approve gate-6
→ E8 moderator 15s 内自主 terminate → E9 终止清单(0s)
```

- **机械层已顺**：approve 后 moderator 完整看见裁决内容并自主收尾，全程零人工
  解释性补话（对照修复前：线程卡死 + "未收到 #5 事件内容"）。
- **时延**：纯讨论 19s/agent 轮、落代码 50–69s/轮（calc n=5 均 50s、todo n=7 均
  69s）；系统自身开销 ≈0（terminate→清单 0s）；墙钟由人响应时间主导。
- **首次合法率**：fluency 100%（6/6）、calc 83.3%——schema 携错重调兜底有效。
- **真实涌现**：moderator 批准后偏离剧本 → backend 引用原始约定当场纠偏 →
  多智能体制衡真实发生（代价 2 轮 + 1 次额外批准）。剩余摩擦属 LLM 行为层。
- 已知边界（沿革）：跨角色 worktree 依赖未解（tester 看不到 backend 代码）。

## 五、快赢清单（按性价比排序）

1. run 过程日志（P1，几行代码，可观测性质变）
2. 入口强制 UTF-8（P1，一行）
3. 无 adapters 显式警告（P1，几行）
4. CLI 错误一行化（P2）
5. docs/USAGE.md 五分钟上手（P3，对外演示价值高）

## 六、环境台账

- 控制台：8801=todo-ws、8802=calc-ws（本次审视中重启，载入最新代码）。
- 实测产生：fluency-ws/t-5ce5f7e8（terminated）、Temp/audit-empty-ws（探针，可删）。
- 绿基线：286 passed + 1 skipped（本次审视开场亲跑）。
