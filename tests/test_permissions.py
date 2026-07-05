"""M2 T1 · 权限三件套 + §8.2 越权注入验收测试（spec §4.5/§8.1/§8.2；M2 契约 §3）。

覆盖任务卡条目 (c) + (d)：
  (c) 权限三件套：
      - ensure_worktrees：为 write_scope 非空角色建 worktree（§8.1）；API 型角色跳过。
      - audit_write_scope：git diff 触及路径 ⊆ write_scope 判定（§8.2）。
      - autocommit：'wip:{role}@E{n}' 格式；无改动跳过；返回 sha（§4.5）。
  (d) §8.2 越权注入：调度环调用适配器时若 diff 触及 write_scope 外路径
      → **整体拒收该信封** + git reset --hard {last_ok_commit} + 追加 system 审计事件
      转 moderator；越权信封**不落盘**（不入 events / 无回复行）。

M2 边界（任务卡红线）：
  - 用 tmp_dir 的**真** git 仓库 + worktree（本机 git 可用）；不用 mock git。
  - FakeCliAdapter 在 invoke 时可注入越权写入以验证审计（M2 契约 §2）。

硬约束（契约 §1/§7）：
  - 顶层只 `import orch.scheduler / orch.adapters / orch.store`；具体符号在函数体内引用。
  - 断言只依赖 M2 契约 §3 公开签名与语义；不依赖内部实现细节。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import orch.adapters   # 包级
import orch.scheduler  # 包级（M2 契约 §3 权限模块）
import orch.store      # 包级

# src FakeCliAdapter/FakeApiAdapter 现原生支持 scripted_replies + inject_side_effect
# （M2 契约 §2/§6 回 B-建议1 补齐），测试层包装桩已退役，直接用 src 权威实现。
# 断言逻辑（越权拒收、reset、审计事件）走 src 权威路径不弱化。
from orch.adapters import FakeApiAdapter, FakeCliAdapter


# ——————————————————————————————————————————————————————————————
# 辅助：本地真 git 仓库（作为 target_repo）+ worktree 根目录
# ——————————————————————————————————————————————————————————————

def _git(cwd, *args) -> str:
    """在 cwd 执行 git，返回 stdout；失败抛异常。"""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _init_target_repo(root: Path) -> Path:
    """构造一个真 git 仓库作为 target_repo，含初始提交（工作树上 M2 worktree 会从此拉分支）。

    每个 write_scope 目录（server/、web/、tests/）预置一个 .gitkeep 文件，确保
    `git worktree add` 后目录**真实存在**（git 不跟踪空目录，只跟踪文件）。
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for d in ("server", "web", "tests"):
        (root / d).mkdir()
        (root / d / ".gitkeep").write_text("", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


def _m2_config(target_repo: Path) -> dict:
    """M2 契约 §5：config.roles 每角色的 write_scope / tools / adapter。"""
    return {
        "target_repo": str(target_repo),
        "thread_defaults": {"max_rounds": 100, "loop_limit": 3, "chat_ttl": 10},
        "roles": {
            "moderator": {"can_decide": True, "write_scope": [], "tools": [],
                          "adapter": "api"},
            "backend": {"can_decide": False, "write_scope": ["server/"],
                        "tools": ["Edit", "Write"], "adapter": "cli"},
            "frontend": {"can_decide": False, "write_scope": ["web/"],
                         "tools": ["Edit", "Write"], "adapter": "cli"},
            "tester": {"can_decide": False, "write_scope": ["tests/", "reports/"],
                       "tools": ["Edit", "Write"], "adapter": "cli"},
        },
    }


# ==============================================================
# (c-1) ensure_worktrees：为写域非空角色建 worktree；API 型跳过
# ==============================================================

def test_ensure_worktrees_creates_one_per_writable_role(tmp_dir):
    """§8.1/M2 契约 §3：为每个 write_scope 非空的角色建 worktree；返回 {role: path}。"""
    target = _init_target_repo(tmp_dir / "target")
    roots = tmp_dir / "wts"

    cfg = _m2_config(target)
    result = orch.scheduler.ensure_worktrees(cfg, target, roots)

    # 三个写域非空角色（backend/frontend/tester）应各得一个 worktree。
    assert set(result.keys()) == {"backend", "frontend", "tester"}
    for role, p in result.items():
        assert Path(p).is_dir()
        # 该目录必须已经是 git worktree（.git 文件存在，指向主仓）。
        assert (Path(p) / ".git").exists()


def test_ensure_worktrees_api_role_skipped(tmp_dir):
    """§8.1：API 型角色（本项目 moderator）**无 worktree**——跳过（M2 契约 §2）。
    ensure_worktrees 结果不含 moderator。"""
    target = _init_target_repo(tmp_dir / "target")
    roots = tmp_dir / "wts"
    result = orch.scheduler.ensure_worktrees(_m2_config(target), target, roots)
    assert "moderator" not in result


def test_ensure_worktrees_reuses_existing(tmp_dir):
    """M2 契约 §3：已存在则复用；第二次调用不报错、路径一致。"""
    target = _init_target_repo(tmp_dir / "target")
    roots = tmp_dir / "wts"
    cfg = _m2_config(target)
    r1 = orch.scheduler.ensure_worktrees(cfg, target, roots)
    r2 = orch.scheduler.ensure_worktrees(cfg, target, roots)
    assert r1 == r2


# ==============================================================
# (c-2) audit_write_scope：§8.2 diff 触及路径 ⊆ write_scope 判定
# ==============================================================

def _make_worktree(target: Path, name: str, base: str = "main") -> Path:
    """在 target 上建 worktree 到 target 的邻居目录，返回该 worktree 路径。"""
    wt_dir = target.parent / name
    _git(target, "worktree", "add", "-b", f"feat/{name}", str(wt_dir), base)
    _git(wt_dir, "config", "user.email", "t@example.com")
    _git(wt_dir, "config", "user.name", "t")
    return wt_dir


def _commit_all(wt: Path, msg: str) -> str:
    """在 wt 中 git add -A + commit，返回新 sha。"""
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", msg)
    return _git(wt, "rev-parse", "HEAD").strip()


def test_audit_write_scope_compliant(tmp_dir):
    """§8.2：diff 触及路径全在 write_scope 前缀内 → 合规（True, [])。"""
    target = _init_target_repo(tmp_dir / "target")
    wt = _make_worktree(target, "wt-backend")
    base = _git(wt, "rev-parse", "HEAD").strip()

    # 在 server/ 下改动（属 backend 的 write_scope）。
    (wt / "server" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _commit_all(wt, "server change")

    ok, violations = orch.scheduler.audit_write_scope(
        wt, ["server/"], last_ok_commit=base,
    )
    assert ok is True
    assert violations == []


def test_audit_write_scope_violation_lists_paths(tmp_dir):
    """§8.2：diff 触及 write_scope 外路径 → (False, [越权路径列表])。"""
    target = _init_target_repo(tmp_dir / "target")
    wt = _make_worktree(target, "wt-backend2")
    base = _git(wt, "rev-parse", "HEAD").strip()

    # 越权：backend 只能写 server/，但改了 web/ 与根 README.md。
    (wt / "web" / "index.html").write_text("<html/>", encoding="utf-8")
    (wt / "README.md").write_text("readme changed\n", encoding="utf-8")
    _commit_all(wt, "cross-scope change")

    ok, violations = orch.scheduler.audit_write_scope(
        wt, ["server/"], last_ok_commit=base,
    )
    assert ok is False
    assert violations, "越权路径列表不应为空"
    joined = " ".join(violations)
    assert "web/" in joined or "web/index.html" in joined
    assert "README.md" in joined


def test_audit_write_scope_partial_violation(tmp_dir):
    """§8.2：有的路径合规、有的越权 → 整体越权（不做部分裁剪，简化决策）。"""
    target = _init_target_repo(tmp_dir / "target")
    wt = _make_worktree(target, "wt-backend3")
    base = _git(wt, "rev-parse", "HEAD").strip()

    # 合规: server/x.py；越权: web/y.js
    (wt / "server" / "x.py").write_text("x=1\n", encoding="utf-8")
    (wt / "web" / "y.js").write_text("var y=1;\n", encoding="utf-8")
    _commit_all(wt, "mixed")

    ok, violations = orch.scheduler.audit_write_scope(
        wt, ["server/"], last_ok_commit=base,
    )
    assert ok is False
    joined = " ".join(violations)
    assert "web/" in joined  # 至少揭示越权路径


# ==============================================================
# (c-3) autocommit：§4.5 'wip:{role}@E{n}' 格式 + 无改动跳过
# ==============================================================

def test_autocommit_with_changes_returns_sha(tmp_dir):
    """§4.5：有改动则 git add -A && git commit -m 'wip:{role}@E{n}'，返回 commit sha。"""
    target = _init_target_repo(tmp_dir / "target")
    wt = _make_worktree(target, "wt-a1")

    (wt / "server" / "a.py").write_text("a=1\n", encoding="utf-8")
    sha = orch.scheduler.autocommit(wt, role="backend", event_id=7)
    assert isinstance(sha, str) and len(sha) >= 7
    # commit 消息格式固定（§9.2 依赖）。
    msg = _git(wt, "log", "-1", "--pretty=%B").strip()
    assert msg == "wip:backend@E7"


def test_autocommit_no_changes_returns_none(tmp_dir):
    """§4.5：无改动跳过返回 None（不制造空提交，恢复引用不受污染）。"""
    target = _init_target_repo(tmp_dir / "target")
    wt = _make_worktree(target, "wt-a2")

    # 没有任何修改。
    result = orch.scheduler.autocommit(wt, role="backend", event_id=3)
    assert result is None


def test_autocommit_msg_format_fixed(tmp_dir):
    """§9.2 恢复算法依赖 'wip:{role}@E{n}' 精确格式；不得改变。"""
    target = _init_target_repo(tmp_dir / "target")
    wt = _make_worktree(target, "wt-a3")
    (wt / "server" / "b.py").write_text("b=2\n", encoding="utf-8")
    orch.scheduler.autocommit(wt, role="tester", event_id=42)
    msg = _git(wt, "log", "-1", "--pretty=%B").strip()
    assert msg == "wip:tester@E42"


# ==============================================================
# (d) §8.2 越权注入：调度环调用适配器时，diff 越权 → 整体拒收
#     + git reset --hard + 追加 system 审计事件转 moderator，回复不落盘
# ==============================================================

def _init_thread(thread_dir: Path) -> "orch.store.Store":
    """构造一个 Store 并塞入一个 pending 事件供调度环消费。"""
    store = orch.store.Store(thread_dir)
    store.set_meta("status", "running")
    # 让 backend 是 pending 目标：human 派发 assign 给 backend。
    store.append_event(
        sender="human", type="assign",
        body="please implement", to=["backend"],
    )
    return store


def test_scheduler_rejects_cross_scope_write_and_appends_audit(tmp_dir):
    """§8.2 端到端注入：FakeCliAdapter 在 invoke 时**故意越权**写 web/ 文件，
    调度环审计发现越权 → git reset --hard 回上个合法 commit + system 审计事件转 moderator。
    背景约束：越权信封不入 events（回复被拒收）。
    """
    target = _init_target_repo(tmp_dir / "target")
    thread_dir = tmp_dir / "t-001"
    store = _init_thread(thread_dir)

    # backend 的 worktree（write_scope=server/）。
    wt = _make_worktree(target, "t001-backend")
    base = _git(wt, "rev-parse", "HEAD").strip()

    # FakeCliAdapter 越权：invoke 时先在 wt 里改 web/index.html（越 backend 的 write_scope）。
    # 契约 §2：FakeCliAdapter 支持 inject_side_effect（cwd=worktree），
    # 用于在返回信封前做文件级越权注入以验证 §8.2 审计。
    def _cross_scope_side_effect(worktree: Path):
        (worktree / "web").mkdir(exist_ok=True)
        (worktree / "web" / "index.html").write_text("<pwn/>", encoding="utf-8")

    cfg = _m2_config(target)
    cfg["last_ok_commit"] = base  # 供审计对齐
    # 把 worktree 位置显式塞入 config 供调度层查询（M2 契约 §3）。
    cfg.setdefault("worktrees", {})["backend"] = str(wt)

    ad = FakeCliAdapter(
        role="backend",
        config={"kind": "cli", "start_cmd": "fake", "timeout_s": 10},
        worktree=wt,
        scripted_replies={1: {"to": ["pm"], "type": "report", "body": "done"}},
        inject_side_effect=_cross_scope_side_effect,
    )
    # moderator 是 pending 目标"审计事件转 moderator"的接收方，需存在 adapter；
    # 用 terminate 结束控制流以便断言（moderator 可发 terminate，§3.2）。
    moderator = FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_replies={1: {"to": [], "type": "terminate", "body": "audit done"}},
    )

    orch.scheduler.run_thread(store, cfg, {"backend": ad, "moderator": moderator})

    # (i) 越权信封不落盘：不存在 sender='backend' 且 type='report' 的事件。
    events = store.events()
    assert not any(
        ev.get("from") == "backend" and ev.get("type") == "report"
        for ev in events
    ), "§8.2：越权信封应被整体拒收，不入 events"

    # (ii) worktree 已 git reset --hard 到 base（HEAD == base）。
    head_now = _git(wt, "rev-parse", "HEAD").strip()
    assert head_now == base, "§8.2：越权后必须 git reset --hard 到上个合法 commit"

    # (iii) 追加了一条 system 审计事件转 moderator。
    audit_events = [
        ev for ev in events
        if ev.get("from") == "system" and "moderator" in (ev.get("to") or [])
        and ("越权" in ev.get("body", "") or "write_scope" in ev.get("body", "")
             or "audit" in ev.get("body", "").lower())
    ]
    assert audit_events, "§8.2：应追加 system 审计事件转 moderator"


def test_scheduler_compliant_write_is_accepted(tmp_dir):
    """§8.2 反向：在 write_scope 内写入 → 不触发拒收；信封正常落盘（回复入 events）。"""
    target = _init_target_repo(tmp_dir / "target")
    thread_dir = tmp_dir / "t-002"
    store = _init_thread(thread_dir)

    wt = _make_worktree(target, "t002-backend")
    base = _git(wt, "rev-parse", "HEAD").strip()

    def _in_scope_side_effect(worktree: Path):
        (worktree / "server" / "impl.py").write_text("ok=1\n", encoding="utf-8")

    cfg = _m2_config(target)
    cfg["last_ok_commit"] = base
    cfg.setdefault("worktrees", {})["backend"] = str(wt)

    ad = FakeCliAdapter(
        role="backend",
        config={"kind": "cli", "start_cmd": "fake", "timeout_s": 10},
        worktree=wt,
        scripted_replies={1: {"to": ["pm"], "type": "report", "body": "done"}},
        inject_side_effect=_in_scope_side_effect,
    )
    # pm 收到 backend 的 report 后 → 转手给 moderator（chat 兜底路由，§4.4(1)）；
    # moderator 收到后 terminate 结束控制流以断言 backend 报文已合规入盘。
    pm = FakeApiAdapter(
        role="pm", config={"kind": "api"},
        scripted_replies={1: {"to": [], "type": "chat", "body": "acked"}},
    )
    moderator = FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_replies={1: {"to": [], "type": "terminate", "body": "done"}},
    )

    orch.scheduler.run_thread(store, cfg,
                              {"backend": ad, "moderator": moderator, "pm": pm})

    events = store.events()
    assert any(
        ev.get("from") == "backend" and ev.get("type") == "report"
        for ev in events
    ), "§8.2 合规写入下：backend 的 report 应正常落盘"


