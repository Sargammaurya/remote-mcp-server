"""
Bearer-token authentication and per-tool authorization.

Tokens look like `bmcp_<token_id>_<secret>`. Only the SHA-256 of the whole
token is stored (the secret is 256 random bits, so a slow hash adds nothing),
in a JSON file that must be mode 0600. The file is re-read whenever its
mtime/size changes, so `admin revoke` or an expiry takes effect on the next
request without a restart.

Two checks, both against the current file:
  1. TokenStoreVerifier.verify_token — called by the MCP library's bearer
     middleware on every HTTP request; unknown, revoked or expired tokens get
     HTTP 401 before any MCP handling.
  2. caller_for_tool — called by every tool with the token of the request
     that carried that tool call; refuses tools not in the token's list.
The user id always comes from the token record, never from tool arguments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.provider import AccessToken

from .paths import user_root  # validates user ids

REQUIRED_SCOPE = "bmcp"
_TOKEN = re.compile(r"bmcp_([0-9a-f]{12})_([A-Za-z0-9_-]{43})")
_TOKEN_ID = re.compile(r"[0-9a-f]{12}")

audit = logging.getLogger("bruin_mcp_service.audit")


class AuthError(PermissionError):
    """The caller may not do this. The message is safe to show the caller."""


class TokenFileError(RuntimeError):
    """The token file is missing, unreadable, malformed or too permissive."""


@dataclass(frozen=True)
class TokenRecord:
    token_id: str
    user_id: str
    sha256: str
    tools: frozenset[str]
    created_at: int
    expires_at: int
    revoked_at: int | None = None

    def to_json(self) -> dict:
        return {
            "token_id": self.token_id, "user_id": self.user_id, "sha256": self.sha256,
            "tools": sorted(self.tools), "created_at": self.created_at,
            "expires_at": self.expires_at, "revoked_at": self.revoked_at,
        }

    @classmethod
    def from_json(cls, d: dict) -> "TokenRecord":
        user_root(Path("/"), d["user_id"])  # raises on a malformed id
        if not _TOKEN_ID.fullmatch(d["token_id"]):
            raise ValueError(f"bad token_id {d['token_id']!r}")
        return cls(
            token_id=d["token_id"], user_id=d["user_id"], sha256=d["sha256"],
            tools=frozenset(d["tools"]), created_at=int(d["created_at"]),
            expires_at=int(d["expires_at"]),
            revoked_at=None if d.get("revoked_at") is None else int(d["revoked_at"]),
        )


@dataclass(frozen=True)
class Caller:
    user_id: str
    token_id: str
    tools: frozenset[str]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """Return (token_id, token). The token is shown once and never stored."""
    token_id = secrets.token_hex(6)
    return token_id, f"bmcp_{token_id}_{secrets.token_urlsafe(32)}"


def check_file_mode(path: Path) -> None:
    st = os.stat(path)
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise TokenFileError(f"{path} must not be readable by group/others (chmod 600)")
    if st.st_uid != os.getuid():
        raise TokenFileError(f"{path} must be owned by the service user")


def read_records(path: Path) -> dict[str, TokenRecord]:
    try:
        check_file_mode(path)
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        records = [TokenRecord.from_json(d) for d in raw["tokens"]]
    except TokenFileError:
        raise
    except FileNotFoundError:
        raise TokenFileError(f"token file {path} does not exist (run the admin `init` command)") from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TokenFileError(f"cannot load token file {path}: {exc}") from exc
    return {r.token_id: r for r in records}


def write_records(path: Path, records: dict[str, TokenRecord]) -> None:
    """Atomically replace the token file (0600)."""
    path = Path(path)
    data = json.dumps({"tokens": [r.to_json() for r in records.values()]}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".tokens.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class TokenStore:
    """Read-only view of the token file, reloaded when the file changes."""

    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._stamp = None
        self._records: dict[str, TokenRecord] = {}
        self._reload_if_changed()  # fail at startup, not on the first request

    def _reload_if_changed(self) -> None:
        st = os.stat(self.path)
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        with self._lock:
            if stamp != self._stamp:
                self._records = read_records(self.path)
                self._stamp = stamp

    def lookup(self, token: str) -> tuple[TokenRecord | None, str]:
        """Return (record, reason). record is None when the token is not usable."""
        m = _TOKEN.fullmatch(token or "")
        if not m:
            return None, "malformed"
        try:
            self._reload_if_changed()
        except (OSError, TokenFileError) as exc:
            audit.error("token file unavailable; refusing all tokens", extra={"error": str(exc)})
            return None, "token_file_unavailable"
        record = self._records.get(m.group(1))
        # compare even when unknown, so timing does not reveal valid token ids
        expected = record.sha256 if record else "0" * 64
        if not hmac.compare_digest(hash_token(token), expected) or record is None:
            return None, "unknown"
        if record.revoked_at is not None:
            return None, "revoked"
        if record.expires_at <= self._clock():
            return None, "expired"
        return record, "ok"

    def record(self, token_id: str) -> TokenRecord | None:
        self._reload_if_changed()
        return self._records.get(token_id)


class TokenStoreVerifier:
    """mcp.server.auth.provider.TokenVerifier backed by a TokenStore."""

    def __init__(self, store: TokenStore, resource: str):
        self.store = store
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        record, reason = self.store.lookup(token)
        if record is None:
            m = _TOKEN.fullmatch(token or "")
            audit.warning("auth_rejected", extra={
                "event": "auth_rejected", "reason": reason,
                "token_id": m.group(1) if m else None,
            })
            return None
        # token= carries only the id: the secret is not kept in request state.
        # client_id/subject identify the principal; the MCP library refuses to
        # serve a session to any other principal than the one that created it.
        return AccessToken(
            token=record.token_id,
            client_id=record.token_id,
            subject=record.user_id,
            scopes=[REQUIRED_SCOPE],
            expires_at=record.expires_at,
            resource=self.resource,
        )


def caller_for_tool(store: TokenStore, access: AccessToken | None, tool: str) -> Caller:
    """Re-check the request's token against the current file and the tool list."""
    if access is None:
        raise AuthError("not authenticated")
    record = store.record(access.client_id)
    if record is None or record.revoked_at is not None or record.expires_at <= store._clock():
        raise AuthError("token is no longer valid")
    if record.user_id != access.subject:
        raise AuthError("token identity mismatch")
    if tool not in record.tools:
        raise AuthError(f"this token is not authorized to call {tool!r}")
    return Caller(user_id=record.user_id, token_id=record.token_id, tools=record.tools)
