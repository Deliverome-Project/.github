#!/usr/bin/env python3
"""Trust-store check for Deliverome laptops: fail while a local TLS root store still trusts a
compromised CA, or while uv is older than its fixed release.

Runs as a pre-push hook in every Deliverome repo (see .pre-commit-hooks.yaml). Package
pins are the dependency guard's job; this covers what a lockfile cannot reach: the OS and
Homebrew bundles, the repo's own virtualenv, and the uv binary, which carries its own roots.

Roots are matched by the SHA-256 of their DER encoding, because some bundles (Homebrew's)
are bare PEM with no names to search for.

Standard library only. Exit status 1 when anything needs fixing.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys

# eMudhra intermediate-CA key compromise, 2026-10-19: the eight intermediates chain to these
# two roots, and stores that cannot block intermediates (no revocation checks) drop the roots.
# https://blog.cloudflare.com/emudhra-certificate-compromise/
COMPROMISED_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": "AAA Certificate Services",
}
MIN_UV = (0, 13, 4)

BUNDLES = [
    "/etc/ssl/cert.pem",  # macOS built-in curl, git, python3, openssl
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew, Apple silicon
    "/usr/local/etc/ca-certificates/cert.pem",  # Homebrew, Intel
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora/RHEL
    ".venv/lib/python*/site-packages/certifi/cacert.pem",  # this repo's requests/boto3
]

FIXES = {
    "/etc/ssl/cert.pem": (
        "macOS has not updated this file yet. Remove the two roots (keeps a backup):\n"
        "      sudo cp -p /etc/ssl/cert.pem /etc/ssl/cert.pem.bak && sudo awk "
        "'/^(###|===) /{skip=0} /^=== .*\\/CN=(AAA Certificate Services|emSign ECC Root CA - G3)$/"
        "{skip=1} !skip' /etc/ssl/cert.pem.bak | sudo tee /etc/ssl/cert.pem > /dev/null"
    ),
    "ca-certificates": "brew update && brew upgrade ca-certificates",
    "certifi": "git pull, then: uv sync --locked",
}

PEM = re.compile(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)


def fingerprints(text: str) -> set[str]:
    out = set()
    for m in PEM.finditer(text):
        try:
            out.add(hashlib.sha256(base64.b64decode("".join(m.group(1).split()))).hexdigest())
        except ValueError:
            continue
    return out


def bundle_paths() -> list[str]:
    paths = [p for pattern in BUNDLES for p in sorted(glob.glob(pattern))]
    if os.environ.get("SSL_CERT_FILE"):
        paths.append(os.environ["SSL_CERT_FILE"])
    return [p for p in dict.fromkeys(paths) if os.path.isfile(p)]


def fix_for(path: str) -> str:
    if path in FIXES:
        return FIXES[path]
    return FIXES["certifi"] if "certifi" in path else FIXES["ca-certificates"]


def uv_version() -> tuple[int, ...] | None:
    uv = shutil.which("uv")
    if not uv:
        return None
    out = subprocess.run([uv, "--version"], capture_output=True, text=True, check=False).stdout
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def main() -> int:
    problems = []
    for path in bundle_paths():
        try:
            with open(path, encoding="ascii", errors="ignore") as fh:
                found = fingerprints(fh.read()) & COMPROMISED_ROOTS.keys()
        except OSError:
            continue
        if found:
            names = ", ".join(sorted(COMPROMISED_ROOTS[f] for f in found))
            problems.append(f"{path} still trusts {names}.\n    Fix: {fix_for(path)}")
    uv = uv_version()
    if uv and uv < MIN_UV:
        have, need = ".".join(map(str, uv)), ".".join(map(str, MIN_UV))
        problems.append(
            f"uv {have} trusts the compromised CAs (fixed in {need}).\n"
            "    Fix: brew upgrade uv   (or `uv self update` if you did not install it with brew)"
        )
    if not problems:
        print("Trust stores OK: no compromised CA roots found.")
        return 0
    print("Your machine still trusts certificates from a compromised certificate authority")
    print("(eMudhra, October 2026), so its connections can be impersonated. Push blocked until")
    print("fixed; do not work around this by turning off certificate verification.\n")
    for p in problems:
        print(f"  - {p}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
