"""
Token administration, run by the VM owner as the service user:

    python -m bruin_mcp_service.admin init
    python -m bruin_mcp_service.admin issue --user alice --tools health,list_datasets
    python -m bruin_mcp_service.admin issue --user alice --all-tools [--days 90]
    python -m bruin_mcp_service.admin revoke <token_id>
    python -m bruin_mcp_service.admin list

`issue` prints the token once; only its hash is stored. Uses BMCP_TOKEN_FILE
from the environment (see .env.example). A running service picks up changes
on its next request.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .auth import TokenRecord, hash_token, new_token, read_records, write_records
from .paths import PathError, user_root
from .tools import TOOL_NAMES

MAX_DAYS = 90


@contextmanager
def _locked(path: Path):
    """Serialise admin commands so two concurrent edits cannot lose one."""
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _token_file() -> Path:
    raw = os.environ.get("BMCP_TOKEN_FILE", "").strip()
    if not raw or not os.path.isabs(raw):
        sys.exit("BMCP_TOKEN_FILE must be set to an absolute path")
    return Path(raw)


def _ts(epoch: int | None) -> str:
    return "-" if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def issue(path: Path, user_id: str, tools: frozenset[str], days: int, now: float | None = None) -> str:
    user_root(Path("/"), user_id)
    unknown = tools - TOOL_NAMES
    if unknown or not tools:
        raise ValueError(f"unknown tools {sorted(unknown)}; known: {sorted(TOOL_NAMES)}")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--days must be 1..{MAX_DAYS}")
    now = int(time.time() if now is None else now)
    with _locked(path):
        records = read_records(path)
        token_id, token = new_token()
        while token_id in records:
            token_id, token = new_token()
        records[token_id] = TokenRecord(
            token_id=token_id, user_id=user_id, sha256=hash_token(token), tools=tools,
            created_at=now, expires_at=now + days * 86400,
        )
        write_records(path, records)
    return token


def revoke(path: Path, token_id: str, now: float | None = None) -> bool:
    with _locked(path):
        records = read_records(path)
        record = records.get(token_id)
        if record is None:
            return False
        if record.revoked_at is None:
            records[token_id] = TokenRecord(**{**record.__dict__, "revoked_at": int(time.time() if now is None else now)})
            write_records(path, records)
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bruin_mcp_service.admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create an empty token file (0600)")
    p_issue = sub.add_parser("issue", help="issue a token and print it once")
    p_issue.add_argument("--user", required=True)
    group = p_issue.add_mutually_exclusive_group(required=True)
    group.add_argument("--tools", help="comma-separated tool names")
    group.add_argument("--all-tools", action="store_true")
    p_issue.add_argument("--days", type=int, default=MAX_DAYS)
    p_revoke = sub.add_parser("revoke", help="revoke a token by id")
    p_revoke.add_argument("token_id")
    sub.add_parser("list", help="list tokens (never shows secrets)")
    args = parser.parse_args(argv)
    path = _token_file()

    try:
        if args.cmd == "init":
            if path.exists():
                sys.exit(f"{path} already exists")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_records(path, {})
            print(f"created {path}")
        elif args.cmd == "issue":
            tools = TOOL_NAMES if args.all_tools else frozenset(t.strip() for t in args.tools.split(",") if t.strip())
            token = issue(path, args.user, tools, args.days)
            print(token)
            print("store this token now; it cannot be shown again", file=sys.stderr)
        elif args.cmd == "revoke":
            if not revoke(path, args.token_id):
                sys.exit(f"no token {args.token_id}")
            print(f"revoked {args.token_id}")
        elif args.cmd == "list":
            for r in read_records(path).values():
                state = "revoked" if r.revoked_at else ("expired" if r.expires_at <= time.time() else "active")
                print(f"{r.token_id}  {r.user_id:<16} {state:<8} expires {_ts(r.expires_at)}  "
                      f"tools={','.join(sorted(r.tools))}")
    except (ValueError, PathError) as exc:
        sys.exit(str(exc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
