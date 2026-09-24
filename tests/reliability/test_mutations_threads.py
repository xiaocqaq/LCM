import asyncio
import threading
from types import SimpleNamespace
from app import service
from test_storage_transactions import Session


def test_upsert_serializes_same_external_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, 'data_dir', str(tmp_path))
    monkeypatch.setattr(service.gitsvc, 'ensure_repo', lambda *a: tmp_path)
    active = peak = 0
    class SlowSession(Session):
        async def execute(self, *a, **kw):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
            return await super().execute(*a, **kw)
    async def run():
        await asyncio.gather(*(service.upsert_user_from_xiaoai(SlowSession(), {'id': 77}) for _ in range(3)))
    asyncio.run(run())
    assert peak == 1


def test_user_repo_initialization_runs_off_event_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, 'data_dir', str(tmp_path))
    main_thread = threading.get_ident()
    threads = []
    monkeypatch.setattr(service.gitsvc, 'ensure_repo', lambda *a: threads.append(threading.get_ident()))
    asyncio.run(service.upsert_user_from_xiaoai(Session(), {'id': 72, 'username': 'fixture'}))
    assert threads and all(t != main_thread for t in threads)


def test_branch_git_operations_run_off_event_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, 'data_dir', str(tmp_path))
    main_thread = threading.get_ident()
    threads = []
    def call(result):
        def run(*a):
            threads.append(threading.get_ident())
            return result
        return run
    monkeypatch.setattr(service.gitsvc, 'ensure_repo', call(tmp_path))
    monkeypatch.setattr(service.gitsvc, 'current_branch', call('main'))
    monkeypatch.setattr(service.gitsvc, 'read_file_from_branch', call(None))
    monkeypatch.setattr(service.gitsvc, 'commit_file_to_branch', call({'ok': True}))
    asyncio.run(service.write_to_branch(Session(), SimpleNamespace(id=1), {'title': 'branch', 'content': 'body'}, 'other'))
    assert len(threads) == 4
    assert all(t != main_thread for t in threads)


def test_failed_branch_write_has_no_success_note(tmp_path, monkeypatch):
    monkeypatch.setattr(service.settings, 'data_dir', str(tmp_path))
    monkeypatch.setattr(service.gitsvc, 'ensure_repo', lambda *a: tmp_path)
    monkeypatch.setattr(service.gitsvc, 'current_branch', lambda *a: 'main')
    monkeypatch.setattr(service.gitsvc, 'read_file_from_branch', lambda *a: None)
    monkeypatch.setattr(service.gitsvc, 'commit_file_to_branch', lambda *a: {'ok': False, 'error': 'full'})
    result = asyncio.run(service.write_to_branch(Session(), SimpleNamespace(id=1), {'content': 'body'}, 'other'))
    assert result['ok'] is False
    assert '已提交' not in result.get('note', '')
