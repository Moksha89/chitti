import hmac
import ipaddress
import json
import os
import secrets
import string
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


@dataclass
class Session:
    username: str | None
    csrf_token: str
    expires_at: float


@dataclass
class LoginAttempt:
    failures: int
    locked_until: float
    last_failure_at: float


class AuthManager:
    quiet_decay_seconds = 300
    max_lock_seconds = 3600
    dummy_password_hash = (
        "$argon2id$v=19$m=65536,t=3,p=4$5HePR5hva2KHIZo/nliEiw$"
        "zKtUyhCrN+sHqp3DaFDx+HOts1eXG442pfcpMko1BPE"
    )

    def __init__(
        self,
        username: str,
        password_hash: str,
        state_path: str,
        session_ttl_minutes: int = 480,
        trusted_proxy_ip: str = "172.31.250.2",
    ) -> None:
        self.username = username
        self.password_hash = password_hash
        self.state_path = Path(state_path)
        self.bootstrap_password_path = self.state_path.with_name("bootstrap_password.txt")
        self.session_ttl_seconds = session_ttl_minutes * 60
        self.must_change_password = True
        self.sessions: dict[str, Session] = {}
        self.attempts: dict[str, LoginAttempt] = {}
        self.hasher = PasswordHasher()
        self.trusted_proxy_ip = ipaddress.ip_address(trusted_proxy_ip)

    def initialize(self) -> None:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.password_hash = str(state["password_hash"])
            self.must_change_password = bool(state["must_change_password"])
            return
        if not self.password_hash:
            alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+="
            password = "".join(secrets.choice(alphabet) for _ in range(32))
            self.password_hash = self.hasher.hash(password)
            self.bootstrap_password_path.parent.mkdir(parents=True, exist_ok=True)
            self.bootstrap_password_path.write_text(password, encoding="utf-8")
            os.chmod(self.bootstrap_password_path, 0o600)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_state()

    def _save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "password_hash": self.password_hash,
                    "must_change_password": self.must_change_password,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.state_path, 0o600)

    def create_session(self, username: str | None = None) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(
            username=username,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.time() + self.session_ttl_seconds,
        )
        self.sessions[token] = session
        return token, session

    def get_session(self, token: str | None) -> Session | None:
        if not token:
            return None
        session = self.sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= time.time():
            self.sessions.pop(token, None)
            return None
        return session

    def delete_session(self, token: str | None) -> None:
        if token:
            self.sessions.pop(token, None)

    def _is_locked(self, client_key: str) -> bool:
        attempt = self.attempts.get(client_key)
        now = time.time()
        if attempt is None:
            return False
        if now - attempt.last_failure_at > self.quiet_decay_seconds:
            self.attempts.pop(client_key, None)
            return False
        return attempt.locked_until > now

    def _record_failure(self, client_key: str) -> None:
        now = time.time()
        attempt = self.attempts.get(client_key, LoginAttempt(0, 0, 0))
        if now - attempt.last_failure_at > self.quiet_decay_seconds:
            attempt = LoginAttempt(0, 0, 0)
        failures = attempt.failures + 1
        lock_seconds = min(self.max_lock_seconds, 60 * 2 ** max(0, failures - 5))
        self.attempts[client_key] = LoginAttempt(failures, now + lock_seconds if failures >= 5 else 0, now)

    def authenticate(self, username: str, password: str, client_key: str) -> bool:
        if self._is_locked(client_key):
            return False
        valid_username = hmac.compare_digest(username, self.username)
        hash_to_verify = self.password_hash if valid_username else self.dummy_password_hash
        valid_password = False
        try:
            valid_password = self.hasher.verify(hash_to_verify, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            pass
        if not (valid_username and valid_password):
            self._record_failure(client_key)
            return False
        self.attempts.pop(client_key, None)
        return True

    def change_password(self, password: str) -> None:
        self.password_hash = self.hasher.hash(password)
        self.must_change_password = False
        self._save_state()
        self.bootstrap_password_path.unlink(missing_ok=True)

    def csrf_valid(self, session: Session, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(session.csrf_token, token or "")

    def rotate_authenticated_session(self, old_token: str, username: str) -> tuple[str, Session]:
        self.delete_session(old_token)
        return self.create_session(username)

    def is_trusted_proxy(self, request: Any) -> bool:
        peer = request.client.host if request.client else ""
        try:
            return ipaddress.ip_address(peer) == self.trusted_proxy_ip
        except ValueError:
            return False

    def client_key(self, request: Any) -> str:
        peer = request.client.host if request.client else "unknown"
        if not self.is_trusted_proxy(request):
            return peer
        forwarded = str(request.headers.get("X-Forwarded-For", ""))
        chain = [item.strip() for item in forwarded.split(",") if item.strip()]
        for candidate in reversed(chain):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if candidate != str(self.trusted_proxy_ip):
                return candidate
        return peer
