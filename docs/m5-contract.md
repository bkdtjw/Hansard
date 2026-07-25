# M5 施工契约（docs/m5-contract.md）

冻结跨任务卡边界的模块/符号/签名/行为约定（设计规格，非实现代码）。
依据：spec §5.6（可用性与降级路由）、§7.6（错误分类）、§11.1（config 字段）、
§12（CLI 三命令与控制台按钮）、§13（两项指标）、§15 M5 验收标准。
T1 据此写验收测试；T2–T6 据此实现。内部逻辑由各 worker 依 spec 自主实现。
凡契约与 spec 冲突，以 spec 为准并停下升级（禁止擅自改契约）。

## 1. 状态模块（T2，新文件 src/orch/adapters/state.py）

```python
DEFAULT_TRIP_AFTER = 3
DEFAULT_UNAVAILABLE_PATTERNS: tuple[str, ...] = (
    "quota", "insufficient", "rate limit", "429", "额度")   # §17 裁决的默认清单

class AdapterStateError(Exception): ...   # 状态文件损坏（启动报错，禁止猜测）

class AdapterAvailability:
    path: Path
    @classmethod
    def load(cls, path) -> "AdapterAvailability"
        # 文件缺失 → 全部 enabled；JSON 损坏 → raise AdapterStateError
    def reload(self) -> None                      # 每轮调度前重读（§5.6.1）
    def is_enabled(self, name: str) -> bool       # 未记录的名字 = enabled
    def disable(self, name, *, reason: str, by: str) -> None   # by ∈ {"human","auto"}；落盘
    def enable(self, name) -> None                # 置 enabled + fail_streak 清零；落盘
    def record_failure(self, name, *, trip_after: int, reason: str) -> bool
        # streak += 1；达阈值 → 自动 disable(by="auto")；返回"本次是否跳闸"；落盘
    def record_success(self, name) -> None        # streak 归零（原为 0 则不写盘）
    def snapshot(self) -> dict[str, dict]
        # {name: {"status","reason","by","ts","fail_streak"}}，供 CLI/web/status 投影

def state_path_for(config_path) -> Path           # = config.yaml 同目录 / "adapter_state.json"

def resolve_effective_adapter(role: str, roles_cfg, availability) -> str | None
    # [主绑定] + fallback 中首个 enabled；全部不可用 → None（§5.6.2）

def validate_availability_config(cfg: dict) -> list[str]
    # 返回错误清单（空 = 合法）：fallback 项必须是已声明 adapter；
    # tools 或 write_scope 非空的角色，主绑定与 fallback 全部项必须 kind=cli（§11.1）
```

- 落盘：整文件 JSON 原子替换（临时文件 + os.replace）。字段编排 §17，但 snapshot
  五键名冻结如上。ts = 最近一次状态变更的 epoch 秒。
- 全局单文件、跨线程共享；每进程持一个实例，reload 兜底外部（CLI/控制台）改动。

## 2. 错误分类（T4，src/orch/adapters/__init__.py）

```python
class AdapterUnavailableError(Exception):
    adapter_name: str      # 触发的 adapter 配置名
    detail: str            # 命中的原始报错摘要
```

- CliAdapter / ApiAdapter：**传输级失败**（超时/进程失败/无输出）时，把可得的
  stderr/退出信息文本与本 adapter 的 unavailable_patterns（缺省 DEFAULT_*）做
  大小写不敏感子串匹配；命中 → raise AdapterUnavailableError；未命中 → 既有
  失败路径不变。schema 层面的非法信封不属传输级，维持 §5.1 原地重调，不分类。
- 配置键（§11.1）：adapter 级可选 `unavailable_patterns: list[str]`、`trip_after: int`。
- MockAdapter 增可选参数：`unavailable_after: int | None = None`、
  `unavailable_text: str = "quota exceeded (mock)"`——第 unavailable_after 次
  invoke 起（含该次）恒抛 AdapterUnavailableError，此前行为不变。ledger 语义不变。

## 3. 调度接线（T3，src/orch/scheduler/core.py 与 async_core.py 两条环对等）

- 派发组标 dispatching 之前：availability.reload() 后 resolve_effective_adapter。
  - None → 该组全部行保持 pending、不 invoke、不加 attempts；首次进入阻塞态
    追加 system 通告事件（见 §4 事件约定）；**禁止**忙等（等待条件按 §5.1 扩展，
    轮询间隔 §17，默认 ≤2s 可配）。
  - effective ≠ 主绑定 → 切换审计事件（首次，见 §4）+ metrics(fallback_switch)。
  - effective ≠ sessions.backend → Store.upsert_session 置 sid 空、gen+1、
    backend=effective；该组各行 attempts 归零（新增 Store 原语，见 §5）。
  - invoke 用"adapter 名 → 实例"映射按 effective 取实例（现状若按角色直取，改经名解析）。
