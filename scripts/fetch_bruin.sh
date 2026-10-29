#!/bin/sh
# Download the pinned Bruin release, verify it, and install the binary under
# vendor/bruin/<version>/. Run once, as the service user, from anywhere.
#
# What the checks prove: the expected hashes below were recorded from the
# GitHub release page for this tag (asset digests reported by the GitHub API).
# They come from the same release as the files, so a match proves the download
# is the published artifact, not that the release itself is trustworthy —
# Bruin publishes no signature to check against.
#
# Network: github.com (+ its release-asset CDN) only. Telemetry is opted out
# for the single `bruin version` call, which itself fetches
# github.com/bruin-data/bruin/releases/latest (no way to turn that off).
set -eu
umask 027

VERSION="v0.11.780"
TARBALL="bruin_Linux_x86_64.tar.gz"
CHECKSUMS="bruin_${VERSION#v}_checksums.txt"
TARBALL_SHA256="0e92ab1e4116a83fe3e3a85ea4af5b6a7fd404adc461026d8dce082b11c28a8c"
CHECKSUMS_SHA256="91e256fd36a8ee3217282a2dd681a42979fdf8dc84425643afd1d0efd4a111c6"
BASE_URL="https://github.com/bruin-data/bruin/releases/download/${VERSION}"

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${PROJECT_ROOT}/vendor/bruin/${VERSION}"
BRUIN_HOME_DIR="${PROJECT_ROOT}/var/bruin-home"

if [ -e "${DEST}/bruin" ]; then
    echo "refusing: ${DEST}/bruin already exists" >&2
    exit 1
fi

WORK="$(mktemp -d "${PROJECT_ROOT}/var/tmp/fetch-bruin.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

fetch() {
    curl --proto '=https' --tlsv1.2 -fsSL --max-time 300 -o "${WORK}/$1" "${BASE_URL}/$1"
}

check_sha() {  # file expected
    actual="$(sha256sum "$1" | cut -d' ' -f1)"
    if [ "${actual}" != "$2" ]; then
        echo "SHA-256 MISMATCH for $(basename "$1"): got ${actual}, expected $2" >&2
        exit 1
    fi
    echo "sha256 ok  $(basename "$1")  ${actual}"
}

fetch "${CHECKSUMS}"
fetch "${TARBALL}"

check_sha "${WORK}/${CHECKSUMS}" "${CHECKSUMS_SHA256}"
check_sha "${WORK}/${TARBALL}" "${TARBALL_SHA256}"

# the release's own checksums file must list the same hash for the tarball
listed="$(awk -v f="${TARBALL}" '$2 == f {print $1}' "${WORK}/${CHECKSUMS}")"
if [ "${listed}" != "${TARBALL_SHA256}" ]; then
    echo "checksums file lists '${listed}' for ${TARBALL}, expected ${TARBALL_SHA256}" >&2
    exit 1
fi
echo "checksums file agrees for ${TARBALL}"

echo "tarball contents:"
tar -tzf "${WORK}/${TARBALL}" | sed 's/^/  /'
tar -xzf "${WORK}/${TARBALL}" -C "${WORK}" bruin
chmod 0750 "${WORK}/bruin"

# the one network-touching version call, telemetry off, dedicated HOME
version_json="$(env -i PATH=/usr/bin:/bin HOME="${BRUIN_HOME_DIR}" TELEMETRY_OPTOUT=true \
    "${WORK}/bruin" version -o json)"
echo "bruin version -o json: ${version_json}"
reported="$(printf '%s' "${version_json}" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
commit="$(printf '%s' "${version_json}" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["commit"])')"
if [ "${reported}" != "${VERSION}" ]; then
    echo "binary reports ${reported}, expected ${VERSION}" >&2
    exit 1
fi

binary_sha="$(sha256sum "${WORK}/bruin" | cut -d' ' -f1)"
mv "${WORK}/bruin" "${DEST}/bruin"
mv "${WORK}/${CHECKSUMS}" "${DEST}/${CHECKSUMS}"

cat > "${DEST}/VERIFIED.txt" <<EOF
version=${VERSION}
commit=${commit}
binary_sha256=${binary_sha}
tarball=${TARBALL}
tarball_sha256=${TARBALL_SHA256}
checksums_file_sha256=${CHECKSUMS_SHA256}
source=${BASE_URL}
verified_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
note=hashes recorded from the GitHub release for this tag; integrity check only, not independent authentication
EOF
echo "installed ${DEST}/bruin (sha256 ${binary_sha})"
