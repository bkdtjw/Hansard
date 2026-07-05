# M1 冻结接口契约（跨任务卡边界）

> Lead 设计规格（非实现代码）。冻结 M1 各模块对外符号/签名/语义边界，供 T1（测试先行）
> 与实现卡共享。与 spec 冲突以 spec 为准。M0 已冻结接口（docs/m0-contract.md）继续有效。

## 0. M1 范围与边界
- 在 M0（protocol/store/adapters/scheduler 最小核心环）之上新增：**render 四层视图组装（§6）**、
  **看门狗三级（§5.3）**、**终止清单完善（§5.4）**、**moderator/角色提示词（§11.2/11.3）**。
- moderator 仍绑 mock；**不做**：真实后端（M2）、resume 热续（M3，§6.5 增量不实现）、
  写域并行/多线程（M3）。§6.4 worktree 现场段对 mock 角色跳过（真实 CLI 属 M2）。
- 验收标准（spec §15）：视图渲染快照测试 + 环路/轮数上限触发路径测试。

---

## 1. `orch.render`（§6 视图组装）—— owner T2(M1)

M0 的最小占位 view 升级为**不对称四层视图**。分层铁律（§2）：视图组装属调度层职责、与厂商无关。

```python
from typing import TypedDict

class RenderedView(TypedDict):
    role: str
    event_ids: list[int]                 # 本轮要回应的事件号（升序）
    text: str                            # 五段拼接的完整视图文本
    sections: dict                       # {system,blackboard,background,focus,instruction} 各段文本（供快照/预算断言）
    meta: dict                           # {token_est:int, budget:dict, dropped:list} 等

def render_view(store, config, *, role: str, event_ids: list[int],
                cold_start: bool = True, instruction: str = "") -> RenderedView:
    """§6.1-§6.4 四层组装（单线程 mock 语境；热续增量 §6.5 是 M3，本函数只走冷启动全量路径）。
    只从 to 渲染 @（§3.1/§16.1，方向恒为 信封→显示）。"""

def render_event_third_person(event: dict, viewer_role: str) -> str:
    """焦点窗单事件第三人称渲染：'#12 [tester→@backend] (defect): {body摘要}'。
    统一第三人称角色标签，禁止第一人称原文流（§16.7）。"""

def estimate_tokens(text: str) -> int:
    """§17 开放决策：全系统统一的 token 近似（字符系数法）。唯一实现，供预算裁剪。"""
```

**五段结构（§6.1，位置效应两端强中间弱，顺序固定）**：
1. `system` 系统层（§6.2，冷启动全文）：角色身份（读 config 的 role.prompt 文件内容）+ 权限申报原文
   `可写: {write_scope}；可用工具: {tools}；越权写入会被系统整体拒收` + 身份声明
   `以下历史中标注 [{role}] 的发言是你自己说过的话` + 输出格式（信封 json 要求 + 最小示例）
   + 幂等指令（`输入事件均带 # 编号；若某编号你已处理过，直接重发当次信封`）。
2. `blackboard` 黑板层：board.md 全文（`store.board_state` 或读 board.md）。
3. `background` 背景层：非焦点 B/C 类一行摘要 `#3 [pm→@backend,@frontend] review: PRD v1 发起评审`；
   D 类超 chat_ttl（默认 10 事件）丢弃。
4. `focus` 焦点窗：满足 `(to∋role)∨(from==role)∨(re∩role的事件≠∅)` 的 **B 类**事件，全文、事件号升序、
   第三人称渲染。
5. `instruction` 指令尾（§6.2）：`你是 {role}。现在只针对 #{ids} 回应：{instruction}`。热续必发（M3）。

**保留策略应用（§3.2 A/B/C/D）**：A→黑板层（已投影）；B→相关入焦点窗全文、否则背景层一行；
C→背景层一行；D→超 chat_ttl 丢弃。

**预算（§6.3）**：上限 = 该 role 绑定 adapter 的 `caps.context_window`（经 config）。分配：焦点窗≥50%、
黑板≤20%、背景≤20%，其余归系统层+指令尾。超预算压缩顺序：**先丢背景最旧摘要 → 再截断焦点窗最旧
事件正文（保首尾各一段）**。压缩动作记入 `meta.dropped`（供测试断言顺序）。

**§6.4 冷启动附加段（仅 CLI 型）**：worktree 现场摘要（git log/status/diff）插在黑板层后。
M1 mock 角色**无 worktree → 跳过**（真实 CLI 属 M2）；实现保留分支但对 mock no-op。

---

## 2. 看门狗三级（§5.3）—— owner T3(M1)，接入 `orch.scheduler`

定时依据一律落盘绝对时间戳（`deadline_ts`），**禁止内存倒计时/sleep 当看门狗**（§16.2）。
环路与轮数每次**从日志现数，不落盘**（§9.1 可推导）。

