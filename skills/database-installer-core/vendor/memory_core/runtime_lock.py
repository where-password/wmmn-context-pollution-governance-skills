from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WorkerLockError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class WorkerProcessLock:
    """OS-backed single-process lock with auditable JSON metadata."""

    def __init__(
        self,
        path: str | Path,
        *,
        owner: str,
        runtime_key: str,
        heartbeat_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path).resolve()
        self.owner = owner
        self.runtime_key = runtime_key
        self.heartbeat_seconds = heartbeat_seconds
        self._handle = None
        self._locked = False
        self.metadata: dict[str, Any] = {}
        self.stale_recovered = False

    def _lock_handle(self, handle) -> None:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_handle(self, handle) -> None:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_handle(handle) -> dict[str, Any]:
        handle.seek(0)
        raw = handle.read()
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _write_handle(self, handle, metadata: dict[str, Any]) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())

    def acquire(self) -> "WorkerProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        handle = self.path.open("r+", encoding="utf-8")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            try:
                self._lock_handle(handle)
            except OSError as exc:
                try:
                    previous = self._read_handle(handle)
                except OSError:
                    previous = {}
                raise WorkerLockError(
                    f"worker lock already held: {self.path}; owner={previous.get('owner')} pid={previous.get('pid')}"
                ) from exc
            previous = self._read_handle(handle)
            previous_pid = int(previous.get("pid") or 0)
            self.stale_recovered = bool(previous) and not _pid_alive(previous_pid)
            now = _utc_now()
            self.metadata = {
                "status": "running",
                "pid": os.getpid(),
                "owner": self.owner,
                "runtime_key": self.runtime_key,
                "started_at": now,
                "heartbeat": now,
                "lock_path": str(self.path),
                "previous": previous or None,
                "stale_recovered": self.stale_recovered,
            }
            self._write_handle(handle, self.metadata)
            self._handle = handle
            self._locked = True
            return self
        except WorkerLockError:
            handle.close()
            raise
        except Exception:
            try:
                self._unlock_handle(handle)
            except Exception:
                pass
            handle.close()
            raise

    def heartbeat(self) -> dict[str, Any]:
        if not self._locked or self._handle is None:
            raise WorkerLockError("worker lock is not held")
        self.metadata["heartbeat"] = _utc_now()
        self._write_handle(self._handle, self.metadata)
        return dict(self.metadata)

    def release(self) -> dict[str, Any]:
        if self._locked and self._handle is not None:
            self.metadata["status"] = "released"
            self.metadata["released_at"] = _utc_now()
            self.metadata["heartbeat"] = self.metadata["released_at"]
            try:
                self._write_handle(self._handle, self.metadata)
            finally:
                try:
                    self._unlock_handle(self._handle)
                finally:
                    self._handle.close()
                    self._locked = False
                    self._handle = None
        return dict(self.metadata)

    def __enter__(self) -> "WorkerProcessLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
