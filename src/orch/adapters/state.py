"""适配器可用性状态：全局单文件、原子替换写、跨线程共享（spec §5.6.1/§5.6.2/§11.1）。

本模块是 M5 的事实底座，只做三件事，**不做**任何调度决策：
  1. 可用性状态的落盘与读回（``AdapterAvailability``——唯一写路径 = 原子替换）；
  2. 生效绑定解析（``resolve_effective_adapter``——纯函数、不落盘，§5.6.2）；
  3. 装载期配置校验（``validate_availability_config``——纯函数，§11.1）。

分层铁律（spec §2）：这里没有角色逻辑；也没有任何形式的自动恢复 / 冷却重试——
§5.6.3 明文禁止，从 disabled 回到 enabled **只有**人工 ``enable()`` 一条路。

状态文件语义（§5.6.1）：
  · 不进线程 db——额度是供应商级事实，跨线程共享；
  · 文件缺失 → 视为全部 enabled（冷启动默认），且 load **不**顺手创建文件；
  · 文件损坏 → 抛 ``AdapterStateError``，**禁止**猜测（与 §9 同一哲学）；
  · 写者有二（CLI／控制台 与 调度器），最后写入者胜，竞态最坏后果是一次多余的
    人工重设；进程内以锁串行化"读-改-写"，进程间靠 os.replace 的原子性。

冻结契约见 docs/m5-contract.md §1（签名 / 常量 / snapshot 五键名一字为准）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

# —— 契约 §1 冻结常量 —————————————————————————————————————————————
# 连续传输级失败达此阈值即跳闸（§5.6.3 第 2 条；adapter 级 trip_after 可覆盖）。
DEFAULT_TRIP_AFTER = 3
# 传输级报错文本的默认"不可用"特征清单（§5.6.3 第 1 条；大小写不敏感子串，§17 裁决）。
DEFAULT_UNAVAILABLE_PATTERNS: tuple[str, ...] = (
    "quota", "insufficient", "rate limit", "429", "额度",
)

# 状态文件名（§4.1 目录布局：与 config.yaml 同目录的全局单文件）。
STATE_FILENAME = "adapter_state.json"

# snapshot 五键名冻结（契约 §1）；落盘条目沿用同一组键（字段编排属 §17）。
_SNAPSHOT_KEYS: tuple[str, ...] = ("status", "reason", "by", "ts", "fail_streak")
_STATUSES: tuple[str, ...] = ("enabled", "disabled")
_BY_VALUES: tuple[str, ...] = ("human", "auto")
# 落盘格式版本（§17 字段编排自决；读回时不做兼容猜测，只作人工排障线索）。
_FILE_VERSION = 1


class AdapterStateError(Exception):
    """状态文件损坏 / 不可读（§5.6.1：启动报错，禁止猜测）。"""


# ——————————————————————————————————————————————————————————————
# 条目：{status, reason, by, ts, fail_streak}
#   ts = **最近一次状态变更**的 epoch 秒（契约 §1）；record_failure / record_success
#   只动 fail_streak 时不刷新 ts，仅当 enabled↔disabled 发生变更（含记录建立）才刷新。
# ——————————————————————————————————————————————————————————————

def _new_entry() -> dict:
    """新建条目：默认 enabled（§5.6.1 冷启动默认），by 空（尚无人设置过）。"""
    return {
        "status": "enabled",
        "reason": "",
        "by": "",
        "ts": time.time(),
        "fail_streak": 0,
    }


def _normalize_entry(path: Path, name: str, value: object) -> dict:
    """把落盘条目规范化为五键条目；任何无法如实解读的字段一律报错（禁止猜测）。"""
    if not isinstance(value, dict):
        raise AdapterStateError(
            f"适配器状态文件损坏（§5.6.1）：{path} 中 {name!r} 的条目不是对象"
        )
    status = value.get("status", "enabled")
    if status not in _STATUSES:
        raise AdapterStateError(
            f"适配器状态文件损坏（§5.6.1）：{path} 中 {name!r} 的 status={status!r}"
            f" 非法（只允许 {list(_STATUSES)}）"
        )
    try:
        ts = float(value.get("ts") or 0.0)
        fail_streak = int(value.get("fail_streak") or 0)
    except (TypeError, ValueError) as exc:
        raise AdapterStateError(
            f"适配器状态文件损坏（§5.6.1）：{path} 中 {name!r} 的 ts/fail_streak 非数值"
        ) from exc
    return {
        "status": str(status),
        "reason": str(value.get("reason") or ""),
        "by": str(value.get("by") or ""),
        "ts": ts,
        "fail_streak": fail_streak,
    }


def _read_entries(path: Path) -> dict[str, dict]:
    """读回状态文件：缺失 → 空表（= 全部 enabled）；损坏 → AdapterStateError。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # json.JSONDecodeError 是 ValueError 子类
        raise AdapterStateError(
            f"适配器状态文件无法读取/解析（§5.6.1 禁止猜测）：{path}（{exc}）"
        ) from exc
    if not isinstance(raw, dict):
        raise AdapterStateError(
            f"适配器状态文件损坏（§5.6.1）：{path} 顶层不是对象"
        )
    adapters = raw.get("adapters")
    if not isinstance(adapters, dict):
        raise AdapterStateError(
            f"适配器状态文件损坏（§5.6.1）：{path} 缺少 adapters 对象"
        )
    return {
        str(name): _normalize_entry(path, str(name), value)
        for name, value in adapters.items()
    }


