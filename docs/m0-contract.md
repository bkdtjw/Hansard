# M0 冻结接口契约（跨任务卡边界）

> 本文件是 **Lead 设计规格**（非实现代码）。冻结 M0 各模块**对外暴露的符号、签名、
> 返回约定与语义边界**，供 T1（写测试）与 T2–T5（写实现）共享，保证测试先行与实现对齐。
> **模块内部逻辑不在此约束**，由各 worker 依 spec 自主实现。
> 与 spec 冲突时以 spec 为准；本文件仅在 spec 未细化跨卡边界处做工程约定。
> 冻结范围 = 下列**公开签名**；未列出的辅助函数/私有实现由 worker 自定。

---

## 0. 通则

### 0.1 信封表示
信封在内存中用 **`dict`**（spec §3 的信封即结构化对象，附录A 是其作者字段 JSON Schema，天然对 dict）。键名一律用 spec §3.1 的协议名：

- 作者字段（模型/human 提供，经附录A 校验）：`to`(list[str]) / `type`(str) / `body`(str) /
  `artifacts`(list[str]) / `corr`(str|None) / `blackboard_ops`(list[dict]|None)
- 系统字段（编排器权威赋值，**禁止信任模型同名输出**，§3.1）：`id`(int) / `thread_id`(str) /
  `ts`(float) / `from`(str) / `re`(list[int]) / `meta`(dict)

> 存储层负责 `from` ↔ DDL 列 `sender` 的映射（`from` 是 SQL/Python 关键字，§4.3）。
> dict 里键名保持 `"from"`（字符串键合法）。

### 0.2 event_id / ts / re 的来源
- `id`：由 sqlite `AUTOINCREMENT` 分配（§3.1、§4.3），`append_event` 落盘后返回。
- `ts`：float UTC；调用方未提供时由存储层取 `time.time()`。
- `re`：编排器按派发批次赋值（§3.1）；首个事件（human 开工）`re=[]`；回复事件 `re = 本批 event_ids`。

---

## 1. `orch.protocol`（spec §3、附录A）—— owner: T2

```python
AUTHOR_SCHEMA: dict                      # 附录A 原样 JSON Schema（draft-07）

def validate_author_fields(obj: dict) -> list[str]:
    """用 jsonschema 校验作者字段。返回错误消息列表；空列表 = 合法。
    仅校验作者字段（附录A）；系统字段不在此校验。"""

TYPE_RETENTION: dict[str, str]           # type -> 保留策略 "A"/"B"/"C"/"D"（§3.2 表）

def allowed_sender(env_type: str, sender: str, *, can_decide: bool) -> bool:
    """§3.2 发送者约束。decision: 仅 can_decide 角色或 human；
    gate_decision: 仅 human；system: 仅编排器(sender=='system')；
    terminate: 仅 moderator/tester/human；其余 type: 任意。"""

def can_apply_blackboard_ops(env_type: str, *, sender_can_decide: bool) -> bool:
    """§3.3 门槛：type ∈ {decision, acceptance, gate_decision} 且 sender 具 can_decide。"""
```

违规处理语义（调度层据此执行，§3.2/§3.3）：发送者约束违规 → 信封降级为 `report` 落盘 + 追加
一条 `system` 审计事件；bb_ops 门槛不满足 → 忽略 ops + 追加 `system` 审计事件。

---

## 2. `orch.store`（spec §4）—— owner: T3

`Store` 绑定**一个线程目录**（§4.1：一线程一目录一 db）。

