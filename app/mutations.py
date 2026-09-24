"""Per-user repository mutations: task-reentrant, cross-worker advisory locks.

Lock files live outside user repositories and are never unlinked (unlinking an
active flock inode would allow a second owner). All workers must share data_dir.
"""
import asyncio
import errno
from contextlib import asynccontextmanager
from pathlib import Path
from functools import wraps

from .config import settings

try:
    import fcntl
except ImportError:  # Windows: lock one byte via the standard-library fallback.
    fcntl = None
    import msvcrt

# Task identity, not ContextVar inheritance: child tasks must not inherit ownership.
_owners = {}


def _try_lock(fd):
    if fcntl is not None:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(fd):
    if fcntl is not None:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    else:
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)


def finish_mutation(fn):
    """Finish multi-step Git+index changes before propagating client cancellation."""
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        task = asyncio.create_task(fn(*args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    try:
                        task.result()
                    except BaseException:
                        pass
                    raise
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            raise asyncio.CancelledError()
        return result
    return wrapped


async def run_blocking(fn, /, *args, **kwargs):
    """Offload blocking work, but keep the caller's lock until it really stops.

    Cancelling asyncio.to_thread doesn't stop its worker. Shield and drain it
    before propagating cancellation, otherwise another request can enter the
    same repository while a cancelled push/checkout is still writing it.
    """
    worker = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            cancelled = True
            if worker.done():
                # Retrieve any exception to avoid an unobserved task failure.
                try:
                    worker.result()
                except BaseException:
                    pass
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


@asynccontextmanager
async def user_lock(user_id):
    directory = Path(settings.data_dir).resolve() / '.mutation-locks'
    key = (str(directory), int(user_id))
    task = asyncio.current_task()
    if _owners.get(key) is task:
        yield
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f'u{int(user_id)}.lock').open('a+b') as fd:
        if fd.tell() == 0:
            fd.write(b'0')
            fd.flush()
        while True:
            try:
                _try_lock(fd)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                    raise
                await asyncio.sleep(.02)
        _owners[key] = task
        try:
            yield
        finally:
            _owners.pop(key, None)
            _unlock(fd)