class AdapterAvailability:
    """适配器可用性的进程内视图 + 唯一写路径（§5.6.1）。

    · 读：``reload()`` 每轮调度前重读——**禁止**只在启动时读一次；
    · 写：每次变更整文件 JSON 原子替换（临时文件 + os.replace）后立即可见于他进程；
    · 未记录的名字 = enabled（状态文件只记录被显式改过的 adapter）。
    """

    path: Path

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, dict] = {}
        # 同一进程内 CLI/web/调度多线程共用一个实例（契约 §1），"读-改-写"须串行。
        self._lock = threading.RLock()

    # —— 装载 / 重读 ————————————————————————————————————————————
    @classmethod
    def load(cls, path: str | Path) -> "AdapterAvailability":
        """从状态文件装载；文件缺失 → 全部 enabled，损坏 → AdapterStateError。"""
        av = cls(path)
        av.reload()
        return av

    def reload(self) -> None:
        """重读状态文件（§5.6.1：调度器每轮调度前必做，兜底 CLI/控制台的外部改动）。"""
        entries = _read_entries(self.path)
        with self._lock:
            self._entries = entries

    # —— 查询 ————————————————————————————————————————————————
    def is_enabled(self, name: str) -> bool:
        """未记录的名字视为 enabled（§5.6.1 冷启动默认）。"""
        with self._lock:
            entry = self._entries.get(str(name))
        return True if entry is None else entry["status"] == "enabled"

    def snapshot(self) -> dict[str, dict]:
        """{name: 五键条目} 的只读拷贝，供 CLI / web / status 投影（契约 §1/§6/§7）。

        只含**被记录过**的 adapter；调用方需按 config 声明补齐未记录者（显示 enabled）。
        """
        with self._lock:
            return {
                name: {key: entry[key] for key in _SNAPSHOT_KEYS}
                for name, entry in self._entries.items()
            }

    # —— 变更（均落盘）————————————————————————————————————————
    def disable(self, name: str, *, reason: str, by: str) -> None:
        """置 disabled 并落盘。by ∈ {"human","auto"}（人工停用 / 自动跳闸）。"""
        if by not in _BY_VALUES:
            raise ValueError(f"by 只允许 {list(_BY_VALUES)}，实得 {by!r}")
        with self._lock:
            entry = self._entry(str(name))
            entry["status"] = "disabled"
            entry["reason"] = str(reason or "")
            entry["by"] = by
            entry["ts"] = time.time()
            self._flush()

    def enable(self, name: str) -> None:
        """人工恢复：置 enabled + fail_streak 清零并落盘（§5.6.3 唯一恢复路径）。"""
        with self._lock:
            entry = self._entry(str(name))
            entry["status"] = "enabled"
            entry["reason"] = ""
            entry["by"] = "human"
            entry["ts"] = time.time()
            entry["fail_streak"] = 0
            self._flush()

    def record_failure(self, name: str, *, trip_after: int, reason: str) -> bool:
        """传输级失败 +1（§5.6.3 第 2 条）；达阈值则自动跳闸。返回"本次是否跳闸"。

        已 disabled 的 adapter 继续累计 streak 但不再"跳闸"（返回 False）——跳闸是
        一次状态**变更**，不是持续状态。schema 校验失败**不得**走这条路径（§5.6.3）。
        """
        with self._lock:
            entry = self._entry(str(name))
            entry["fail_streak"] = int(entry["fail_streak"]) + 1
            threshold = max(1, int(trip_after))
            tripped = entry["status"] == "enabled" and entry["fail_streak"] >= threshold
            if tripped:
                entry["status"] = "disabled"
                entry["reason"] = str(reason or "")
                entry["by"] = "auto"
                entry["ts"] = time.time()
            self._flush()
            return tripped

    def record_success(self, name: str) -> None:
        """成功 invoke → fail_streak 归零（§5.6.3）；原为 0（含从未记录）则不写盘。

        **不**改 status：成功不等于恢复，disabled 只能人工 enable（§5.6.3）。
        """
        with self._lock:
            entry = self._entries.get(str(name))
            if entry is None or int(entry["fail_streak"]) == 0:
                return
            entry["fail_streak"] = 0
            self._flush()

    # —— 内部 ————————————————————————————————————————————————
    def _entry(self, name: str) -> dict:
        """取（必要时建）该 adapter 的条目。调用方须已持锁。"""
        entry = self._entries.get(name)
        if entry is None:
            entry = _new_entry()
            self._entries[name] = entry
        return entry

    def _flush(self) -> None:
        """整文件 JSON 原子替换（§5.6.1：临时文件 + rename）。调用方须已持锁。

        目标文件因此**永远**是某一份完整快照：要么旧的、要么新的，绝无半截；
        临时文件与目标同目录（跨目录 rename 非原子），成功即被 os.replace 消耗，
        失败则就地删除，不留残迹。
        """
        payload = {
            "version": _FILE_VERSION,
            "adapters": {
                name: {key: entry[key] for key in _SNAPSHOT_KEYS}
                for name, entry in sorted(self._entries.items())
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def state_path_for(config_path: str | Path) -> Path:
    """状态文件路径 = config.yaml **同目录** / adapter_state.json（契约 §1、§4.1）。"""
    return Path(config_path).parent / STATE_FILENAME


def resolve_effective_adapter(
    role: str, roles_cfg: dict, availability: AdapterAvailability
) -> str | None:
    """生效绑定 = [主绑定] + fallback 中**首个** enabled 项；全不可用 → None（§5.6.2）。

    纯函数：只读配置与传入的可用性视图，**不读盘也不写盘**（"每次派发时现算，不落盘"）；
    重读时机由调用方掌握（调度器每轮 ``reload()``，§5.6.1）。
    主绑定缺省用角色名兜底，与 ``scheduler.core._adapter_name``（会话 backend 列）同一约定。
    """
    role_conf = (roles_cfg or {}).get(role) or {}
    primary = str(role_conf.get("adapter") or role)
    fallback = role_conf.get("fallback") or []          # §11.1：缺省 []（无备胎）
    chain = [primary]
    if isinstance(fallback, (list, tuple)):
        chain.extend(str(item) for item in fallback)
    for name in chain:
        if availability.is_enabled(name):
            return name
    return None


def validate_availability_config(cfg: dict) -> list[str]:
    """§11.1 装载期校验，返回错误清单（空 = 合法；调用方据此启动报错）。

    两条规则（§11.1 "可用性与降级字段"段）：
      1. fallback 项必须是**已声明**的 adapter；
      2. tools 或 write_scope 非空的角色，其**主绑定与全部 fallback 项**必须是
         cli 型——API 型不带工具循环（§7.3）。

    边界（本函数刻意不管的）：主绑定是否已声明。§11.1 只要求 fallback 项已声明，
    且主绑定缺省用角色名兜底（见 ``resolve_effective_adapter``）；kind 无从判定时
    跳过规则 2，避免把"未声明"误报成"型别不符"。
    """
    errors: list[str] = []
    raw_adapters = cfg.get("adapters") if isinstance(cfg, dict) else None
    raw_roles = cfg.get("roles") if isinstance(cfg, dict) else None
    adapters: dict = raw_adapters if isinstance(raw_adapters, dict) else {}
    roles: dict = raw_roles if isinstance(raw_roles, dict) else {}

    def _kind(name: str) -> str | None:
        conf = adapters.get(name)
        if not isinstance(conf, dict) or not conf.get("kind"):
            return None
        return str(conf["kind"])

    for role, conf in roles.items():
        if not isinstance(conf, dict):
            continue
        # 带工具循环的角色：tools 或 write_scope 非空（§11.1 原句的两个触发条件）。
        needs_cli = bool(conf.get("tools")) or bool(conf.get("write_scope"))

        primary = str(conf.get("adapter") or role)
        primary_kind = _kind(primary)
        if needs_cli and primary_kind is not None and primary_kind != "cli":
            errors.append(
                f"角色 {role}：tools/write_scope 非空，主绑定 {primary!r} 是"
                f" {primary_kind} 型，必须为 cli 型（§11.1/§7.3）"
            )

        fallback = conf.get("fallback") or []
        if not isinstance(fallback, (list, tuple)):
            errors.append(
                f"角色 {role}：fallback 必须是有序列表（§11.1），实得"
                f" {type(fallback).__name__}"
            )
            continue
        for item in fallback:
            name = str(item)
            if name not in adapters:
                errors.append(
                    f"角色 {role}：fallback 项 {name!r} 未在 adapters 中声明（§11.1）"
                )
                continue
            kind = _kind(name)
            if needs_cli and kind is not None and kind != "cli":
                errors.append(
                    f"角色 {role}：tools/write_scope 非空，fallback 项 {name!r} 是"
                    f" {kind} 型，必须为 cli 型（§11.1/§7.3）"
                )
    return errors
