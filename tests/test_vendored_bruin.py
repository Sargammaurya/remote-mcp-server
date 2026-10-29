"""Checks against the real vendored binary; skipped when it has not been fetched."""

from pathlib import Path

import pytest

from bruin_mcp_service.bruin_cli import read_verified, sha256_file

VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "bruin" / "v0.11.780"

pytestmark = pytest.mark.skipif(not (VENDOR / "bruin").exists(),
                                reason="run scripts/fetch_bruin.sh first")


def test_vendored_binary_matches_verified_record():
    record = read_verified(VENDOR / "VERIFIED.txt")
    assert record["version"] == "v0.11.780"
    assert sha256_file(VENDOR / "bruin") == record["binary_sha256"]


def test_release_checksums_file_lists_recorded_tarball_hash():
    record = read_verified(VENDOR / "VERIFIED.txt")
    listed = {}
    for line in (VENDOR / "bruin_0.11.780_checksums.txt").read_text().splitlines():
        digest, _, name = line.partition("  ")
        listed[name.strip()] = digest
    assert listed[record["tarball"]] == record["tarball_sha256"]
