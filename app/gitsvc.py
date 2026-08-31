"""git 版本化：每用户一个 repo（/var/lib/memorys/data/users/uN）。
写入即提交（失败不阻断主流程），sync 负责推 GitHub 长期备份。
"""
import subprocess
from pathlib import Path

from .config import settings
from .mdstore import user_root


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 30) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(p.stderr or '').strip()[:300]}")
    return p.stdout.strip()


def ensure_repo(data_dir: str, user_id: int) -> Path:
    root = user_root(data_dir, user_id)
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        _git(root, "init", "-b", settings.sync_branch)
        _git(root, "config", "user.name", settings.sync_identity_name)
        _git(root, "config", "user.email", settings.sync_identity_email)
        (root / ".gitignore").write_text(".DS_Store\nThumbs.db\n*.tmp\n")
    return root


def has_changes(root: Path) -> bool:
    return bool(_git(root, "status", "--porcelain", check=False))


def commit_all(root: Path, message: str) -> str | None:
    _git(root, "add", "-A")
    if not has_changes(root):
        return None
    _git(root, "commit", "-m", message, "--no-verify")
    return _git(root, "rev-parse", "--short", "HEAD")


def try_commit_all(root: Path, message: str) -> str | None:
    try:
        return commit_all(root, message)
    except Exception:
        return None


def history(root: Path, rel_path: str, limit: int = 20) -> list[dict]:
    try:
        out = _git(root, "log", "--follow", f"--max-count={limit}", "--pretty=%h|%ad|%s", "--date=iso", "--", rel_path, check=False)
    except Exception:
        return []
    rows = []
    for line in out.split("\n"):
        if "|" in line:
            h, d, s = line.split("|", 2)
            rows.append({"hash": h, "date": d, "message": s})
    return rows


def remote_for(user_id: int) -> str | None:
    """每用户的远端地址。

    MEM_GITHUB_REMOTE 支持 {uid} 占位符：
      git@github.com:me/memorys-u{uid}.git  → 每个用户一个独立仓库
      git@github.com:me/memorys-data.git    → 共用一个仓库，靠分支隔离（见 branch_for）
    """
    tpl = (settings.github_remote or "").strip()
    if not tpl:
        return None
    return tpl.replace("{uid}", str(user_id))


def branch_for(user_id: int) -> str:
    """每用户的备份分支。

    远端模板里带 {uid}（一人一仓）时，直接用 sync_branch；
    否则多用户共用一个仓库，必须按 u<uid> 分支隔离，否则会互相覆盖。
    """
    if "{uid}" in (settings.github_remote or ""):
        return settings.sync_branch
    return f"u{user_id}"


def ensure_remote(root: Path, user_id: int) -> str | None:
    url = remote_for(user_id)
    if not url:
        return None
    remotes = _git(root, "remote", check=False).split()
    if "origin" in remotes:
        current = _git(root, "remote", "get-url", "origin", check=False)
        if current != url:
            _git(root, "remote", "set-url", "origin", url)
    else:
        _git(root, "remote", "add", "origin", url)
    return url


def sync_to_github(root: Path, user_id: int) -> dict:
    """commit + push 到该用户自己的远端分支。

    远端仓库不存在时返回错误信息（需先在 GitHub 建库），不抛异常。
    """
    url = remote_for(user_id)
    if not url:
        return {"ok": False, "error": "未配置 MEM_GITHUB_REMOTE，无法推送。"}
    branch = branch_for(user_id)
    try:
        ensure_remote(root, user_id)
        commit_all(root, "sync: backup to github")
        # 本地分支名可能是 main（ensure_repo 用 sync_branch 建的），
        # 推送时显式写成 HEAD:<branch>，保证落到该用户自己的远端分支
        p = subprocess.run(
            ["git", "-C", str(root), "push", "-u", "origin", f"HEAD:refs/heads/{branch}"],
            capture_output=True, text=True, timeout=120,
        )
        if p.returncode != 0:
            err = (p.stderr or "").strip()[-400:]
            out = {"ok": False, "remote": url, "branch": branch, "error": err}
            if "Repository not found" in err or "does not exist" in err:
                out["hint"] = (
                    f"远端仓库还不存在。先在 GitHub 建一个私有空仓库（名字对上 {url}），"
                    "建好后再点一次推送即可；SSH 免密本机已配好，无需额外授权。"
                )
            elif "Permission denied" in err or "publickey" in err:
                out["hint"] = "SSH 认证失败：确认本机 ~/.ssh 私钥已加到 GitHub 账号。"
            return out
        return {"ok": True, "remote": url, "branch": branch,
                "message": f"已推送到 {url} 分支 {branch}"}
    except Exception as e:
        return {"ok": False, "remote": url, "branch": branch, "error": str(e)[:400]}


def pull_from_github(root: Path, user_id: int) -> dict:
    """从远端拉取（用于换机/灾恢）。冲突时不自动合并，返回错误让人工处理。"""
    url = remote_for(user_id)
    if not url:
        return {"ok": False, "error": "未配置 MEM_GITHUB_REMOTE。"}
    branch = branch_for(user_id)
    try:
        ensure_remote(root, user_id)
        p = subprocess.run(
            ["git", "-C", str(root), "pull", "--ff-only", "origin", branch],
            capture_output=True, text=True, timeout=120,
        )
        if p.returncode != 0:
            return {"ok": False, "remote": url, "branch": branch,
                    "error": (p.stderr or "").strip()[-400:]}
        return {"ok": True, "remote": url, "branch": branch, "output": p.stdout.strip()[-400:]}
    except Exception as e:
        return {"ok": False, "remote": url, "branch": branch, "error": str(e)[:400]}
