from app import gitsvc


def test_cross_branch_commit_failure_is_structured(tmp_path, monkeypatch, caplog):
    root = gitsvc.ensure_repo(str(tmp_path), 123)
    def fail(*a, **kw): raise RuntimeError('index unavailable')
    monkeypatch.setattr(gitsvc, '_git_env', fail)
    try:
        result = gitsvc.commit_file_to_branch(root, 'other', 'main/doc.md', 'body', 'save')
    except RuntimeError:
        result = None
    assert isinstance(result, dict)
    assert result['ok'] is False
    assert 'index unavailable' in caplog.text


def test_commit_failure_is_structured_and_logged(tmp_path, monkeypatch, caplog):
    def fail(*a, **kw):
        raise RuntimeError('disk full')
    monkeypatch.setattr(gitsvc, 'commit_all', fail)
    result = gitsvc.try_commit_all(tmp_path, 'save')
    assert isinstance(result, dict)
    assert result['ok'] is False
    assert result['status'] == 'failed'
    assert 'disk full' in caplog.text


def test_branch_switch_stops_when_autocommit_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(gitsvc, 'list_branches', lambda p: {'branches': [{'name': 'other'}]})
    monkeypatch.setattr(gitsvc, 'try_commit_all', lambda *a: {'ok': False, 'status': 'failed', 'error': 'full'})
    calls = []
    monkeypatch.setattr(gitsvc, '_git', lambda *a, **kw: calls.append(a))
    result = gitsvc.switch_branch(tmp_path, 'other')
    assert result['ok'] is False
    assert calls == []
