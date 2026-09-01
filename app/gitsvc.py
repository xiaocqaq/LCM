"""git 版本化：每用户一个 repo（/var/lib/memorys/data/users/uN）。
写入即提交（失败不阻断主流程），sync 负责推 GitHub 长期备份。
"""
import os
import re
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


# ---- 分支管理（供 Web UI 图形化操作）----
# 场景：想整理/重构记忆但不想动主线，就开个分支改，满意了再合回来。
# 分支名做严格白名单校验：这些值最终会拼进 git 命令，不能让用户输入自由发挥。
_BRANCH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$")


def _valid_branch(name: str) -> str:
    """校验分支名，非法直接抛。挡掉 `--flag`、`..`、`@{`、空格等 git 特殊语义。"""
    name = (name or "").strip()
    if not _BRANCH_OK.match(name):
        raise ValueError("分支名只允许字母数字和 . _ - /，且需以字母数字开头")
    if ".." in name or name.endswith("/") or name.endswith(".lock") or "@{" in name:
        raise ValueError("分支名含 git 保留写法（.. / 结尾 / .lock / @{）")
    return name


def current_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False) or settings.sync_branch


def list_branches(root: Path) -> dict:
    """列出本地分支 + 每个分支的最后一次提交，标出当前分支。"""
    cur = current_branch(root)
    out = _git(root, "for-each-ref", "--sort=-committerdate", "refs/heads/",
               "--format=%(refname:short)|%(objectname:short)|%(committerdate:iso)|%(contents:subject)",
               check=False)
    items = []
    for line in (out or "").split("\n"):
        parts = line.split("|", 3)
        if len(parts) == 4:
            items.append({"name": parts[0], "hash": parts[1], "date": parts[2],
                          "subject": parts[3], "current": parts[0] == cur})
    return {"current": cur, "branches": items, "dirty": has_changes(root)}


def create_branch(root: Path, name: str, switch: bool = True) -> dict:
    """从当前 HEAD 开新分支。默认建完就切过去。"""
    name = _valid_branch(name)
    existing = {b["name"] for b in list_branches(root)["branches"]}
    if name in existing:
        raise ValueError(f"分支 {name} 已存在")
    # 未提交的改动先落一笔，否则切分支会把它们带过去，造成归属混乱
    try_commit_all(root, "wip: 建分支前自动保存")
    _git(root, "branch", "--", name)
    if switch:
        _git(root, "checkout", name)
    return {"ok": True, "branch": name, "current": current_branch(root),
            "message": f"已创建分支 {name}" + ("并切换过去" if switch else "")}


def switch_branch(root: Path, name: str) -> dict:
    """切分支。切之前把未提交改动提交掉，避免脏工作区导致 checkout 失败或串味。"""
    name = _valid_branch(name)
    existing = {b["name"] for b in list_branches(root)["branches"]}
    if name not in existing:
        raise ValueError(f"分支 {name} 不存在")
    try_commit_all(root, "wip: 切分支前自动保存")
    _git(root, "checkout", name)
    return {"ok": True, "branch": name, "current": current_branch(root),
            "message": f"已切换到 {name}",
            "note": "md 文件已按该分支内容换过，记得在「同步」页重建索引。"}


def merge_branch(root: Path, name: str, message: str = "") -> dict:
    """把指定分支合进当前分支。冲突不自动解决，回滚后报错让人工处理。"""
    name = _valid_branch(name)
    cur = current_branch(root)
    if name == cur:
        raise ValueError("不能把分支合并到它自己")
    try_commit_all(root, "wip: 合并前自动保存")
    p = subprocess.run(
        ["git", "-C", str(root), "merge", "--no-ff", "-m",
         message or f"merge: {name} → {cur}", name],
        capture_output=True, text=True, timeout=60,
    )
    if p.returncode != 0:
        err = ((p.stderr or "") + (p.stdout or "")).strip()[-400:]
        # 合并失败大概率是冲突，先把工作区恢复干净，别把半成品留给用户
        subprocess.run(["git", "-C", str(root), "merge", "--abort"],
                       capture_output=True, timeout=30)
        return {"ok": False, "branch": name, "current": cur, "error": err,
                "hint": "合并有冲突，已自动回滚。请在服务器上手工处理，或改为逐篇复制内容。"}
    return {"ok": True, "branch": name, "current": cur,
            "message": f"已把 {name} 合并进 {cur}",
            "note": "内容有变，记得在「同步」页重建索引。"}


