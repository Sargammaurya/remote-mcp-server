"""
The only way the service runs Bruin.

Before every run the binary is checked offline against VERIFIED.txt, which
scripts/fetch_bruin.sh wrote after checking the release hashes and running
`bruin version` once: the binary's SHA-256 must equal the recorded one, and
the recorded version must equal BRUIN_EXPECTED_VERSION. `bruin version` is
never called here, because it always contacts GitHub.

Bruin gets a minimal environment, not the service's: a fixed PATH, the
dedicated HOME, and TELEMETRY_OPTOUT=true (Bruin opts out on any non-empty
value; `true` is the documented one).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .config import Settings

BASE_PATH = "/usr/local/bin:/usr/bin:/bin"


class BruinPinError(RuntimeError):
    """The Bruin binary is not the verified, pinned release."""


def read_verified(path: Path) -> dict[str, str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BruinPinError(f"cannot read {path}: {exc}") from exc
    record = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep:
            record[key.strip()] = value.strip()
    return record


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_pinned_binary(settings: Settings) -> dict[str, str]:
    record = read_verified(settings.bruin_verified_file)
    for key in ("version", "binary_sha256"):
        if not record.get(key):
            raise BruinPinError(f"{settings.bruin_verified_file} has no {key}")
    if record["version"] != settings.bruin_expected_version:
        raise BruinPinError(
            f"verified version {record['version']} != expected {settings.bruin_expected_version}"
        )
    try:
        actual = sha256_file(settings.bruin_bin)
    except OSError as exc:
        raise BruinPinError(f"cannot read Bruin binary {settings.bruin_bin}: {exc}") from exc
    if actual != record["binary_sha256"]:
        raise BruinPinError(
            f"{settings.bruin_bin} sha256 {actual} does not match the verified "
            f"{record['binary_sha256']}"
        )
    return record


def bruin_env(settings: Settings, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(extra or {})
    # set last so a caller can never override them
    env.update({
        "PATH": BASE_PATH,
        "HOME": str(settings.bruin_home),
        "TELEMETRY_OPTOUT": "true",
        "LANG": "C.UTF-8",
    })
    return env


def run_bruin(settings: Settings, args: list[str], cwd: Path, timeout: float,
              extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the verified Bruin binary; raises BruinPinError before running anything else."""
    verify_pinned_binary(settings)
    return subprocess.run(
        [str(settings.bruin_bin), *args],
        cwd=cwd,
        env=bruin_env(settings, extra_env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
