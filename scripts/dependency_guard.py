"""Dependency guard for Deliverome repos: no silent downgrades, no versions under 7 days old.

Compares every lockfile in the repository at HEAD with the pull request's base:

* ``uv.lock``            (Python, PyPI)
* ``Cargo.lock``         (Rust, crates.io)
* ``package-lock.json``  (JavaScript, npm; lockfile v2/v3)

and fails when a PR

1. moves any package to an **older** version (a dependency's cap made the resolver go
   backwards, e.g. pandas 3.0.3 -> 2.3.3 on 2026-09-10 in deliverome-analysis), or
2. newly locks a version released **less than MIN_AGE_DAYS** (default 7) ago, transitive
   dependencies included. Supply-chain attacks are most dangerous in their first week, and
   Dependabot's cooldown only covers the package it bumps, not what the lockfile re-resolves;
   Dependabot *security* updates skip the cooldown entirely.

Deliberate exceptions are written in the PR description, one per line, so they are reviewed
and recorded:

    allow-downgrade: pandas
    allow-fresh: openssl-sys

Standard library only (Python >= 3.11). Environment: BASE_SHA (default ``origin/main``),
PR_BODY, MIN_AGE_DAYS, plus ALLOW_DOWNGRADE / ALLOW_FRESH (comma-separated) for local runs.
Exit status 1 on any unallowed violation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

LOCKFILES = {"uv.lock": "pypi", "Cargo.lock": "crates", "package-lock.json": "npm"}
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
USER_AGENT = "deliverome-dependency-guard/1.0 (+https://github.com/Deliverome-Project/.github)"
TIMEOUT = 20

# ------------------------------------------------------------------------------------------
# lockfile parsing: {key: (name, version)}; key distinguishes nested npm copies
# ------------------------------------------------------------------------------------------


def parse_uv(text: str) -> dict[str, tuple[str, str]]:
    out = {}
    for p in tomllib.loads(text).get("package", []):
        src = p.get("source", {})
        if "registry" in src and p.get("version"):  # skip git, path, editable, the project
            out[p["name"]] = (p["name"], p["version"])
    return out


def parse_cargo(text: str) -> dict[str, tuple[str, str]]:
    out = {}
    for p in tomllib.loads(text).get("package", []):
        if p.get("source") == CRATES_IO:
            # several semver-incompatible versions of one crate can coexist: key by the
            # compatibility line (major for >= 1, 0.minor for 0.x, as Cargo does)
            major, minor = _semver(p["version"])[:2]
            line = f"{major}" if major else f"0.{minor}"
            out[f"{p['name']}@{line}"] = (p["name"], p["version"])
    return out


def parse_npm(text: str) -> dict[str, tuple[str, str]]:
    out = {}
    data = json.loads(text)
    for path, p in data.get("packages", {}).items():
        if not path or p.get("link") or not p.get("version") or "resolved" not in p:
            continue  # root project, workspace links, local paths
        resolved = p["resolved"]
        if not resolved.startswith("https://registry.npmjs.org/"):
            continue
        name = p.get("name") or path.rsplit("node_modules/", 1)[-1]
        out[path] = (name, p["version"])
    return out


PARSERS = {"pypi": parse_uv, "crates": parse_cargo, "npm": parse_npm}

# ------------------------------------------------------------------------------------------
# version ordering
# ------------------------------------------------------------------------------------------


def _pep440(version: str) -> tuple:
    v = version.lower().split("+", 1)[0]
    m = re.match(r"^(?:\d+!)?(\d+(?:\.\d+)*)(.*)$", v)
    if not m:
        return ((0,), (0, 0))
    release = tuple(int(x) for x in m.group(1).split("."))
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    rest = m.group(2)
    pre = re.search(r"(a|b|rc|alpha|beta|c|pre|preview)\.?(\d*)", rest)
    post = re.search(r"(?:post|rev|r)\.?(\d*)|-(\d+)", rest)
    dev = re.search(r"dev\.?(\d*)", rest)
    if pre:
        order = {"a": 0, "alpha": 0, "b": 1, "beta": 1}.get(pre.group(1), 2)
        phase = (0, order, int(pre.group(2) or 0))
    elif post:
        phase = (2, int(post.group(1) or post.group(2) or 0))
    elif dev:
        phase = (-1, int(dev.group(1) or 0))
    else:
        phase = (1, 0)
    return (release, phase)


def _semver(version: str) -> tuple:
    core, _, pre = version.split("+", 1)[0].partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in (core.split(".") + ["0", "0"])[:3])
    if not pre:
        return (*nums, 1, ())
    ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in pre.split("."))
    return (*nums, 0, ids)


def version_key(ecosystem: str, version: str) -> tuple:
    return _pep440(version) if ecosystem == "pypi" else _semver(version)


# ------------------------------------------------------------------------------------------
# release dates
# ------------------------------------------------------------------------------------------


def _get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None


def _iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def release_time(ecosystem: str, name: str, version: str) -> datetime | None:
    if ecosystem == "pypi":
        d = _get_json(f"https://pypi.org/pypi/{name}/{version}/json")
        times = [_iso(f.get("upload_time_iso_8601")) for f in (d or {}).get("urls", [])]
        times = [t for t in times if t]
        return min(times) if times else None
    if ecosystem == "npm":
        d = _get_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}")
        return _iso((d or {}).get("time", {}).get(version))
    if ecosystem == "crates":
        d = _get_json(f"https://crates.io/api/v1/crates/{name}/{version}")
        return _iso((d or {}).get("version", {}).get("created_at"))
    return None


# ------------------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------------------


def _allowed(kind: str) -> set[str]:
    names = set(os.environ.get(f"ALLOW_{kind.upper()}", "").lower().split(","))
    for line in os.environ.get("PR_BODY", "").splitlines():
        m = re.match(rf"\s*allow-{kind}:\s*(.+)$", line, re.IGNORECASE)
        if m:
            names |= {n.lower() for n in re.split(r"[,\s]+", m.group(1))}
    return names - {""}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def lockfiles() -> list[tuple[str, str]]:
    files = _git("ls-files").stdout.splitlines()
    return [
        (f, LOCKFILES[Path(f).name])
        for f in files
        if Path(f).name in LOCKFILES and "node_modules/" not in f
    ]


def compare(base: dict, head: dict, ecosystem: str):
    """(downgrades, new_or_changed) between two parsed lockfiles."""
    down, changed = [], []
    for key, (name, new) in head.items():
        old = base.get(key, (None, None))[1]
        if old == new:
            continue
        changed.append((name, new))
        if old and version_key(ecosystem, new) < version_key(ecosystem, old):
            down.append((name, old, new))
    return down, changed


def main() -> int:
    base_ref = os.environ.get("BASE_SHA") or "origin/main"
    min_age = timedelta(days=float(os.environ.get("MIN_AGE_DAYS") or 7))
    allow_down, allow_fresh = _allowed("downgrade"), _allowed("fresh")
    now = datetime.now(tz=UTC)
    errors, notes = [], []

    for path, eco in lockfiles():
        head_text = Path(path).read_text()
        base = _git("show", f"{base_ref}:{path}")
        if base.returncode != 0:
            notes.append(f"{path}: new lockfile, skipped (no base to compare)")
            continue
        parse = PARSERS[eco]
        down, changed = compare(parse(base.stdout), parse(head_text), eco)
        for name, old, new in down:
            ok = name.lower() in allow_down
            line = f"{path}: {name} downgraded {old} -> {new}"
            (notes if ok else errors).append(line + (" (allowed)" if ok else ""))
        uniq = sorted(set(changed))
        with ThreadPoolExecutor(max_workers=10) as pool:
            times = list(pool.map(lambda nv: release_time(eco, *nv), uniq))
        unknown = 0
        for (name, version), t in zip(uniq, times, strict=True):
            if t is None:
                unknown += 1
                continue
            age = now - t
            if age < min_age:
                ok = name.lower() in allow_fresh
                line = f"{path}: {name} {version} is {age.total_seconds() / 86400:.1f} days old"
                (notes if ok else errors).append(line + (" (allowed)" if ok else ""))
        notes.append(f"{path}: {len(uniq)} new/changed versions checked, {len(down)} downgraded"
                     + (f", {unknown} without a registry date" if unknown else ""))

    for n in notes:
        print(n)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## Dependency guard\n\n" + "\n".join(f"- {x}" for x in errors + notes) + "\n")
    if not errors:
        print("Dependency guard passed.")
        return 0
    print()
    for e in errors:
        print(f"::error::{e}")
    print()
    print("Downgrades usually mean a new or re-pinned dependency caps a version and the resolver")
    print("went backwards. Fresh versions are held for a week as supply-chain protection. If the")
    print("change is intended, add `allow-downgrade: <name>` or `allow-fresh: <name>` to the PR")
    print("description and re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
