"""One advisory lock shared by CLI, initialization, notes and sync adapter."""
import fcntl
import os
from pathlib import Path
import stat
import threading
from contextlib import contextmanager

_local = threading.local()
class Busy(Exception):
    pass

@contextmanager
def write_lock(database):
    path = Path(str(Path(database).resolve()) + '.lock')
    held = getattr(_local, 'held', {})
    _local.held = held
    if str(path) in held:
        yield held[str(path)]
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = None
    try:
        inherited = os.environ.get('REVIEW_LOCK_FD')
        if inherited is not None:
            fd = os.dup(int(inherited))
            a, b = os.fstat(fd), path.lstat()
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                raise ValueError('Invalid inherited lock')
        else:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Invalid lock permissions')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy() from None
        held[str(path)] = fd
        try:
            yield fd
        finally:
            held.pop(str(path), None)
    finally:
        if fd is not None:
            os.close(fd)
