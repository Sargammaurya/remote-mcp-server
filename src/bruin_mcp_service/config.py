"""
Service settings, read from the environment and validated once at startup.

Nothing machine-specific is hard-coded here: every path comes from an
environment variable (see .env.example). A setting that is missing or
malformed stops the service with a ConfigError naming the variable, rather
than falling back to a guess.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

SUPPORTED_INPUT_EXT = frozenset({".csv", ".xlsx"})
ENVIRONMENTS = ("dev", "prod")


class ConfigError(ValueError):
    """A setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    env: str
    bind_host: str
    port: int
    workspace_root: Path
    log_dir: Path
    bruin_bin: Path
    bruin_home: Path
    bruin_expected_version: str
    bruin_verified_file: Path
    max_upload_mb: int
    max_export_mb: int
    user_quota_gb: int
    allowed_input_ext: frozenset[str]
    public_url: str
    token_file: Path
    tmp_dir: Path
    tool_timeout_s: int
    user_concurrency: int
    max_concurrency: int
    run_timeout_s: int
    max_runs: int

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_export_bytes(self) -> int:
        return self.max_export_mb * 1024 * 1024

    @property
    def user_quota_bytes(self) -> int:
        return self.user_quota_gb * 1024 ** 3


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is not set")
    return value


def _abs_path(env: Mapping[str, str], key: str) -> Path:
    path = Path(_required(env, key))
    if not path.is_absolute():
        raise ConfigError(f"{key} must be an absolute path, got {str(path)!r}")
    return path


def _positive_int(env: Mapping[str, str], key: str, default: int,
                  maximum: int | None = None) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from None
    if value <= 0 or (maximum is not None and value > maximum):
        bound = f"1..{maximum}" if maximum is not None else "> 0"
        raise ConfigError(f"{key} must be {bound}, got {value}")
    return value


def _input_ext(env: Mapping[str, str]) -> frozenset[str]:
    raw = env.get("BMCP_ALLOWED_INPUT_EXT", "").strip()
    if not raw:
        return SUPPORTED_INPUT_EXT
    exts = frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
    unsupported = exts - SUPPORTED_INPUT_EXT
    if not exts or unsupported:
        raise ConfigError(
            "BMCP_ALLOWED_INPUT_EXT may only contain "
            f"{sorted(SUPPORTED_INPUT_EXT)}, got {sorted(exts)}"
        )
    return exts


def _public_url(env: Mapping[str, str], mode: str) -> str:
    """The URL clients use; it is the OAuth resource id and the allowed Host."""
    url = _required(env, "BMCP_PUBLIC_URL").rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path \
            or parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"BMCP_PUBLIC_URL must be scheme://host[:port] only, got {url!r}")
    if mode == "prod" and parts.scheme != "https":
        raise ConfigError("BMCP_ENV=prod requires an https BMCP_PUBLIC_URL")
    if mode == "dev":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = parts.hostname == "localhost"
        if not loopback:
            raise ConfigError(f"BMCP_ENV=dev only allows a loopback BMCP_PUBLIC_URL, got {url}")
    return url


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    mode = env.get("BMCP_ENV", "dev").strip()
    if mode not in ENVIRONMENTS:
        raise ConfigError(f"BMCP_ENV must be one of {ENVIRONMENTS}, got {mode!r}")

    bind_host = _required(env, "BMCP_BIND_HOST")
    try:
        bind_ip = ipaddress.ip_address(bind_host)
    except ValueError:
        raise ConfigError(f"BMCP_BIND_HOST must be an IP address, got {bind_host!r}") from None
    if mode == "dev" and not bind_ip.is_loopback:
        raise ConfigError(f"BMCP_ENV=dev only allows a loopback BMCP_BIND_HOST, got {bind_host}")

    return Settings(
        env=mode,
        bind_host=bind_host,
        port=_positive_int(env, "BMCP_PORT", default=18443, maximum=65535),
        workspace_root=_abs_path(env, "BMCP_WORKSPACE_ROOT"),
        log_dir=_abs_path(env, "BMCP_LOG_DIR"),
        bruin_bin=_abs_path(env, "BRUIN_BIN"),
        bruin_home=_abs_path(env, "BRUIN_HOME"),
        bruin_expected_version=_required(env, "BRUIN_EXPECTED_VERSION"),
        bruin_verified_file=_abs_path(env, "BRUIN_VERIFIED_FILE"),
        max_upload_mb=_positive_int(env, "BMCP_MAX_UPLOAD_MB", default=100),
        max_export_mb=_positive_int(env, "BMCP_MAX_EXPORT_MB", default=100),
        user_quota_gb=_positive_int(env, "BMCP_USER_QUOTA_GB", default=5),
        allowed_input_ext=_input_ext(env),
        public_url=_public_url(env, mode),
        token_file=_abs_path(env, "BMCP_TOKEN_FILE"),
        tmp_dir=_abs_path(env, "BMCP_TMP_DIR"),
        tool_timeout_s=_positive_int(env, "BMCP_TOOL_TIMEOUT_S", default=120, maximum=3600),
        user_concurrency=_positive_int(env, "BMCP_USER_CONCURRENCY", default=2, maximum=64),
        max_concurrency=_positive_int(env, "BMCP_MAX_CONCURRENCY", default=8, maximum=256),
        run_timeout_s=_positive_int(env, "BMCP_RUN_TIMEOUT_S", default=600, maximum=7200),
        max_runs=_positive_int(env, "BMCP_MAX_RUNS", default=2, maximum=32),
    )
