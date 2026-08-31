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


def ensure_remote(root: Path) -> str | None:
    if not settings.github_remote:
        return None
    remotes = _git(root, "remote", check=False)
    if settings.github_remote not in remotes:
        if "origin" in remotes:
            _git(root, "remote", "set-url", "origin", settings.github_remote)
        else:
            _git(root, "remote", "add", "origin", settings.github_remote)
    return settings.github_remote


def sync_to_github(root: Path) -> dict:
    """commit + push。远端仓库不存在时会返回错误信息（需先在 GitHub 建库）。"""
    try:
        ensure_remote(root)
        commit_all(root, "sync: backup to github")
        p = subprocess.run(
            ["git", "-C", str(root), "push", "-u", "origin", settings.sync_branch],
            capture_output=True, text=True, timeout=90,
        )
        if p.returncode != 0:
            return {"ok": False, "error": (p.stderr or "").strip()[-400:]}
        return {"ok": True, "branch": settings.sync_branch}
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}