# ==============================================================
# (C-4) last_ok_commit 生产回路：autocommit 产出的 sha 必须回写 store，
#       下一轮审计对齐点用**新 sha** 而非静态 config[last_ok_commit]。
# ==============================================================
#
# 三维评审 C-4：生产路径下 config[last_ok_commit] 只有测试补丁塞入，
# 生产恒 skip → §8.2 审计 fail open。修复：autocommit 成功后把 sha 落
# store.set_meta("last_ok_commit:{role}", sha)；下一轮 _last_ok_commit 优先
# 读 store.get_meta（新 sha），config 兜底。
# ==============================================================


def test_autocommit_sha_persists_to_store_and_drives_next_audit(tmp_dir):
    """C-4：autocommit 成功后 sha 落 store.get_meta("last_ok_commit:{role}")，
    第二轮调用即以该 sha 作为审计对齐点，即便 config 从未提供 last_ok_commit。

    模拟两轮 backend invoke：
      - 第 1 轮：写 server/a.py（合规）→ autocommit 生成 sha X。
      - 第 2 轮：写 web/xss.js（越权）→ 审计**必须**以 sha X 为对齐点触发拒收
                （若仍读 config[last_ok_commit]（未提供）→ 审计 skip，越权信封会误入盘）。
    """
    target = _init_target_repo(tmp_dir / "target")
    thread_dir = tmp_dir / "t-c4-a"
    store = orch.store.Store(thread_dir)
    store.set_meta("status", "running")

    # 只播 E1（round1）；round2 由 pm 回一条 assign 到 backend 触发（避免同批聚合
    # 成单次 invoke，只有分两次 dispatch 才能验证第 1 轮的 sha 影响第 2 轮审计）。
    store.append_event(sender="human", type="assign", body="round1", to=["backend"])

    wt = _make_worktree(target, "tc4a-backend")
    base_head = _git(wt, "rev-parse", "HEAD").strip()

    # 关键：config 不塞 last_ok_commit —— 模拟生产路径无外部补丁。
    cfg = _m2_config(target)
    cfg.setdefault("worktrees", {})["backend"] = str(wt)
    assert "last_ok_commit" not in cfg

    call_counter = {"n": 0}

    def _side_effect(worktree: Path):
        call_counter["n"] += 1
        if call_counter["n"] == 1:
            (worktree / "server" / "a.py").write_text("a=1\n", encoding="utf-8")
        else:
            (worktree / "web").mkdir(exist_ok=True)
            (worktree / "web" / "xss.js").write_text("bad\n", encoding="utf-8")

    ad = FakeCliAdapter(
        role="backend",
        config={"kind": "cli", "start_cmd": "fake", "timeout_s": 10},
        worktree=wt,
        scripted_replies={
            1: {"to": ["pm"], "type": "report", "body": "round1 done"},
            2: {"to": ["moderator"], "type": "report", "body": "round2 done"},
        },
        inject_side_effect=_side_effect,
    )
    # pm 收到 backend 的 round1 报告后，assign 一条给 backend 触发 round2。
    pm = FakeApiAdapter(
        role="pm", config={"kind": "api"},
        scripted_replies={
            1: {"to": ["backend"], "type": "assign", "body": "round2 please"},
        },
    )
    moderator = FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_replies={
            1: {"to": [], "type": "terminate", "body": "audited"},
            2: {"to": [], "type": "terminate", "body": "audited-fallback"},
        },
    )

    orch.scheduler.run_thread(
        store, cfg, {"backend": ad, "moderator": moderator, "pm": pm}
    )

    # ① sha 已持久化到 store（生产回路的必要证据）。
    stored_sha = store.get_meta("last_ok_commit:backend")
    assert stored_sha, (
        "C-4：autocommit 成功后必须把 sha 落 store.get_meta('last_ok_commit:backend')；"
        "当前为空 → 生产路径 §8.2 审计 fail open。"
    )
    assert stored_sha != base_head, (
        "C-4：落盘 sha 应为 autocommit 产出的新 sha，不应等于初始 base_head。"
    )

    # ② 第 2 轮越权：越权信封未落盘（审计以 store 里的 sha X 为对齐点触发拒收）。
    events = store.events()
    round2_reports = [
        ev for ev in events
        if ev.get("from") == "backend" and ev.get("type") == "report"
        and ev.get("body") == "round2 done"
    ]
    assert not round2_reports, (
        "C-4：第 2 轮越权信封应被审计整体拒收，不入 events；"
        f"实际找到 {len(round2_reports)} 条 backend/round2 report。"
    )

    audit_events = [
        ev for ev in events
        if ev.get("from") == "system"
        and "moderator" in (ev.get("to") or [])
        and ("越权" in ev.get("body", "") or "write_scope" in ev.get("body", ""))
    ]
    assert audit_events, "C-4：越权时应追加 system 审计事件转 moderator"


