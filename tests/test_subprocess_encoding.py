"""subprocess 编码回归：git / verify / gate_op / 异步作业输出含非 ASCII 必须完整读回。

缺陷现场（IMPLEMENTATION_NOTES"联跑捎出的新缺陷"台账）：
  subprocess.run/Popen 传 text=True 而不传 encoding → Windows 退
  locale.getpreferredencoding(False)=cp936；而 git 与真实 CLI 的输出恒为 UTF-8
  （与 adapters/__init__.py Q1 实测同源）。cp936 解码 UTF-8 字节的两种死法：
    a) UnicodeDecodeError 死在 subprocess 内部 reader 线程 → 调用点拿到
       rc=0 + stdout=''（不抛错，stderr 上刷 traceback）；
    b) "成功"解出 mojibake（UTF-8 双字节对在 cp936 恰好合法）。
  两种模式下诊断信息全丢。本仓路径自带中文（多agent协作系统），
  `git worktree list --porcelain` 每轮输出中文绝对路径 → 联跑每轮喷堆栈的现场。

钉住四处（修法与 adapters/__init__.py 样板同款 encoding='utf-8', errors='replace'）：
  ① permissions._git：中文文件名（quotepath=false）原文读回；
  ② permissions._worktree_exists：中文路径下已注册 worktree 判定不失明；
  ③ core._run_verify：verify 命令的 UTF-8 中文输出原文入 output；
  ④ systemexec._run_gate_op：gate_op 命令同上；
  ⑤ async_core.register_async_job：异步作业回调 system 事件 body 含原文。

环境说明：本组用例在 UTF-8 locale（PYTHONUTF8=1 / PEP 686）机器上天然绿——
缺陷只在 legacy codepage（cp936 等）环境暴露，而那正是联跑发生的环境。
子进程统一用 `python -X utf8` 强制 UTF-8 输出，与"真实 CLI 输出恒 UTF-8"前提一致。

硬约束（契约 §1/§7）：顶层只包级 import；被测符号在函数体内引用。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import orch.scheduler  # 包级
import orch.store      # 包级

# 中文文件名/输出常量：断言必须见到"原文"，mojibake 与空串都判红。
CJK_FILENAME = "中文文件名.txt"


def _run_git(cwd, *args) -> subprocess.CompletedProcess:
    """测试自备 git（显式 UTF-8）：仅作 setup，不代跑被测函数。"""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    )


def _init_repo(root: Path) -> Path:
    """真 git 仓 + 初始提交（与 test_permissions._init_target_repo 同姿势，简化版）。"""
    root.mkdir(parents=True, exist_ok=True)
    _run_git(root, "init", "-b", "main")
    _run_git(root, "config", "user.email", "t@example.com")
    _run_git(root, "config", "user.name", "t")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-m", "init")
    return root


def _utf8_emitter(tmp_dir: Path, text: str) -> str:
    """生成一条 shell 命令：子进程以 UTF-8 输出 text（-X utf8 强制，不受控制台 codepage 摆布）。"""
    script = tmp_dir / "emit.py"
    script.write_text(f"print({text!r})\n", encoding="utf-8")
    return f'"{sys.executable}" -X utf8 "{script}"'


# ==================================================================
# ① permissions._git：git 输出的中文必须原文读回（rc=0+stdout='' 即缺陷）
# ==================================================================

def test_git_reads_chinese_filename_verbatim(tmp_dir):
    """带中文文件名的真仓：`-c core.quotepath=false status --porcelain` 输出原始
    UTF-8 文件名字节。_git 必须原文读回——reader 线程解码死亡时调用点只见
    rc=0 + stdout=''（不抛错），mojibake 时不含原文，两种都在此判红。"""
    from orch.scheduler.permissions import _git
    repo = _init_repo(tmp_dir / "repo")
    (repo / CJK_FILENAME).write_text("x\n", encoding="utf-8")

    proc = _git(repo, "-c", "core.quotepath=false", "status", "--porcelain")

    assert proc.returncode == 0
    assert CJK_FILENAME in (proc.stdout or ""), (
        f"git stdout 未完整读回（空串=reader 线程解码死亡；乱码=错误 codepage）："
        f"{proc.stdout!r}"
    )


# ==================================================================
# ② permissions._worktree_exists：中文路径 worktree 判定不失明
# ==================================================================

def test_worktree_exists_sees_chinese_path(tmp_dir):
    """`git worktree list --porcelain` 输出含中文的绝对路径（本仓真实形态：项目
    目录名即中文）。解码失败 → stdout='' → 已注册 worktree 被误判不存在 →
    ensure_worktrees 走重建路径报错 + 每轮 stderr 喷 traceback。"""
    from orch.scheduler.permissions import _worktree_exists
    base = tmp_dir / "多agent工作区"  # 项目路径已含中文，此处再叠一层保住自明性
    repo = _init_repo(base / "repo")
    wt = base / "wt-角色"
    _run_git(repo, "worktree", "add", "-b", "feat/enc-t", str(wt), "HEAD")

    assert _worktree_exists(repo, wt), (
        "已注册 worktree 被误判不存在：worktree list --porcelain 的中文路径行"
        "未被正确解码读回"
    )


# ==================================================================
# ③ core._run_verify：verify 命令的 UTF-8 输出原文入 output
# ==================================================================

def test_run_verify_preserves_utf8_output(tmp_dir):
    """§8.3 verify 钩子的 output 是降级审计与人读诊断的唯一载体：
    UTF-8 中文输出必须原文保留（空串/乱码=诊断信息全丢）。"""
    from orch.scheduler.core import _run_verify
    cmd = _utf8_emitter(tmp_dir, "中文验证输出")
    config = {"roles": {"tester": {"verify": {"cmd": cmd, "cwd": str(tmp_dir)}}}}

    res = _run_verify(config, "tester")

    assert res is not None
    assert res["exit_code"] == 0, res
    assert "中文验证输出" in res["output"], (
        f"verify output 未保住 UTF-8 原文：{res['output']!r}"
    )


# ==================================================================
# ④ systemexec._run_gate_op：gate_op 命令输出同上
# ==================================================================

def test_run_gate_op_preserves_utf8_output(tmp_dir):
    """§5.5 系统执行器的 output 直接进 system 事件 body（人读审计）：
    UTF-8 中文输出必须原文保留。"""
    from orch.scheduler.systemexec import _run_gate_op
    cmd = _utf8_emitter(tmp_dir, "中文门禁输出")

    res = _run_gate_op({"cmd": cmd, "cwd": str(tmp_dir)})

    assert res["exit_code"] == 0, res
    assert "中文门禁输出" in res["output"], (
        f"gate_op output 未保住 UTF-8 原文：{res['output']!r}"
    )


# ==================================================================
# ⑤ async_core.register_async_job：异步作业回调 body 含原文
# ==================================================================

def test_async_job_callback_preserves_utf8_output(thread_dir):
    """§5.2 异步作业完成后 append 的 system 事件 body 含作业输出：
    UTF-8 中文输出必须原文进 body（Popen 与 run 同病同修）。"""
    st = orch.store.Store(thread_dir)
    st.set_meta("status", "running")
    cmd = [sys.executable, "-X", "utf8", "-c", "print('中文作业输出')"]

    async def _run() -> list[dict]:
        orch.scheduler.register_async_job(
            st, corr="job-enc", cmd=cmd, callback_to="moderator",
        )
        # 轮询落盘真相（与 test_async_scheduler c-1 同姿势），最多 10s。
        for _ in range(100):
            evs = [
                e for e in st.events()
                if e.get("type") == "system" and e.get("corr") == "job-enc"
            ]
            if evs:
                return evs
            await asyncio.sleep(0.1)
        return []

    evs = asyncio.run(_run())

    assert evs, "§5.2：作业完成后应 append system 回调事件（corr=job-enc）"
    body = evs[-1].get("body") or ""
    assert "中文作业输出" in body, (
        f"异步作业回调 body 未保住 UTF-8 原文：{body!r}"
    )