```python
class Store:
    def __init__(self, thread_dir: str | Path): ...
        # 建目录：<thread_dir>/{events.db, blackboard/, logs/}
        # events.db 用 WAL；建齐 §4.3 全部 DDL（events/dispatches/sessions/jobs/thread_meta/metrics）

    # —— §4.4 事务(1)：事件追加 + 派发行生成（单事务）——
    def append_event(self, *, sender: str, type: str, body: str,
                     to: list[str] | None = None, re: list[int] | None = None,
                     corr: str | None = None, artifacts: list[str] | None = None,
                     blackboard_ops: list[dict] | None = None,
                     meta: dict | None = None, ts: float | None = None) -> int:
        """插入 events（id 自增）+ 为每个 to 目标插 dispatches(pending)。
        to 为空 → 生成 target='moderator' 的派发行（§4.4(1) 兜底落盘）。
        terminate 型**不生成派发行**（§5.4，信号非待办）。返回 event_id。"""

    def pending_dispatches(self) -> list[dict]:
        """全部 status='pending' 的派发行，按 event_id 升序（§5.1）。
        每行至少含 event_id/target/status/deadline_ts/attempts。"""

    def dispatches_snapshot(self) -> list[dict]:
        """（M5 后追加，只读原语）全部派发行**不过滤 status**，按 event_id/target 升序。
        每行含 event_id/target/status/deadline_ts/attempts。仅供展示层（控制台
        /status 全五态 + deadline_ts）；**禁改** pending_dispatches 的 SQL 与语义。"""

    def mark_dispatching(self, event_id: int, target: str, deadline_ts: float) -> None:
        """§4.4 事务(2)：status→dispatching + 写绝对截止时间戳。"""

    # —— §4.4 事务(5)：回复落盘 + 标 done + 会话表（单事务，消除崩溃窗口）——
    def reply_and_done(self, *, done_event_id: int, done_target: str,
                       reply: dict, session: dict | None = None) -> int:
        """单事务内：① 回复信封落盘（reply 含系统字段 from/re 已由调度层赋好）
        ② (done_event_id, done_target) status→done
        ③ 若 session 非空则 upsert sessions(role,backend,sid,last_evt,gen)。
        返回回复事件 id。"""

    def mark_failed(self, event_id: int, target: str) -> None: ...
    def mark_gate_wait(self, event_id: int, target: str) -> None: ...   # §10
    def bump_attempt(self, event_id: int, target: str) -> int: ...       # 看门狗 attempts+1，返回新值
    def set_pending(self, event_id: int, target: str) -> None: ...       # §9.1(c) 重派发

    def events(self) -> list[dict]:  ...       # 全部事件，id 升序，dict 形态（键见 §0.1）
    def get_meta(self, key: str) -> str | None: ...
    def set_meta(self, key: str, value: str) -> None: ...   # thread_meta：status/suspend_corr/...

    # —— jobs（§5.2 长作业登记）——
    def register_job(self, *, corr: str, kind: str, cmd: str,
                     callback_to: str, started_evt: int) -> None: ...
    def set_job_status(self, corr: str, status: str) -> None: ...

    # —— metrics（§13 采集点，M0 至少提供落点）——
    def record_metric(self, key: str, value: float, extra: str = "") -> None: ...

    # —— invoke 原文审计（§14：一等公民）——
    def write_invoke_log(self, *, event_ids: list[int], role: str,
                         view_text: str, output_text: str) -> None:
        """落 <thread_dir>/logs/ 一个文件，文件名含事件号与角色。"""


# —— 黑板：决策类事件的投影（§4.6）——
def apply_blackboard_ops(store: Store, ops: list[dict], source_event_id: int) -> None:
    """按 §3.3 三种 op 更新 blackboard/state.json，随后重渲染 board.md。
    state.json 结构：{contracts:{name:{version,path,frozen_at}}, decisions:[{evt,text}], tasks:{key:status}}。
    调用方**须已判过** can_apply_blackboard_ops 门槛（本函数不再判权限）。"""

def rebuild_blackboard(store: Store) -> None:
    """清空 state 后按 id 升序重放全部满足 §3.3 门槛的 A 类事件的 bb_ops。
    结果**必须**与增量维护逐字段一致（§4.6，T1 以单测保证）。
    恢复时黑板文件缺失/损坏即调用它（§9.1）。"""

def board_state(store: Store) -> dict:   # 读 state.json 供断言
    ...
# （M5 后追加，签名不变）board_state 亦是**展示层**读权威黑板的唯一入口：控制台
# GET /api/threads/{id}/board 经它投影三节，只读、不写盘；展示层禁止据事件的
# bb_ops 自行重投影（落库 ≠ 生效，被 §3.3 门槛拒绝的 ops 照样在事件里）。

def board_state_checked(store: Store) -> tuple[dict, str | None]:
    """（M5 后追加，只读原语）同 board_state 的键形状，另给"读不出来"的一句人话：
    文件不存在 → (空结构, None)；JSON 坏 / 顶层非对象 / 读不动 → (空结构, 人话)。
    仅供**展示层**（/board 的 board_error）——它不重建只渲染，损坏必须说出来；
    调度/恢复路径仍用宽松的 board_state（§9.1 缺失与损坏同解为 rebuild）。
    **禁改** board_state / Store._read_state 的宽松语义与签名。"""
```

