from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import StoreError

try:  # POSIX, including macOS and Linux
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes_atomic(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            os.chmod(path, mode)
        _fsync_dir(parent)
    except Exception as exc:
        if fd is not None:
            os.close(fd)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise StoreError(f"не удалось атомарно записать {path}: {exc}") from exc


def write_text_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    write_bytes_atomic(path, text.encode("utf-8"), mode=mode)


@contextmanager
def exclusive_lock(lock_path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Cross-process best-effort exclusive lock.

    Modal itself does not participate in this lock, but all mutating
    modal-cockpit operations do. The timeout prevents a permanently stuck CLI
    when another cockpit process dies while performing a long external action.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        raise StoreError("fcntl недоступен: modal-cockpit поддерживает только POSIX-системы")
    handle = lock_path.open("a+b")
    locked = False
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreError(
                        f"другой процесс modal-cockpit удерживает lock {lock_path} "
                        f"дольше {timeout_seconds:.0f} секунд"
                    )
                time.sleep(0.025)
        yield
    finally:
        if locked and fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
