"""
JSON-lines logging with secret redaction.

Every record — message, arguments and `extra` fields — passes through
redact() before it is written, so a bearer token or password that reaches a
log call never reaches disk.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REDACTED = "[REDACTED]"

# a key ending in _id (token_id, user_id) names an identifier, not a secret;
# its value is still scanned by _PATTERNS
_SECRET_KEY = re.compile(r"(?i)(pass(word)?|secret|token|api[_-]?key|authorization)(?!.*_id$)")
_PATTERNS = [
    # "Authorization: Bearer abc" / "authorization=abc"
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;\"']+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    # key=value / key: value for secret-looking keys
    re.compile(r"(?i)((?:pass(?:word)?|secret|token|api[_-]?key)\s*[:=]\s*)[^\s,;\"']+"),
]

# attributes every LogRecord has; anything else came from `extra=`
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message"}


def redact(value):
    if isinstance(value, str):
        for pattern in _PATTERNS:
            value = pattern.sub(lambda m: m.group(1) + REDACTED, value)
        return value
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS:
                entry[key] = REDACTED if _SECRET_KEY.search(key) else redact(value)
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def configure_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """Log JSON lines to <log_dir>/service.log and stderr (never stdout)."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    formatter = JsonFormatter()

    logger = logging.getLogger("bruin_mcp_service")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    file_handler = logging.FileHandler(log_dir / "service.log", encoding="utf-8")
    stderr_handler = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, stderr_handler):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger
