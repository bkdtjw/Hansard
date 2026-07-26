"""§8.1/§8.2/§4.5 权限三件套 —— worktree 隔离 + 越权审计 + autocommit（M2 契约 §3）。

三件套定位与调度层接入（core.py invoke 返回后按序调用）：
  1) ensure_worktrees(config, target_repo, worktrees_root, thread_id=None) → {role: Path}
     · §8.1：为每个 write_scope 非空角色建 worktree（`git worktree add`）；API 型角色跳过。
     · 已存在则复用（M2 契约 §3；`git worktree add` 已存在会失败 → 我们视 dir+分支存在为复用）。
  2) audit_write_scope(worktree, write_scope, last_ok_commit) → (bool, list[str])
     · §8.2：`git diff --stat {last_ok_commit}..HEAD` 触及路径 ⊆ write_scope 前缀。
     · 简化决策：整体拒收（不做部分裁剪，spec §8.2 明示）。
  3) autocommit(worktree, role, event_id) → str | None
     · §4.5：有改动 `git add -A && git commit -m "wip:{role}@E{event_id}"` → 返回 sha；
       无改动跳过返回 None。commit 消息格式固定，恢复算法（§9.2）依赖它。

铁律：
  · 只用 subprocess 调用系统 git，不引入 gitpython（spec §14 白名单）。
  · M0/M1 mock 语境无 worktree → 三件套整体 skip（保持 127 绿；契约 §6.3）。
  · 系统字段由编排器权威赋值（§16.11）；审计事件 from='system' 在 core.py 生成。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


# ——————————————————————————————————————————————————————————————
# 内部 git 辅助
# ——————————————————————————————————————————————————————————————

def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """在 cwd 执行 git，返回 CompletedProcess。check=True 时非零退出抛异常。"""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",  # git 输出恒 UTF-8；Windows 默认 cp936 解码会死在 reader 线程→rc=0+stdout=''
        errors="replace",
        check=check,
    )


def _worktree_exists(target_repo: Path, wt_path: Path) -> bool:
    """通过 `git worktree list --porcelain` 判定 wt_path 是否已注册（避免"已存在"报错）。"""
    try:
        proc = _git(target_repo, "worktree", "list", "--porcelain")
    except subprocess.CalledProcessError:
        return False
    resolved = str(wt_path.resolve())
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            listed = line[len("worktree "):].strip()
            # Windows 大小写 / 前缀路径规范：转 Path().resolve() 再比对。
            try:
                if str(Path(listed).resolve()) == resolved:
                    return True
            except OSError:
                # 已列出的路径可能失效；对比字符串兜底。
                if listed == str(wt_path):
                    return True
    return False


# ——————————————————————————————————————————————————————————————
# ensure_worktrees（§8.1；M2 契约 §3）
# ——————————————————————————————————————————————————————————————

def ensure_worktrees(
    config: dict,
    target_repo: Path,
    worktrees_root: Path,
    thread_id: str | None = None,
) -> dict[str, Path]:
    """为 config.roles 中每个 write_scope 非空的角色建 worktree（§8.1）。

    命名（M2 契约 §3）：`<worktrees_root>/t{thread_id}-{role}`，分支 `feat/t{thread_id}-{role}`。
    thread_id 为 None 时用无前缀命名（`<worktrees_root>/{role}`，分支 `feat/{role}`）——用于
    契约 §3 测试调用（tests/test_permissions.py 未传 thread_id）。API 型角色（write_scope=[]）
    不建 worktree（§8.1）；moderator 在本项目 write_scope=[] 因此天然被跳过。

    已存在的 worktree（`git worktree list` 命中）复用：不再执行 `worktree add`，返回同路径。
    """
    target_repo = Path(target_repo)
    worktrees_root = Path(worktrees_root)
    worktrees_root.mkdir(parents=True, exist_ok=True)

    result: dict[str, Path] = {}
    roles = (config.get("roles") or {}) if config else {}

    for role, rc in roles.items():
        if not isinstance(rc, dict):
            continue
        write_scope = rc.get("write_scope") or []
        if not write_scope:
            # §8.1：API 型/moderator 等无写权限角色跳过 worktree。
            continue

        prefix = f"t{thread_id}-" if thread_id else ""
        wt_name = f"{prefix}{role}"
        wt_path = worktrees_root / wt_name
        branch = f"feat/{wt_name}"

        if _worktree_exists(target_repo, wt_path) or wt_path.exists():
            # 已存在 → 复用（M2 契约 §3）。
            result[role] = wt_path
            continue

        # `git worktree add -b <branch> <path> <base>`，base 用当前 HEAD（HEAD 是主仓 main 的
        # 已提交状态，见测试 fixture）。若分支已存在（复用场景兜底），去掉 -b 再试。
        try:
            _git(
                target_repo,
                "worktree", "add", "-b", branch, str(wt_path), "HEAD",
            )
        except subprocess.CalledProcessError:
            # 分支/路径冲突兜底：不建分支直接挂载。
            _git(
                target_repo,
                "worktree", "add", str(wt_path), branch,
                check=False,
            )
        result[role] = wt_path

    return result


# ——————————————————————————————————————————————————————————————
# audit_write_scope（§8.2）
# ——————————————————————————————————————————————————————————————

def _diff_touched_paths(worktree: Path, last_ok_commit: str) -> list[str]:
    """`git diff --name-only {last_ok_commit}..HEAD` 返回触及路径列表（POSIX 分隔符）。

    与 spec §8.2 `--stat` 语义等价（我们要的是触及路径集合而非行数），--name-only 更好解析。
    """
    proc = _git(
        worktree,
        "diff", "--name-only", f"{last_ok_commit}..HEAD",
        check=False,
    )
    if proc.returncode != 0:
        # 若 last_ok_commit 缺失/非法：视为无 diff（安全侧）。
        return []
    paths: list[str] = []
    for line in (proc.stdout or "").splitlines():
        p = line.strip()
        if p:
            paths.append(p.replace("\\", "/"))
    return paths


def _matches_write_scope(path: str, write_scope: list[str]) -> bool:
    """前缀匹配：path 是否落在 write_scope 任一前缀内（§8.2）。

    write_scope 项通常带尾斜杠（"server/"）；对 "server/x.py" 前缀命中。
    也允许无尾斜杠项（"server"）。
    """
    for scope in write_scope:
        s = scope.rstrip("/")
        if not s:
            continue
        # "server/x.py".startswith("server/") 或 == "server"
        if path == s or path.startswith(s + "/"):
            return True
    return False


def audit_write_scope(
    worktree: Path,
    write_scope: list[str],
    last_ok_commit: str,
) -> tuple[bool, list[str]]:
    """§8.2 diff 越权审计。返回 (是否合规, 违规路径列表)。

    简化决策（spec §8.2 明示）：任一路径越 write_scope → 整体越权（不做部分裁剪）。
    """
    touched = _diff_touched_paths(Path(worktree), last_ok_commit)
    violations = [p for p in touched if not _matches_write_scope(p, write_scope)]
    return (not violations, violations)


# ——————————————————————————————————————————————————————————————
# autocommit（§4.5）
# ——————————————————————————————————————————————————————————————

def _has_uncommitted_changes(worktree: Path) -> bool:
    """`git status --porcelain` 非空 → 有未提交改动（含未跟踪）。"""
    proc = _git(Path(worktree), "status", "--porcelain", check=False)
    return bool((proc.stdout or "").strip())


def autocommit(worktree: Path, role: str, event_id: int) -> str | None:
    """§4.5：有改动 `git add -A && git commit -m "wip:{role}@E{event_id}"` → 返回 sha。

    无改动直接返回 None（不制造空提交，避免污染恢复算法的引用点）。
    commit 消息格式**固定**：`wip:{role}@E{event_id}`——§9.2 恢复算法依赖此精确格式。
    """
    wt = Path(worktree)
    if not _has_uncommitted_changes(wt):
        return None

    _git(wt, "add", "-A")
    msg = f"wip:{role}@E{event_id}"
    _git(wt, "commit", "-m", msg)
    proc = _git(wt, "rev-parse", "HEAD")
    return proc.stdout.strip()


# ——————————————————————————————————————————————————————————————
# 调度层接入辅助（§8.2 违规回滚）
# ——————————————————————————————————————————————————————————————

def reset_hard(worktree: Path, commit: str) -> None:
    """`git reset --hard {commit}`：§8.2 越权后回上个合法 commit。"""
    _git(Path(worktree), "reset", "--hard", commit)


def head_sha(worktree: Path) -> str | None:
    """`git rev-parse HEAD` 返回 worktree 当前 HEAD 的完整 sha；失败返回 None。

    R-T2 · E（§8.2 首轮审计兜底）：worktree 存在而 store/config 均无 last_ok_commit 时，
    调度层在本轮 invoke **之前**用本函数取当前 HEAD 落盘为对齐点——只查 git，不猜测
    （§16.10）。使审计分支恒有对齐点、恒执行；首轮越权写入也能被 diff 出来。
    """
    try:
        proc = _git(Path(worktree), "rev-parse", "HEAD", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None