- 异常消费：
  - AdapterUnavailableError → availability.disable(name, by="auto",
    reason=detail 摘要) + 跳闸审计事件 + metrics(adapter_trip, trigger="pattern")；
    该行回 pending、attempts 不变；**同一轮内立即重解析该组**（spec §5.6.3
    "立即按 §5.6.2 重解析"字面）：解析出新绑定 → 当场换绑重派该组再续后续组
    （保持组间最小事件号先后）；None → note_blocked 跳过。
    （R2 修订 2026-07-25：初稿"本轮 continue 下轮接手"与 spec 冲突，已废——
    kill 时序错开双跳闸时会让下游组先产出回复，破坏 §9.4 逐字节一致，
    T6 混沌跨 seed 取证。）
  - 其他传输级失败（超时/进程失败/解析失败）→ 既有 attempts 语义不变，
    叠加 availability.record_failure(trip_after=该 adapter 配置或缺省)；
    返回 True 时补跳闸审计事件 + metrics(adapter_trip, trigger="streak")。
  - 成功 invoke → availability.record_success。
- schema 校验失败：不触碰 availability（§5.6.3）。

## 4. 事件与指标约定（T3 产、T1 断言、T5 展示）

- 三种 system 事件均：sender=orchestrator 系统身份（沿既有 system 事件惯例）、
  **不生成派发行**（比照 terminate）、body 为人话中文、meta_json 含机器字段：
  - 切换：meta.kind="fallback_switch"，meta 另含 role/primary/effective/reason
  - 阻塞：meta.kind="adapter_blocked"，meta 含 role/primary
  - 跳闸：meta.kind="adapter_trip"，meta 含 adapter/trigger("pattern"|"streak")/detail
- "首次才记"判定：现查该线程日志中最近一条同 kind 且同 role（或同 adapter）事件
  是否已表达同一状态；**禁止**内存驻留去重标志。
- metrics 表键名冻结：`fallback_switch`（extra 含 role/from/to）、
  `adapter_trip`（extra 含 adapter/trigger）。`orch metrics` 汇总两行：
  降级切换次数、自动跳闸次数（可按分组展开）。
- 频次口径（R1 修订 2026-07-25）：`fallback_switch` **指标**逐次降级派发各记
  一条（§13 字面"每次…派发记一条"）；切换**审计事件**保持首次一条（§5.6.2）。
  二者有意不同频，禁止共用去重判定。

## 5. Store 原语（T3，src/orch/store/__init__.py 仅新增，禁改既有）

```python
def reset_attempts(self, event_id: int, target: str) -> None   # UPDATE dispatches SET attempts=0
```
（若既有等价原语，复用并在汇报中说明，不重复造。）

## 6. CLI（T5，src/orch/cli/main.py）

- `orch adapters [--config PATH]`：表格输出 snapshot ∪ config 声明的全部 adapter
  （未记录者显示 enabled）；列：name/状态(✅|⛔)/reason/by/ts/fail_streak。
- `orch adapter disable NAME [--reason TEXT] [--config PATH]`
- `orch adapter enable NAME [--config PATH]`
- 三命令 --config 缺省值沿 orch run 同一约定；状态文件位置 = state_path_for(config)。
- `orch status`：提供 --config 时，角色行追加生效绑定；主绑定 disabled 显示 ⛔ 与
  生效备胎；无可用者显著警示（含"无可用"字样）。

## 7. Web（T5，src/orch/web/）

- 端点（沿既有网关风格，JSON）：
  - GET  /api/adapters                     → {"adapters": [{name,status,reason,by,ts,fail_streak}, …]}
  - POST /api/adapters/enable  body {"name"}                → 200 + 新 snapshot
  - POST /api/adapters/disable body {"name","reason"?}      → 200 + 新 snapshot
  - 未知 name → 400。写路径 = 同一 AdapterAvailability 原子替换。
- 前端（index.html/app.js/styles.css）：适配器面板列出全部 adapter，每行
  状态徽章 + **enable/disable 开关按钮**（disable 可填 reason，玻璃感风格一致）；
  存在 disabled 时线程视图给出警示条；轮询节奏沿既有 D6 约定。

## 8. 混沌与 E2E（T6，src/orch/chaos/；测试 T1 先行）

- MockAdapter 双实例扮主/备（同一脚本表，ledger 各自独立文件或共文件均可，
  但对账口径在测试内自洽）；场景：主 adapter 于第 k 次 invoke 起抛额度错误 →
  自动跳闸 + fallback 接手 → 跑完附录 B 至 terminated。
- 终态比较沿 R-T1 口径：mock ledger 字节 + blackboard/state.json 与"不中断基准"
  逐字节一致（事件因号偏移允许类型级比较）。
- kill -9 扩展：在"跳闸落盘前后/切换审计前后/换绑重派前后"间隙注入 + 纯随机，
  ≥ 20 轮 100%（§9.4 不变量扩展）。opt-in 门槛沿 --chaos-50 惯例另立标志
  （命名 T1 自决并记录）。

## 9. 里程碑边界

- 不做：自动恢复/冷却重试（spec 明文禁止）；额度数字查询；真实后端计费联跑
  （验收全 mock/fake；真实后端冒烟属交付后人工陪跑项）。
- 全程零 DDL 变更、零信封 schema 变更、零 §4.4 落盘顺序变更——任何卡若发现
  必须动这三者，立即停卡升级，禁止先斩后奏。