def test_last_ok_commit_read_prefers_store_over_config(tmp_dir):
    """C-4：_last_ok_commit(role) 应优先读 store.get_meta('last_ok_commit:{role}')，
    config[last_ok_commit] 仅作兜底。

    构造：
      - store 里预置 base_head（模拟前一轮 autocommit 已回写）；
      - config 里塞一个明显"虚假"值（"0"*40，git 认不出的假 sha）；
      - 单次 backend 越权写入 web/ → 审计对齐点走 store 的 base_head 时能识别越权
        并触发拒收；若误用 config 的假 sha，diff 命令会失败 → 权限模块视为无 diff 走
        fail-open，越权信封会落盘。
    """
    target = _init_target_repo(tmp_dir / "target")
    thread_dir = tmp_dir / "t-c4-b"
    store = orch.store.Store(thread_dir)
    store.set_meta("status", "running")
    store.append_event(
        sender="human", type="assign", body="please implement", to=["backend"],
    )

    wt = _make_worktree(target, "tc4b-backend")
    base_head = _git(wt, "rev-parse", "HEAD").strip()

    store.set_meta("last_ok_commit:backend", base_head)

    cfg = _m2_config(target)
    cfg.setdefault("worktrees", {})["backend"] = str(wt)
    cfg["last_ok_commit"] = "0" * 40  # 虚假 sha —— 若被误用则审计 fail open

    def _cross_scope(worktree: Path):
        (worktree / "web").mkdir(exist_ok=True)
        (worktree / "web" / "bad.js").write_text("pwn\n", encoding="utf-8")

    ad = FakeCliAdapter(
        role="backend",
        config={"kind": "cli", "start_cmd": "fake", "timeout_s": 10},
        worktree=wt,
        scripted_replies={1: {"to": ["moderator"], "type": "report", "body": "done"}},
        inject_side_effect=_cross_scope,
    )
    moderator = FakeApiAdapter(
        role="moderator", config={"kind": "api"},
        scripted_replies={1: {"to": [], "type": "terminate", "body": "audited"}},
    )

    orch.scheduler.run_thread(store, cfg, {"backend": ad, "moderator": moderator})

    head_now = _git(wt, "rev-parse", "HEAD").strip()
    assert head_now == base_head, (
        "C-4：应优先读 store 的 last_ok_commit（base_head 是有效对齐点，越权被拒），"
        f"实际 HEAD={head_now[:8]}"
    )
    events = store.events()
    assert not any(
        ev.get("from") == "backend" and ev.get("type") == "report"
        for ev in events
    ), "C-4：优先读 store 的对齐点 → 越权信封应被拒收，不入 events"
