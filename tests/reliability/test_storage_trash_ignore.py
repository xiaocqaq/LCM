"""数据仓库的 .trash/ 必须不进 git —— 软删除的语义是"别再留着"。

背景：commit_all 用 `git add -A`，如果 .gitignore 不忽略 .trash/，软删除的
文档会照样被提交、被 sync 推到 GitHub 长期留存。用户以为删掉了，实际远端还能
翻出来。这些用例锁住"新仓库"和"老仓库"两条路径。
"""
from pathlib import Path

from app import gitsvc


def _tracked(root: Path) -> set[str]:
    out = gitsvc._git(root, 'ls-files', check=False)
    return {line for line in out.splitlines() if line}


def test_new_repo_ignores_trash(tmp_path):
    root = gitsvc.ensure_repo(str(tmp_path), 201)
    assert '.trash/' in (root / '.gitignore').read_text(encoding='utf-8')


def test_soft_deleted_doc_never_reaches_git(tmp_path):
    """端到端：写正文 → 提交 → 移到 .trash → 再提交，.trash 内容不该被跟踪。"""
    root = gitsvc.ensure_repo(str(tmp_path), 202)
    lib = root / 'main'
    lib.mkdir()
    doc = lib / 'note.md'
    doc.write_text('---\ntitle: note\n---\nbody\n', encoding='utf-8')
    gitsvc.commit_all(root, 'create: note')
    assert 'main/note.md' in _tracked(root)

    trash = root / '.trash'
    trash.mkdir()
    doc.rename(trash / 'note.md')
    gitsvc.commit_all(root, 'delete: note')

    tracked = _tracked(root)
    assert 'main/note.md' not in tracked
    assert not any(p.startswith('.trash/') for p in tracked), tracked


def test_existing_repo_gets_gitignore_backfilled(tmp_path):
    """老仓库（.gitignore 没有 .trash/）在 ensure_repo 时被补上。"""
    root = gitsvc.ensure_repo(str(tmp_path), 203)
    (root / '.gitignore').write_text('.DS_Store\nThumbs.db\n*.tmp\n', encoding='utf-8')
    gitsvc.ensure_repo(str(tmp_path), 203)
    assert '.trash/' in (root / '.gitignore').read_text(encoding='utf-8')


def test_backfill_preserves_user_added_rules(tmp_path):
    """补写不能整体覆盖 —— 用户自己加过的规则必须留住。"""
    root = gitsvc.ensure_repo(str(tmp_path), 204)
    (root / '.gitignore').write_text('.DS_Store\nmy-own-rule/\n', encoding='utf-8')
    gitsvc.ensure_repo(str(tmp_path), 204)
    text = (root / '.gitignore').read_text(encoding='utf-8')
    assert 'my-own-rule/' in text
    assert '.trash/' in text


def test_backfill_is_idempotent(tmp_path):
    """反复调用不重复追加。"""
    root = gitsvc.ensure_repo(str(tmp_path), 205)
    gitsvc.ensure_repo(str(tmp_path), 205)
    first = (root / '.gitignore').read_text(encoding='utf-8')
    gitsvc.ensure_repo(str(tmp_path), 205)
    assert (root / '.gitignore').read_text(encoding='utf-8') == first
