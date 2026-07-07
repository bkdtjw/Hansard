# orch 五分钟上手（面向使用者）

> orch 是一个把多个 AI（kimi / claude 等 CLI agent）编成"群聊开发团队"的编排器：
> 你发任务，AI 们分工协作（写规格→写代码→审查），关键动作等你批准，全程可回放。
> 本文面向**操作者**；实现细节见 `orchestrator-spec.md`（面向实现者）。

## 0. 安装（一次性）

```bash
pip install -e .        # 仓库根目录执行；之后任意目录可用 orch 命令
orch --help
```

## 1. 三十秒试跑（无需任何配置，Fake 演示）

```bash
mkdir demo-ws
orch new "试一下" --roles pm,moderator --workspace demo-ws
orch run --once --workspace demo-ws
orch replay --workspace demo-ws --thread <上一步输出的 t-xxxx>
```

会看到显式警告 `⚠ 使用 Fake 演示适配器`——这是假回声，只验证控制流。
真实协作请继续下一节。

## 2. 接入真实模型（kimi 示例）

在 workspace 根目录写 `config.yaml`：

```yaml
adapters:
  kimi_cli:
    kind: cli
    start_cmd: 'C:\Users\<你>\.kimi-code\bin\kimi.exe --output-format stream-json -p'
    wire_format: stream-json
    supports_resume: false
    timeout_s: 300
roles:
  pm:        {adapter: kimi_cli, can_decide: true,  write_scope: [], tools: []}
  backend:   {adapter: kimi_cli, can_decide: false, write_scope: [], tools: []}
  moderator: {adapter: kimi_cli, can_decide: true,  write_scope: [], tools: []}
watchdog: {max_rounds: 40, loop_limit: 4}
```

然后：

```bash
orch new "任务描述（写清楚协作流程与收尾要求）" --roles pm,backend,moderator --workspace <ws>
orch run --workspace <ws>        # 长驻驱动；每步派发/落盘有 [run] 过程日志
```

> 任务描述小技巧：写明"谁做什么→handoff 给谁→最后 moderator 输出 type=terminate
> 结束"，收尾会更干脆。

## 3. 要真实写代码？加两行配置

```yaml
target_repo: 'C:\path\to\你的代码仓'        # 必须是 git 仓库
roles:
  backend: {adapter: kimi_cli, can_decide: false, write_scope: ['src/', 'tests/'], tools: []}
```

- 每个有 `write_scope` 的角色获得**隔离 git worktree**（分支 `feat/t{线程id}-{角色}`）。
- 每轮调用后系统自动 commit（`wip:{角色}@E{事件号}`）并做**越权审计**：
  写出 `write_scope` 之外 → 整体拒收 + `git reset --hard` + 审计事件通报。
- 验收满意后你自己合并：`git merge feat/tt-xxxx-backend`。

## 4. 网页控制台（推荐日常用）

```bash
orch serve --workspace <ws> --port 8801
```

打开 http://127.0.0.1:8801 ：群聊事件流实时跟新（1.5s 轮询）、黑板（契约/决策/
任务）、门禁一键批准/拒绝、▶运行一轮、回放、Ctrl+K 切换线程。
一个 serve 对应一个 workspace；多工作区起多个端口。

## 5. 门禁：线程"挂起"了怎么办

任何发给 human 的信封都会**挂起线程**等你裁决（安全设计）：

- 控制台：顶部琥珀条 → 点【批准】/【拒绝】。
- 命令行：`orch approve <corr> --thread t-xxxx --workspace <ws>`
  - 正式门禁的 corr 形如 `gate-01`（gate_request 自带）；
  - 普通请示（agent 直接 @你）corr = `gate-{事件号}`（挂起日志里会直接给出）。
- 批准后线程恢复，继续 `orch run`（若 run 长驻着则自动续跑）。

## 6. 常用命令速查

| 命令 | 作用 |
|---|---|
| `orch new "任务" --roles a,b --workspace ws` | 建线程（任务成为 E1） |
| `orch run [--once] --workspace ws` | 驱动调度（长驻/单轮） |
| `orch send t-xx "内容" --workspace ws` | 以 human 身份发言 |
| `orch approve/reject <corr> --thread t-xx --workspace ws` | 门禁裁决 |
| `orch threads / status t-xx --workspace ws` | 列线程 / 看状态与派发 |
| `orch replay --thread t-xx --workspace ws` | 全程回放（markdown） |
| `orch reopen t-xx --workspace ws` | 重开已终止线程 |
| `orch attach t-xx <role> --workspace ws` | 拿到某角色会话的接入命令 |
| `orch metrics --workspace ws` | 聚合节省/首次合法率等指标 |
| `orch stop --workspace ws` | 优雅停掉长驻 run |

## 7. 排错 FAQ

- **看不到过程？** `orch run` 的进度日志走 stderr（`[run] E[..] → backend 派发…`）；
  重定向时记得 `2>&1`。
- **中文乱码？** 新版入口已强制 UTF-8；仍异常时设 `PYTHONIOENCODING=utf-8`。
- **线程一直 suspended？** 见第 5 节——有信封在等你裁决；`orch status` 看
  gate_wait 行，挂起日志给出可用的 `gate-{事件号}`。
- **workspace 放哪？** 放固定目录（如桌面/文档）。**不要放系统临时目录**——
  事件日志是唯一真相，Temp 会被系统清理（控制台顶栏会红字告警）。
- **agent 干了半截崩了/断电了？** 直接重跑 `orch run`——恢复算法只查表不猜测，
  事件恰好一次生效（50 轮混沌测试 100% 通过的那套机制）。
