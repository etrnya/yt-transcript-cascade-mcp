"""Cross-process advisory file lock (Windows msvcrt / POSIX fcntl) with deadline support."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import TracebackType

from ..domain.models import CascadeError, ErrorCode

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class CrossProcessFileLock:
    """Portable cross-process exclusive file lock without deleting the sentinel lockfile."""

    def __init__(
        self,
        lock_path: Path,
        *,
        deadline: float | None = None,
        timeout_sec: float = 30.0,
        poll_interval_sec: float = 0.05,
    ) -> None:
        self.lock_path = lock_path
        self.deadline = deadline if deadline is not None else (time.monotonic() + timeout_sec)
        self.poll_interval_sec = poll_interval_sec
        self._fd: int | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)
        try:
            # Ensure at least 1 byte exists so Windows msvcrt.locking locks byte 0 reliably
            if os.fstat(fd).st_size == 0:
                try:
                    os.write(fd, b"0")
                except OSError:
                    pass

            while True:
                try:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = fd
                    return
                except OSError:
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise CascadeError(
                            ErrorCode.TIMEOUT,
                            f"Timed out waiting for cross-process lock: {self.lock_path.name}",
                        )
                    time.sleep(min(self.poll_interval_sec, remaining))
        except Exception:
            os.close(fd)
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> CrossProcessFileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()