> board.md 的具体**渲染格式**属 spec §17 开放决策点，由 T3 自定并记 NOTES（不影响本契约）。

---

## 3. `orch.adapters`（spec §7.1、§7.4）—— owner: T4

```python
from typing import TypedDict

class Caps(TypedDict):                    # §7.1 原样
    context_window: int
    tools: list[str]
    write_scope: list[str]
    cost_tier: str
    supports_resume: bool
    timeout_s: int
    max_concurrent: int

class MockAdapter:                        # §7.4 脚本化确定性 agent
    caps: Caps
    def __init__(self, *, role: str, script: dict, ledger_path: str | Path,
                 caps: Caps | None = None): ...
        # script: {触发事件号(int): 预置作者字段信封(dict)}，来自附录B fixture

    def invoke(self, view: "dict", sess: dict | None
              ) -> tuple[dict, dict | None]:
        """按 (role, 事件号) 查 script 返回**预置作者字段信封**（只含作者字段）。
        事件号取自 view['event_ids'] 的最大值（= 本批触发号）。
        副作用：每处理一个事件号，**追加一行**到 ledger 文件（供 §9.4 exactly-once 校验）。
        ledger 行格式：'{role}:{event_id}\\n'。返回 (env_dict, sess)。"""
```

> M0 的 `view` 是**最小占位** dict：`{"role": str, "event_ids": list[int], "events": list[dict], "board": str}`。
> mock 只用 `role` + `event_ids`；完整四层视图渲染（§6）是 **M1**，M0 不做。

---

## 4. `orch.scheduler`（spec §5.1、§5.2、§9.1）—— owner: T5

```python
def run_thread(store: Store, config: dict, adapters: dict[str, "MockAdapter"]) -> None:
    """§5.1 核心循环的**单线程串行版**。跑到 thread status ∈ {suspended, terminated} 返回。
    每轮：pending 按 event_id 升序 → group_by(target) 聚合（同目标同批一次 invoke，re=全部 event_ids）
    → 逐组串行处理（M0 不做写域并行，§5.1 的并行是 M3）：
      · target=='human' → mark_gate_wait + thread_meta.status='suspended' 落盘后**返回**（§10，可整体停机）
      · 否则：mark_dispatching(+deadline) → 组装最小 view → adapter.invoke
              → schema 校验回复（§附录A；失败按 §5.1 原地重调一次，两次失败 failed+转 moderator）
              → [reply_and_done 事务] 赋系统字段 from=target, re=event_ids
              → 若回复满足 §3.3 门槛则 apply_blackboard_ops
              → verify 钩子（§8.3：回复为 acceptance 时，编排器亲自执行 role 的 verify.cmd，
                 写 meta.verify.exit_code；!=0 则降级为 report）
              → 终止/门禁/看门狗检查
    mock 角色无 worktree → 跳过 autocommit 与越权审计（§4.5 无 worktree 角色跳过；真实 CLI 是 M2）。"""

def recover(store: Store, config: dict) -> None:
    """§9.1 恢复算法（启动时对线程机械执行，**禁止猜测**，只查表与数日志）：
      · 黑板缺失/损坏 → rebuild_blackboard
      · thread status=suspended → 保持挂起，gate_wait 行不动，只等 gate_decision（直接返回）
      · 对每个 status='dispatching' 的 (E_n, T):
          a) 存在 sender=T 且 n ∈ re 的回复 → 补标 done（纵深防御）
          b) now > deadline_ts        → 看门狗路径（bump_attempt 计一次）
          c) 其余                      → set_pending 重派发
      · pending 行不处理（主循环接手）；计数器由日志重数。"""
```

### 4.1 M0 需覆盖的调度子能力（附录B E1–E19 驱动）
- **兜底路由**（§5.2）：`to` 空 → moderator（落盘已在 `append_event` 处理，调度层正常 invoke moderator）。
- **聚合**（§5.1）：同批同 target 多行 → 一次 invoke，`re` 含全部；记 `batch_size`（§13）。
- **门禁 + 系统执行器**（§5.5、§10）：`gate_request(to=[human])` → suspended；human `approve`（E2E 直接调
  API 模拟）→ `gate_decision` 事件 + gate_wait 行 done + thread resume；approve 关联特权操作 →
  **系统执行器**按 `config['gate_ops'][op]` 命令模板 subprocess 执行 → 结果作为 `system` 事件入队。
