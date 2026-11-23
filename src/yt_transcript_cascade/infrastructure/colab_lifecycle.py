"""Persistent ColabRuntimeLifecycleManager with owner_id isolation, Atomic Write, and 3-strike STOP_FAILED policy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from ..domain.models import CascadeError, ErrorCode
from .file_lock import CrossProcessFileLock

SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_CLEANUP_RETRIES = 3
DEFAULT_COOLDOWN_SEC = 15 * 60  # 15 minutes (PRD v1.2.2 §4.2)

SessionStatus = Literal[
    "CREATED",
    "ACTIVE",
    "CLEANUP_PENDING",
    "STOPPING",
    "STOPPED",
    "STOP_FAILED",
    "RETRY_PENDING",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def default_owner_id() -> str:
    host = socket.gethostname() or "local"
    host_hash = hashlib.sha256(host.encode("utf-8")).hexdigest()[:12]
    return f"yt-cascade-mcp:{host_hash}"


def validate_session_id(session_id: str) -> str:
    """Enforce strict session_id whitelist before persisting or passing to CLI."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise CascadeError(
            ErrorCode.INVALID_INPUT,
            "Invalid Colab session_id format (must match ^[a-zA-Z0-9_-]{1,64}$).",
        )
    return session_id


class ColabSessionRecord(BaseModel):
    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    owner_id: str
    created_at: str
    last_activity_at: str
    status: SessionStatus
    cleanup_required: bool
    retry_count: int = Field(default=0, ge=0)
    last_error: str | None = None


class ColabSessionStoreSchema(BaseModel):
    schema_version: str = "1.2.2"
    sessions: list[ColabSessionRecord] = Field(default_factory=list)


