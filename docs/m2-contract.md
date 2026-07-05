# M2 冻结接口契约（跨任务卡边界）

> Lead 设计规格（非实现代码）。M0/M1 已冻结接口继续有效。与 spec 冲突以 spec 为准。

## 0. M2 范围与边界（spec §15）
- **新增**：真实后端冷启动（§7.2 CLI 型 + §7.3 API 型）、权限三件套（§8.1 worktree 隔离 + 工具白名单 + §8.2 diff 越权审计）、门禁与系统执行器完善（§10/§5.5）、CLI 用户界面（§12 部分命令）。
- **不做**：resume 热续增量（§6.5，M3）、聚合/写域并行、多线程（M3）、混沌完整化（M4）。
- **验收标准**（spec §15）：≥2 家异构后端完成 1 小功能全流程，含 1 次人工门禁 + 停机后重启续跑；越权写入注入测试被拦截。
- **playbook §5 明示**："M2 仍是最费人力段：真实 CLI 的 flag、session_id 提取要现场实测，安排整块时间陪跑。"

## 1. 诚实边界（**M2 无人值守夜段可自动完成的范围**）
自动化边界（Lead 整晚做）：
- (a) CLI 型适配器骨架（§7.2）：进程管理、`--allowedTools` 注入、cwd=worktree、输出解析（取最后一个 ```json 块）。
- (b) API 型适配器（§7.3）：直连 messages 单步；`supports_resume=False`；本项目 API 型角色（moderator）不配工具。
- (c) 权限三件套（§8.1/§8.2）：worktree 隔离（每有写权限角色一 worktree）、工具白名单（CLI 参数）、§8.2 diff 越权审计（`git diff --stat` 触及路径 ⊆ write_scope，违规 `git reset --hard` + system 审计）。
- (d) 门禁 + 系统执行器（§5.5/§10）：M0/M1 已有大部分；M2 补 `gate_ops` 命令模板执行 + 凭据只在编排器环境。
- (e) 停机-续跑控制流验收：用**假 CLI 适配器**（模拟子进程延迟 + session_id 返回）在 mock 语境下端到端跑\"gate_wait→kill 主进程→重启→approve→续跑到 terminate\"。
- (f) CLI §12 子集：typer 命令 `run / new / send / chat / status / approve / reject / stop / attach` 骨架，参数与 spec §12 表一致。

需人陪跑（升级 QUESTIONS.md，**M2 不宣告完全完成**直到人陪跑）：
- (g) 三家真实 CLI（claude / codex / kimi）的 `--help` flag 实测、session_id 提取正则、命令形态。
- (h) 真实后端与真实 CLI 联跑 1 个小功能全流程。
- (i) 停机三小时重启续跑（需你实际操作停机+隔时段 approve）。

M2 完成定义：(a)-(f) 全绿 + 独立评审无阻塞 + QUESTIONS.md 明列 (g)/(h)/(i) 升级项供你醒来陪跑。

## 2. `orch.adapters` 扩展（§7.1/§7.2/§7.3）—— owner T2(M2)

M0 已有 `Caps` / `MockAdapter`。M2 追加：

```python
class CliAdapter:
    """CLI 型适配器（§7.2）。子进程执行，cwd=角色 worktree，权限经 CLI 参数注入。"""
    caps: Caps
    def __init__(self, *, role: str, config: dict, worktree: Path,
                 caps: Caps | None = None): ...
        # config 结构见 §11.1：{kind:cli, start_cmd, resume_cmd, timeout_s, ...}
        # M2 仅冷启动路径；resume_cmd 骨架保留，实际调用是 M3。
    def invoke(self, view: RenderedView, sess: Session | None
              ) -> tuple[dict, Session | None]:
        """冷启动：subprocess 执行 start_cmd + view['text']；超时 kill。
        输出解析：取标准输出最后一个 ```json 块（§17 §7.2）；解析失败按 §5.1 原地重调路径。
        提取 session_id（M2 骨架：从输出 JSON 的 session_id/sid/session 字段任一 + 正则兜底，
        由 config.session_id_extract 提供正则；无匹配则 sid=None、gen+=1）。
        session_id 提取的具体正则/字段名属 §17 开放决策，M2 骨架给缺省 + 允许覆盖。"""

class ApiAdapter:
    """API 型（§7.3）。直连 messages 接口；无会话；永远全量组装；supports_resume=False。"""
    caps: Caps
    def __init__(self, *, role: str, config: dict, caps: Caps | None = None): ...
    def invoke(self, view, sess): ...

