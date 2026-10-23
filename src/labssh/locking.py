"""Small cross-process locks and atomic JSON writes."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import BinaryIO, Self


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: BinaryIO | None = None

    def try_acquire(self) -> bool:
        if self.stream is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if not self.path.stat().st_size:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self.stream = stream
        return True

    def acquire(self, stop: threading.Event | None = None) -> bool:
        stop = stop if stop is not None else threading.Event()
        while not stop.is_set():
            if self.try_acquire():
                return True
            stop.wait(0.05)
        return False

    def release(self) -> None:
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def atomic_json(path: Path, value: object) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, allow_nan=False)
            stream.flush()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