def _default_stop_session(session_id: str) -> tuple[bool, str | None, bool]:
    """Stop a Colab session via `colab` CLI. Returns (stopped_ok, error_msg, not_found)."""
    validate_session_id(session_id)
    try:
        res = subprocess.run(
            ["colab", "stop", session_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except FileNotFoundError:
        return False, "colab CLI not found on PATH.", False
    except subprocess.TimeoutExpired:
        return False, "colab stop timed out after 30s.", False
    except Exception as exc:
        return False, f"colab stop failed: {type(exc).__name__}", False

    combined = f"{res.stdout}\n{res.stderr}".lower()
    if res.returncode == 0:
        return True, None, False
    if any(k in combined for k in ("not found", "does not exist", "no such session", "404")):
        return True, None, True
    return False, (res.stderr or res.stdout or f"exit code {res.returncode}").strip()[-300:], False


class ColabRuntimeLifecycleManager:
    """Manages Colab session persistence, cleanup retries, and startup recovery (PRD v1.2.2 §4.2)."""

    def __init__(
        self,
        state_path: Path | None = None,
        *,
        owner_id: str | None = None,
        cooldown_sec: int = DEFAULT_COOLDOWN_SEC,
        stop_fn: Callable[[str], tuple[bool, str | None, bool]] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_path = (
            state_path
            if state_path is not None
            else Path.home() / ".cache" / "yt-transcript-cascade" / "colab_sessions.json"
        )
        self.lock_path = self.state_path.with_suffix(".lock")
        self.owner_id = owner_id or default_owner_id()
        self.cooldown_sec = cooldown_sec
        self._stop_fn = stop_fn or _default_stop_session
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _load_unlocked(self) -> ColabSessionStoreSchema:
        if not self.state_path.exists():
            return ColabSessionStoreSchema()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return ColabSessionStoreSchema.model_validate(raw)
        except Exception:
            return ColabSessionStoreSchema()

    def _save_unlocked(self, store: ColabSessionStoreSchema) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(store.model_dump_json(indent=2), encoding="utf-8")
            if os.name != "nt":
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
            os.replace(tmp, self.state_path)
            if os.name != "nt":
                try:
                    os.chmod(self.state_path, 0o600)
                except OSError:
                    pass
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def list_sessions(self) -> list[ColabSessionRecord]:
        with CrossProcessFileLock(self.lock_path, timeout_sec=10.0):
            return list(self._load_unlocked().sessions)

    def register_session(self, session_id: str, *, status: SessionStatus = "CREATED") -> ColabSessionRecord:
        validate_session_id(session_id)
        now_iso = self._now_fn().isoformat()
        with CrossProcessFileLock(self.lock_path, timeout_sec=10.0):
            store = self._load_unlocked()
            rec = ColabSessionRecord(
                session_id=session_id,
                owner_id=self.owner_id,
                created_at=now_iso,
                last_activity_at=now_iso,
                status=status,
                cleanup_required=True,
                retry_count=0,
                last_error=None,
            )
            store.sessions = [s for s in store.sessions if s.session_id != session_id]
            store.sessions.append(rec)
            self._save_unlocked(store)
            return rec

    def update_status(
        self,
        session_id: str,
        status: SessionStatus,
        *,
        cleanup_required: bool | None = None,
        last_error: str | None = None,
    ) -> ColabSessionRecord | None:
        validate_session_id(session_id)
        now_iso = self._now_fn().isoformat()
        with CrossProcessFileLock(self.lock_path, timeout_sec=10.0):
            store = self._load_unlocked()
            for idx, rec in enumerate(store.sessions):
                if rec.session_id == session_id:
                    updated = rec.model_copy(
                        update={
                            "status": status,
                            "last_activity_at": now_iso,
                            "cleanup_required": rec.cleanup_required if cleanup_required is None else cleanup_required,
                            "last_error": rec.last_error if last_error is None else last_error,
                        }
                    )
                    store.sessions[idx] = updated
                    self._save_unlocked(store)
                    return updated
        return None

    def mark_cleanup_pending(self, session_id: str, *, reason: str | None = None) -> ColabSessionRecord | None:
        """Persist CLEANUP_PENDING immediately so deadline/cancellation never drops remote cleanup."""
        return self.update_status(
            session_id,
            "CLEANUP_PENDING",
            cleanup_required=True,
            last_error=reason,
        )

    def cleanup_session(self, session_id: str, *, max_attempts_this_call: int = MAX_CLEANUP_RETRIES) -> ColabSessionRecord | None:
        """Run stop attempts up to MAX_CLEANUP_RETRIES; never delete or fake-STOPPED on exhaustion."""
        validate_session_id(session_id)
        with CrossProcessFileLock(self.lock_path, timeout_sec=10.0):
            store = self._load_unlocked()
            idx = next((i for i, s in enumerate(store.sessions) if s.session_id == session_id), None)
            if idx is None:
                return None

            rec = store.sessions[idx]
            if rec.owner_id != self.owner_id:
                return rec
            if rec.status == "STOPPED":
                return rec

            attempts_used = 0
            while attempts_used < max_attempts_this_call and rec.retry_count < MAX_CLEANUP_RETRIES:
                rec = rec.model_copy(update={"status": "STOPPING", "last_activity_at": self._now_fn().isoformat()})
                store.sessions[idx] = rec
                self._save_unlocked(store)

                ok, err_msg, _not_found = self._stop_fn(session_id)
                attempts_used += 1
                now_iso = self._now_fn().isoformat()
                if ok:
                    rec = rec.model_copy(
                        update={
                            "status": "STOPPED",
                            "cleanup_required": False,
                            "last_activity_at": now_iso,
                            "last_error": None,
                        }
                    )
                    store.sessions[idx] = rec
                    self._save_unlocked(store)
                    return rec

                new_retries = rec.retry_count + 1
                next_status: SessionStatus = (
                    "RETRY_PENDING"
                    if (new_retries < MAX_CLEANUP_RETRIES and attempts_used < max_attempts_this_call)
                    else "STOP_FAILED"
                )
                rec = rec.model_copy(
                    update={
                        "status": next_status,
                        "cleanup_required": True,
                        "retry_count": new_retries,
                        "last_activity_at": now_iso,
                        "last_error": err_msg or "Unknown colab stop failure.",
                    }
                )
                store.sessions[idx] = rec
                self._save_unlocked(store)

            return rec

    def recover_pending_sessions(self) -> list[str]:
        """Startup recovery: clean owned pending sessions; STOP_FAILED gets at most 1 cooldown retry (never resets retry_count)."""
        warnings: list[str] = []
        now = self._now_fn()

        with CrossProcessFileLock(self.lock_path, timeout_sec=10.0):
            store = self._load_unlocked()
            changed = False

            for idx, rec in enumerate(store.sessions):
                if rec.owner_id != self.owner_id:
                    continue
                if rec.status == "STOPPED":
                    continue

                if rec.status in ("CREATED", "ACTIVE", "CLEANUP_PENDING", "RETRY_PENDING", "STOPPING"):
                    # Run remaining retries up to MAX_CLEANUP_RETRIES
                    while rec.retry_count < MAX_CLEANUP_RETRIES:
                        ok, err_msg, _ = self._stop_fn(rec.session_id)
                        now_iso = self._now_fn().isoformat()
                        if ok:
                            rec = rec.model_copy(
                                update={
                                    "status": "STOPPED",
                                    "cleanup_required": False,
                                    "last_activity_at": now_iso,
                                    "last_error": None,
                                }
                            )
                            changed = True
                            break
                        new_retries = rec.retry_count + 1
                        rec = rec.model_copy(
                            update={
                                "status": "STOP_FAILED",
                                "cleanup_required": True,
                                "retry_count": new_retries,
                                "last_activity_at": now_iso,
                                "last_error": err_msg or "Startup recovery stop failed.",
                            }
                        )
                        changed = True
                    store.sessions[idx] = rec

                elif rec.status == "STOP_FAILED":
                    # PRD §4.2: NEVER reset retry_count. Allow at most 1 extra retry after cooldown if retry_count == 3.
                    elapsed = (now - _parse_iso(rec.last_activity_at)).total_seconds()
                    if rec.retry_count == MAX_CLEANUP_RETRIES and elapsed >= self.cooldown_sec:
                        ok, err_msg, _ = self._stop_fn(rec.session_id)
                        now_iso = self._now_fn().isoformat()
                        if ok:
                            rec = rec.model_copy(
                                update={
                                    "status": "STOPPED",
                                    "cleanup_required": False,
                                    "last_activity_at": now_iso,
                                    "last_error": None,
                                }
                            )
                        else:
                            rec = rec.model_copy(
                                update={
                                    "status": "STOP_FAILED",
                                    "cleanup_required": True,
                                    "retry_count": rec.retry_count + 1,
                                    "last_activity_at": now_iso,
                                    "last_error": err_msg or rec.last_error,
                                }
                            )
                        store.sessions[idx] = rec
                        changed = True

                if rec.status == "STOP_FAILED":
                    warnings.append(
                        f"Colab session '{rec.session_id}' is in STOP_FAILED (retries={rec.retry_count}). "
                        f"Please verify manually via `colab sessions`. Last error: {rec.last_error}"
                    )

            if changed:
                self._save_unlocked(store)

        return warnings

    def pending_session_count(self) -> int:
        return sum(
            1
            for s in self.list_sessions()
            if s.owner_id == self.owner_id and s.status != "STOPPED"
        )

    def get_stop_failed_warnings(self) -> list[str]:
        return [
            f"Colab session '{s.session_id}' is in STOP_FAILED (retries={s.retry_count}). "
            f"Verify manually via `colab sessions`. Last error: {s.last_error}"
            for s in self.list_sessions()
            if s.owner_id == self.owner_id and s.status == "STOP_FAILED"
        ]