```python
def check_watchdogs(store, config, *, now: float | None = None) -> list[dict]:
    """核心环每轮调用。now 可注入假时钟（测试用，默认 time.time()）。返回触发动作列表。
    级别1 单次调用超时：now > deadline_ts → attempt+1（M1 仅计量）。
      完整语义"kill 子进程 / 重试1次 / 再败 failed 转 moderator"属 **M2 真实后端**（mock 无子进程对象）；
      M1 mock 同步返回不残留 dispatching，故级别1 在活循环中实际为 no-op，仅崩溃恢复注入假时钟的
      测试路径会计 attempt。**不得据此误认为 M1 已具备超时 kill/重试**。
    级别2 互@环路：同一有序对 (A→B) 的 defect 事件数 ≥ loop_limit(默认3) → 自动 gate_request 升级 human。
    级别3 全局轮数：线程事件总数 ≥ max_rounds(默认100) → 自动 gate_request。
    """
```

看门狗触发的 gate_request（`to=[human]`）复用 M0 门禁机制 → 线程 suspended。

---

## 3. 终止清单完善（§5.4）—— owner T3(M1)

- 终止清单汇总：黑板契约 + 全部 artifacts + 分支列表 + 会话台账 → 一条 system 总结事件。
- **评审建议②**：终止总结 system 事件**不生成派发行**（落盘时排除，或建后即 done），保持派发表整洁。
  （M0 遗留一行惰性 pending，M1 修正。）terminate 本身不生成派发行（M0 已实现，§5.4）。

---

## 4. 视图接入调度层（§6 落地）—— owner T3(M1)

M0 `core.py` 用最小占位 view；M1 改为调用 `render.render_view` 组装完整视图后 `adapter.invoke`：
- `view = render_view(store, config, role=target, event_ids=ids, cold_start=<该 role sessions.gen==0 或无 sid>)`
- `adapter.invoke(view, sess)`；mock 仍按 `view['event_ids']` 查表（不依赖 text），但 text 现为完整渲染。
- 落 invoke log 用 `view['text']` + 输出原文（§14）。
- 保持 M0 已有的聚合（同 target 同批一次 invoke）与串行；**不新增**并行（M3）。

---

## 5. 提示词与 config（§11.2/11.3）—— owner T4(M1)

`prompts/*.md`（§17 文案，**必须**遵守结构）：
- `prompts/moderator.md`（§11.2）：只做一件事——读黑板与最近事件，输出信封 `to=下一个该谁`、
  body 一句理由。**禁止**产出任何实体工作（代码/文档/评审意见）。
- `prompts/{pm,backend,frontend,tester}.md`（§11.3）：三段固定结构——职责边界 / 交接产物与格式 /
  何时向谁交接。

**config 结构（M1 扩展 M0 的 config dict；§11.1 子集）**：
```
config = {
  thread_defaults: {max_rounds:100, loop_limit:3, chat_ttl:10},
  gate_ops: {...},                       # M0 已有
  adapters: {<name>: {kind, context_window, timeout_s, ...}},   # render 预算取 context_window
  roles: {<role>: {adapter:<name>, can_decide:bool, write_scope:[...], tools:[...],
                   prompt:"prompts/<role>.md"}},
}
```
render 读 `config.roles[role].prompt` 文件内容组系统层；预算上限取 `config.adapters[roles[role].adapter].context_window`。

---

## 6. 给测试的约定（T1 M1，测试先行见红）
- **视图快照**：`render_view` 的 `sections` 各段做稳定快照；断言五段顺序（system→blackboard→background→focus→instruction）。
- **第三人称**：焦点窗渲染断言有 `[from→@to] (type)` 标签、无第一人称"我"原文流（§16.7）。
- **保留策略**：A→黑板、B相关→焦点、B不相关→背景、C→背景、D超 chat_ttl→丢弃，各一用例。
- **预算压缩**：构造超 context_window 场景，断言 `meta.dropped` 顺序（背景最旧先丢→焦点最旧截断保首尾）。
- **看门狗**：构造 (A→B) defect×3 → gate_request+suspended；事件×100 → gate_request；注入 `now` 假时钟测单次超时。
- **终止**：终止总结 system 事件**不**产生 pending 派发行；终止清单含契约/artifacts/分支/会话。
- 顶层只 `import orch.render / orch.scheduler`，具体符号函数体内引用（未实现→运行时红）。

---

## 7. M1 里程碑简化清单（记 IMPLEMENTATION_NOTES.md）
1. token 估算：字符系数近似（§17，`estimate_tokens` 唯一实现），全系统一致。
2. §6.4 worktree 现场段对 mock 角色 no-op（真实 CLI 属 M2）。
3. 热续增量（§6.5）不实现，render 恒走冷启动全量（M3）。
4. moderator/角色提示词文案属 §17，遵守 §11.2/§11.3 结构即可。