- **长作业回调**（§5.2）：`run_ci` 登记 `jobs(corr, callback_to)` → 执行 → 插 `system` 事件 `to=[callback_to]`,`corr` 回填。
- **终止清单**（§5.4）：`terminate` 落盘不生成派发行 → 汇总产物（黑板契约 + artifacts + 分支 + 会话台账）
  生成一条 `system` 总结事件 → thread status='terminated' → 拒绝新派发。

---

## 5. 线程目录布局（M0 最小，§4.1）
```
<thread_dir>/
  events.db          # sqlite WAL：events/dispatches/sessions/jobs/thread_meta/metrics
  blackboard/
    state.json       # 结构化投影
    board.md         # 渲染稿（格式属 §17，T3 自定）
  logs/              # 每次 invoke 的输入/输出原文（§14）
```

---

## 6. M0 里程碑简化清单（自行决策，记 IMPLEMENTATION_NOTES.md）
下列为"单线程串行 + mock"里程碑下的**有意退化**，控制流与 spec 一致，后续里程碑补全：
1. **视图组装**：M0 用最小占位 view；四层结构/第三人称渲染/尾部重锚定是 **M1**（§6）。
2. **长作业异步**：M0 将 `run_ci` 等异步长作业**退化为同步执行 + 立即回调**（单线程无并发）；
   真 asyncio 异步是 **M3**（§5.2/§14）。控制流（jobs 登记→执行→system 回调）保持一致。
3. **worktree/autocommit/越权审计**：mock 角色无 worktree，M0 全跳过；真实 CLI 三件套是 **M2**（§4.5/§8.2）。
4. **看门狗高级级别**：M0 恢复算法含"单次调用超时"路径（§9.1 b）；互@环路/全局轮数的**主动触发**是 **M1**（§5.3）。
5. **gate_ops 命令**：E2E fixture 用跨平台无害命令（`python -c ...`）验证控制流，不真 merge 目标仓库。

---

## 7. 给 T1（测试）的对接约定
- 测试**顶层**只 `import orch.protocol / orch.store / orch.adapters / orch.scheduler`（包级，T0 已保证可导入）；
  具体函数/类在**测试函数体内**引用，使未实现符号表现为运行时红（fail/error），而非 collection 中断。
- 断言只依赖本契约列出的**公开签名与返回约定**；不依赖任何未冻结的内部实现细节。
- 附录B fixture（`tests/fixtures/like_feature.*`）由 T1 建立最小可用版；T6 负责最终 E2E 装配跑绿。

---

## 8. T1 反馈裁决（Lead 冻结，补充 §1–§4）

T1（测试先行）诚实上报 5 处跨卡边界缺口（均属工程约定缺口，非 spec 内部矛盾）。Lead 裁决并纳入冻结契约：

1. **human approve 入口**（缺口①②）→ 新增 `orch.scheduler`（owner **T5**）：
   ```python
   def apply_gate_decision(store, config, adapters, *, corr: str,
                           approve: bool, sender: str = "human") -> None:
       """§10 `orch approve|reject` 的编排器入口。
       ① 产生 gate_decision 事件（from=sender, corr 回填, to=[原 gate_request.sender]）
       ② 把对应 gate_wait 派发行标 done → thread status='running'（resume）
       ③ approve 且该 gate 关联特权操作 → 系统执行器按 config['gate_ops'] 执行，
          结果作为 system 事件入队；run_ci 类经 jobs 登记（M0 同步退化）后回调
          system 事件 to=[callback_to]、corr 回填（§5.2/§5.5）。
       reject：只入 gate_decision 并 resume，不执行特权操作。"""
   ```
   T6 装配时用它替换 E2E 中的 store 原语/裸 sqlite 注入。

2. **ledger 父目录自动创建**（缺口③）→ `MockAdapter` 写 ledger 前
   `Path(ledger_path).parent.mkdir(parents=True, exist_ok=True)`。owner **T4**。

3. **Store 线程目录属性**（缺口④）→ 冻结公开属性 `Store.thread_dir: Path`
   （构造时绑定的线程目录）。owner **T3**。

4. **通用标 done 原语**（缺口⑤）→ 新增 `Store.mark_done(event_id: int, target: str) -> None`
   （把任一派发行状态置 done，供 apply_gate_decision 标 gate_wait 行）。owner **T3**。

裁决效果：E2E 不再需要直连 sqlite hack；相关断言在 T6 装配阶段切换到上述冻结接口。