class FakeCliAdapter:
    """（测试/夜段自动化用）——模拟真实 CLI 子进程行为的假适配器：
    支持超时、session_id 提取、可注入越权写入以验证 §8.2 审计。
    对外接口与 CliAdapter 一致，但不真启动 CLI（避免夜段无人时依赖外部安装）。"""
```

## 3. 权限三件套（§8.1/§8.2）—— owner T3(M2)，接入 `orch.scheduler`

M0 mock 无 worktree，本层 skip。M2 落地：

```python
def ensure_worktrees(config: dict, target_repo: Path, worktrees_root: Path) -> dict[str, Path]:
    """为每有 write_scope 的角色建 worktree（§8.1）：
    git worktree add <worktrees_root>/t{thread_id}-{role} -b feat/t{thread_id}-{role} <base>
    已存在则复用。返回 {role: worktree_path}。API 型角色无 worktree（跳过）。"""

def audit_write_scope(worktree: Path, write_scope: list[str], last_ok_commit: str) -> tuple[bool, list[str]]:
    """§8.2 diff 越权审计。计算 `git diff --stat {last_ok_commit}..HEAD` 触及路径，
    与 write_scope 前缀匹配。返回 (是否合规, 违规路径列表)。
    违规处理由 core.py 负责：整体拒收信封 + git reset --hard {last_ok_commit} +
    追加 system 审计事件转 moderator（§8.2 简化决策，不做部分裁剪）。"""

def autocommit(worktree: Path, role: str, event_id: int) -> str | None:
    """§4.5：有改动则 git add -A && git commit -m "wip:{role}@E{event_id}"，返回 commit sha；
    无改动跳过返回 None。commit message 格式固定（恢复 §9.2 依赖）。"""
```

## 4. 门禁/系统执行器完善（M0 已有主要逻辑）—— owner T3(M2)
M0 `apply_gate_decision` 已实现基础；M2 补：
- gate_ops 命令模板从 config 读取，凭据只在编排器进程 env（**不进任何 agent 环境**，§5.5）。
- 特权操作分类：`merge_main` / `deploy` / `run_ci` 等；模板 `{target_repo}` `{branch}` 占位替换。
- run_ci 长作业异步登记 jobs 表（M0 同步退化仍保留，M3 真异步）。

## 5. CLI（spec §12 子集）—— owner T4(M2)
`src/orch/cli/main.py`(typer)：
```
orch run                          启动调度进程（常驻）
orch new "任务" [--roles ...]    建线程：目录/db/worktrees + E1 入队
orch send t-xxx "..." [--to r]    人类发言入队
orch chat t-xxx [--follow]        事件日志渲染为群聊
orch status t-xxx                 派发表 + 状态
orch approve|reject <corr>        门禁裁决
orch stop / orch reopen t-xxx     优雅停机 / 重开
orch attach t-xxx role            打印该角色原生会话接入命令（§12 明列"必须实现"）
orch threads                      列线程
```
命令与 spec §12 表一致;flag 具体形态以 `--help` 实测为准(M2 骨架 + QUESTIONS.md 升级差异)。

## 6. 测试 M2 约定
- 权限三件套单测:`ensure_worktrees`(mock git worktree)、`audit_write_scope`(构造越权 diff 断言拒收+reset+audit)、`autocommit`(格式与恢复引用一致)。
- CliAdapter 用 `FakeCliAdapter` 走单元测试(不依赖真实 claude/codex 安装):session_id 提取、超时、越权注入。
- ApiAdapter 用 `FakeApiAdapter`(模拟 API 响应 dict)测。
- **端到端**:附录B 或简化 fixture,在 `FakeCliAdapter`+`FakeApiAdapter` 组合下跑通\"gate_wait→kill 主进程→重启→approve→续跑到 terminate\"控制流(不真起 CLI)。
- CLI 测试用 typer.testing.CliRunner。

## 7. M2 里程碑简化清单(记 IMPLEMENTATION_NOTES.md)
1. 真实 CLI 实测项(g)/(h)/(i)升级 QUESTIONS.md,等你陪跑。
2. FakeCliAdapter 是**测试双**,不是产品实现。真实 CliAdapter 骨架含 subprocess 逻辑但需人实测 flag。
3. session_id 提取正则默认从常见字段(session_id/sid/session),config 可覆盖;真实值以 `--help` 实测为准。
4. resume_cmd 保留骨架但 M2 不调用(M3)。
