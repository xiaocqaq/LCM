"""验证 GitHub 备份链路（用本地 bare 仓库替代真实 GitHub，代码路径完全相同）。

重点验证：多用户共用一个远端仓库时，各自落到 u<uid> 分支，不互相覆盖。
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/opt/memorys")

from app import gitsvc  # noqa: E402
from app.config import settings  # noqa: E402
from app.mdstore import user_root  # noqa: E402

FAIL: list[str] = []


def check(name, cond, extra=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + (f" — {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr).strip()


TMP = Path("/tmp/memorys-git-test")
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True)

# ---- 1. 共用一个远端仓库（remote 不含 {uid}）→ 应按 u<uid> 分支隔离 ----
bare = TMP / "shared.git"
subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], capture_output=True, timeout=30)

settings.github_remote = str(bare)
settings.sync_branch = "main"

DATA = str(TMP / "data")
made = {}
for uid, title in ((901, "用户901的记忆"), (902, "用户902的记忆")):
    root = gitsvc.ensure_repo(DATA, uid)
    (root / "main").mkdir(parents=True, exist_ok=True)
    (root / "main" / f"doc-{uid}.md").write_text(
        f"---\nid: mem{uid}\ntitle: {title}\n---\n\n# {title}\n\n仅属于用户 {uid}。\n",
        encoding="utf-8")
    gitsvc.commit_all(root, f"create: {title}")
    made[uid] = root

check("branch_for 共用仓库时按用户隔离",
      gitsvc.branch_for(901) == "u901" and gitsvc.branch_for(902) == "u902",
      f"{gitsvc.branch_for(901)} / {gitsvc.branch_for(902)}")

r1 = gitsvc.sync_to_github(made[901], 901)
check("用户901 推送成功", r1.get("ok") is True, str(r1))
r2 = gitsvc.sync_to_github(made[902], 902)
check("用户902 推送成功", r2.get("ok") is True, str(r2))

code, out = git(bare, "branch", "--list")
branches = sorted(b.strip().lstrip("* ") for b in out.split("\n") if b.strip())
check("远端出现两个独立分支", branches == ["u901", "u902"], str(branches))

# 各分支只含自己的文件 —— 这才是"没互相覆盖"的证据
code, f901 = git(bare, "ls-tree", "-r", "--name-only", "u901")
code, f902 = git(bare, "ls-tree", "-r", "--name-only", "u902")
check("u901 分支只有自己的文档",
      "doc-901.md" in f901 and "doc-902.md" not in f901, f901.replace("\n", " "))
check("u902 分支只有自己的文档",
      "doc-902.md" in f902 and "doc-901.md" not in f902, f902.replace("\n", " "))

# ---- 2. 幂等：无改动再推一次不应报错 ----
r3 = gitsvc.sync_to_github(made[901], 901)
check("无改动重复推送不报错", r3.get("ok") is True, str(r3))

# ---- 3. 增量推送 ----
(made[901] / "main" / "doc-901b.md").write_text("---\nid: x\ntitle: 增量\n---\n\n新增\n", encoding="utf-8")
gitsvc.commit_all(made[901], "create: 增量")
gitsvc.sync_to_github(made[901], 901)
code, f901b = git(bare, "ls-tree", "-r", "--name-only", "u901")
check("增量推送生效", "doc-901b.md" in f901b, f901b.replace("\n", " "))

# ---- 4. 灾恢：从远端 clone 回来，md 原样可读 ----
restore = TMP / "restore901"
code, out = git(TMP, "clone", "-b", "u901", str(bare), str(restore))
md = (restore / "main" / "doc-901.md")
check("从远端恢复出的 md 内容完整",
      md.exists() and "仅属于用户 901" in md.read_text(encoding="utf-8"),
      f"exists={md.exists()}")

# ---- 5. 一人一仓模板（remote 含 {uid}）----
settings.github_remote = str(TMP / "solo-u{uid}.git")
for uid in (903,):
    subprocess.run(["git", "init", "--bare", "-b", "main", str(TMP / f"solo-u{uid}.git")],
                   capture_output=True, timeout=30)
check("remote_for 展开 {uid}",
      gitsvc.remote_for(903) == str(TMP / "solo-u903.git"), str(gitsvc.remote_for(903)))
check("一人一仓时分支用 sync_branch", gitsvc.branch_for(903) == "main", gitsvc.branch_for(903))
root903 = gitsvc.ensure_repo(DATA, 903)
(root903 / "main").mkdir(parents=True, exist_ok=True)
(root903 / "main" / "doc-903.md").write_text("---\nid: y\ntitle: 独立仓\n---\n\n独立仓库\n", encoding="utf-8")
gitsvc.commit_all(root903, "create: 独立仓")
r4 = gitsvc.sync_to_github(root903, 903)
check("一人一仓推送成功", r4.get("ok") is True and r4.get("branch") == "main", str(r4))

# ---- 6. 未配置远端时给出可读错误，而不是抛异常 ----
settings.github_remote = ""
r5 = gitsvc.sync_to_github(made[901], 901)
check("未配置远端时返回可读错误", r5.get("ok") is False and "MEM_GITHUB_REMOTE" in r5.get("error", ""), str(r5))

# ---- 7. 远端不存在时不崩、返回 stderr ----
settings.github_remote = str(TMP / "does-not-exist.git")
r6 = gitsvc.sync_to_github(made[901], 901)
check("远端不存在时返回错误而非崩溃", r6.get("ok") is False and bool(r6.get("error")), str(r6)[:200])

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAIL:
    print(f"❌ {len(FAIL)} 项失败: {FAIL}")
    sys.exit(1)
print("✅ GitHub 备份链路全部通过")
