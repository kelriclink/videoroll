from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from videoroll.apps.publish_gateway import SUPPORTED_SOCIAL_PLATFORMS, normalize_publish_platform
from videoroll.apps.social_publisher.account_store import canonicalize_storage_state, upsert_account, validate_account_name
from videoroll.apps.social_publisher.sau_cli import build_login_command
from videoroll.config import SocialPublisherSettings
from videoroll.db.session import db_session


ACTIVE_STATES = {"starting", "running", "canceling"}


def _tail(file_obj, max_bytes: int) -> str:
    file_obj.flush()
    size = file_obj.tell()
    file_obj.seek(max(0, size - max(1, max_bytes)))
    return file_obj.read().decode("utf-8", errors="replace").replace("\x00", "").strip()


@dataclass
class BrowserLoginSession:
    id: uuid.UUID
    platform: str
    account_name: str
    browser_url: str
    state: str = "starting"
    message: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)


class BrowserLoginManager:
    def __init__(self, settings: SocialPublisherSettings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._sessions: dict[uuid.UUID, BrowserLoginSession] = {}

    def start(self, platform: str, account_name: str) -> BrowserLoginSession:
        value = normalize_publish_platform(platform)
        if value not in SUPPORTED_SOCIAL_PLATFORMS:
            raise ValueError("unsupported social platform")
        safe_name = validate_account_name(account_name)
        with self._lock:
            if any(session.state in ACTIVE_STATES for session in self._sessions.values()):
                raise RuntimeError("another browser login session is already active")
            session = BrowserLoginSession(
                id=uuid.uuid4(),
                platform=value,
                account_name=safe_name,
                browser_url=self.settings.login_browser_url,
            )
            self._sessions[session.id] = session
        threading.Thread(target=self._run, args=(session.id,), daemon=True, name=f"social-login-{session.id}").start()
        return session

    def get(self, session_id: uuid.UUID) -> BrowserLoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def cancel(self, session_id: uuid.UUID) -> BrowserLoginSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.state in ACTIVE_STATES:
                session.state = "canceling"
                session.message = "login canceled"
                process = session.process
                if process is not None and process.poll() is None:
                    process.terminate()
            return session

    def _update(self, session_id: uuid.UUID, **changes: object) -> BrowserLoginSession:
        with self._lock:
            session = self._sessions[session_id]
            for key, value in changes.items():
                setattr(session, key, value)
            return session

    def _run(self, session_id: uuid.UUID) -> None:
        session = self.get(session_id)
        if session is None:
            return
        cookies_dir = Path(self.settings.sau_cookies_dir).resolve()
        cookies_dir.mkdir(parents=True, exist_ok=True)
        cookie_path = cookies_dir / f"{session.platform}_{session.account_name}.json"
        cookie_path.unlink(missing_ok=True)
        command = build_login_command(self.settings, session.platform, session.account_name)
        env = os.environ.copy()
        env["DISPLAY"] = self.settings.login_display
        env.setdefault("DOUYIN_COOKIE_AUTH_HEADLESS", "true")
        try:
            with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=self.settings.sau_runtime_dir,
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                )
                self._update(session_id, state="running", message="browser opened; complete login in the new window", process=process)
                timed_out = False
                try:
                    returncode = process.wait(timeout=max(1.0, self.settings.login_timeout_seconds))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.terminate()
                    try:
                        returncode = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        returncode = process.wait(timeout=5)
                stdout = _tail(stdout_file, self.settings.output_max_bytes)
                stderr = _tail(stderr_file, self.settings.output_max_bytes)

            current = self.get(session_id)
            if current is None or current.state == "canceling":
                self._update(session_id, state="canceled", finished_at=datetime.now(timezone.utc), process=None)
                return
            if timed_out:
                self._update(
                    session_id,
                    state="failed",
                    message="login timed out",
                    finished_at=datetime.now(timezone.utc),
                    process=None,
                )
                return
            if returncode != 0 or not cookie_path.is_file():
                detail = (stderr or stdout or f"SAU login failed with exit={returncode}")[-1000:]
                self._update(
                    session_id,
                    state="failed",
                    message=detail,
                    finished_at=datetime.now(timezone.utc),
                    process=None,
                )
                return

            canonical = canonicalize_storage_state(cookie_path.read_bytes())
            db = next(db_session(self.settings.database_url))
            try:
                account = upsert_account(db, session.platform, session.account_name, canonical)
                account.check_state = "valid"
                account.last_checked_at = datetime.now(timezone.utc)
                account.last_check_message = "browser login completed"
                db.add(account)
                db.commit()
            finally:
                db.close()
            self._update(
                session_id,
                state="succeeded",
                message="login succeeded and account was saved",
                finished_at=datetime.now(timezone.utc),
                process=None,
            )
        except Exception as exc:
            self._update(
                session_id,
                state="failed",
                message=(str(exc) or type(exc).__name__)[:1000],
                finished_at=datetime.now(timezone.utc),
                process=None,
            )
        finally:
            cookie_path.unlink(missing_ok=True)