def delete_branch(root: Path, name: str, force: bool = False) -> dict:
    """删分支。默认拒删未合并的分支（git -d 的语义），force 才用 -D。"""
    name = _valid_branch(name)
    if name == current_branch(root):
        raise ValueError("不能删除当前所在分支，请先切到别的分支")
    if name == settings.sync_branch:
        raise ValueError(f"{settings.sync_branch} 是主分支，不允许删除")
    p = subprocess.run(["git", "-C", str(root), "branch", "-D" if force else "-d", name],
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        err = (p.stderr or "").strip()[-300:]
        out = {"ok": False, "branch": name, "error": err}
        if "not fully merged" in err:
            out["hint"] = "该分支还有没合并的提交，删了内容就丢了。确认要丢就勾选「强制删除」。"
        return out
    return {"ok": True, "branch": name, "message": f"已删除分支 {name}"}


def commit_file_to_branch(root: Path, branch: str, rel_path: str,
                          content: str, message: str) -> dict:
    """把一个文件直接提交到指定分支，**完全不碰工作区和当前分支**。

    为什么不用 checkout：切过去写完再切回来要翻动两次工作区、重建两次索引，
    中途任何一步失败都会把用户留在错误的分支上。用 plumbing 直接在对象库里
    造 blob → tree → commit，工作区一动不动，当前分支也不变。

    branch 不存在时从当前 HEAD 开一个。返回新 commit 的短 hash。
    """
    branch = _valid_branch(branch)
    if not rel_path or rel_path.startswith("/") or ".." in rel_path:
        raise ValueError("非法文件路径")

    # 1) 内容写进对象库，拿到 blob hash
    p = subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"],
                       input=content, capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(f"hash-object 失败: {(p.stderr or '').strip()[:200]}")
    blob = p.stdout.strip()

    # 2) 用临时 index 拼出目标分支的新 tree（不能用默认 index，那是工作区的）
    ref = f"refs/heads/{branch}"
    parent = _git(root, "rev-parse", "--verify", "--quiet", ref, check=False)
    created = not parent
    if not parent:
        # 分支不存在 → 以当前 HEAD 为基础开一个
        parent = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", check=False)

    tmp_index = root / ".git" / f"index-mem-{blob[:8]}"
    env = {"GIT_INDEX_FILE": str(tmp_index)}
    try:
        if parent:
            _git_env(root, env, "read-tree", parent)
        else:
            _git_env(root, env, "read-tree", "--empty")
        _git_env(root, env, "update-index", "--add", "--cacheinfo",
                 f"100644,{blob},{rel_path}")
        tree = _git_env(root, env, "write-tree")
    finally:
        tmp_index.unlink(missing_ok=True)

    # 3) 造 commit 并把分支指针挪上去
    args = ["commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    commit = _git_env(root, {
        "GIT_AUTHOR_NAME": settings.sync_identity_name,
        "GIT_AUTHOR_EMAIL": settings.sync_identity_email,
        "GIT_COMMITTER_NAME": settings.sync_identity_name,
        "GIT_COMMITTER_EMAIL": settings.sync_identity_email,
    }, *args)
    _git(root, "update-ref", ref, commit)
    return {"ok": True, "branch": branch, "commit": commit[:7],
            "created_branch": created}


def read_file_from_branch(root: Path, branch: str, rel_path: str) -> str | None:
    """读取指定分支上某个文件的内容。不存在返回 None。"""
    branch = _valid_branch(branch)
    out = subprocess.run(
        ["git", "-C", str(root), "show", f"refs/heads/{branch}:{rel_path}"],
        capture_output=True, text=True, timeout=30)
    return out.stdout if out.returncode == 0 else None


def _git_env(repo: Path, env: dict, *args: str, timeout: int = 30) -> str:
    """带额外环境变量跑 git（用于 GIT_INDEX_FILE / 作者身份）。"""
    merged = dict(os.environ)
    merged.update(env)
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, timeout=timeout, env=merged)
    if p.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(p.stderr or '').strip()[:300]}")
    return p.stdout.strip()


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
