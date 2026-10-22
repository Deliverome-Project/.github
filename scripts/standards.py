"""Keep every Deliverome repository on the Python, uv and package versions in standards.toml.

Three uses, one source of truth (``standards.toml`` at the repository root):

``drift`` (also run by the dependency guard on every pull request)
    Fails when a checkout disagrees with the standard: the ``requires-python`` floor,
    ``.python-version``, ruff ``target-version``, mypy ``python_version``, ``setup-python``
    versions and CI matrices, ``[tool.uv] required-version``, or a ``setup-uv`` ``version:``
    pin, and in any repository with a ``uv.lock`` the managed Claude Code cloud hook
    (``.claude/hooks/ensure-uv.sh``, rendered from ``managed/claude/ensure-uv.sh`` with the
    standard's uv versions, plus its SessionStart entry in ``.claude/settings.json``). Lockfile
    floors are the guard's own check.

``report`` (weekly, and before a fan-out)
    Discovers every long-lived branch of every Python repository in the org and checks each
    one through the GitHub API, without cloning: drift, lockfile floors, ``exclude-newer-package``
    exceptions that have outlived the 7-day quarantine, required status checks whose names
    embed a Python version, and open pull requests already touching the same files.

``fanout``
    For each discovered branch: clone it, apply the standard, re-lock (``uv lock
    --upgrade-package <pkg>==<floor>``; ``cargo update --precise`` for crates), and refuse
    when the lock moves anything other than the target packages, when ``uv lock --check``
    fails, or when the repository's own age check or the shared guard fails. ``--dry-run``
    prints the plan; otherwise it opens one pull request per branch.

Long-lived branches are found, not listed: the default branch, protected branches, branches a
ruleset covers, and branches that were the base of ``min_merged_prs`` merged pull requests in
the last ``window_days`` (an unprotected integration branch that PRs merge into).

Standard library only (Python >= 3.11); ``gh`` does the GitHub calls, with its own auth or
``GH_TOKEN``.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
DEFAULT_STANDARDS = HERE.parent / "standards.toml"
# Claude Code cloud sessions ship an older uv than required-version allows; this hook installs
# the standard's. Managed: the template lives beside standards.toml and every copy must match.
HOOK = ".claude/hooks/ensure-uv.sh"
SETTINGS = ".claude/settings.json"
HOOK_TEMPLATE = "managed/claude/ensure-uv.sh"
HOOK_ENTRY = {"matcher": "startup|resume", "hooks": [
    {"type": "command", "command": 'bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/ensure-uv.sh',
     "timeout": 180}]}
QUARANTINE = timedelta(days=7)
# repository-local age checks that fan-out runs after re-locking
AGE_CHECKS = ("scripts/check-fresh-deps.py", "scripts/check_dependency_age.py")
# fan-out checks out only what it reads, writes or locks (data-heavy repos take many minutes
# to clone in full): configuration, lockfiles, scripts and package sources
SPARSE = ["/*", "/.github/", "/.claude/", "/scripts/", "/src/", "/python/", "/crates/",
          *(f"**/{n}" for n in ("pyproject.toml", "uv.lock", ".python-version", "ruff.toml",
                                ".ruff.toml", "Cargo.toml", "Cargo.lock", "package-lock.json",
                                "README.md", "LICENSE"))]


def _guard():
    """dependency_guard.py, which sits beside this file (lockfile parsers, release dates)."""
    sys.path.insert(0, str(HERE))
    import dependency_guard

    return dependency_guard


# ------------------------------------------------------------------------------------------
# the standard
# ------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Floor:
    ecosystem: str
    package: str
    version: str
    reason: str


@dataclasses.dataclass(frozen=True)
class Standards:
    python: str
    uv_minimum: str
    floors: tuple[Floor, ...]
    exceptions: tuple[dict, ...]
    discovery: dict
    uv_cloud: str = ""
    hook_template: str = ""

    def ensure_uv(self) -> str:
        """The managed ensure-uv.sh, with this standard's uv versions filled in."""
        return (self.hook_template.replace("@UV_MIN@", self.uv_minimum)
                .replace("@UV_PIN@", self.uv_cloud or self.uv_minimum))

    def python_for(self, repo: str, path: str) -> str:
        """The Python X.Y a file in ``repo`` must target, honouring [[exceptions]]."""
        p = PurePosixPath(path)
        for exc in self.exceptions:
            ex = PurePosixPath(exc["path"])
            if exc["repo"] == repo and (p == ex or ex in p.parents):
                return exc["python"]
        return self.python

    def floor(self, ecosystem: str, package: str) -> Floor | None:
        for f in self.floors:
            if f.ecosystem == ecosystem and f.package == package.lower():
                return f
        return None


def load(path: Path | str | None = None) -> Standards:
    path = Path(path or os.environ.get("STANDARDS_FILE") or DEFAULT_STANDARDS)
    data = tomllib.loads(path.read_text())
    floors = tuple(
        Floor(f["ecosystem"], f["package"].lower(), f["version"], f["reason"])
        for f in data.get("floors", [])
    )
    template = path.parent / HOOK_TEMPLATE
    return Standards(
        python=data["python"]["version"],
        uv_minimum=data["uv"]["minimum"],
        floors=floors,
        exceptions=tuple(data.get("exceptions", [])),
        discovery=data.get("discovery", {}),
        uv_cloud=data["uv"].get("cloud", data["uv"]["minimum"]),
        hook_template=template.read_text() if template.exists() else "",
    )


def _xy(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version)[:2])


def _pep440(version: str) -> tuple:
    return _guard().version_key("pypi", version)


# ------------------------------------------------------------------------------------------
# drift: what a tree of files says, against what the standard says
# ------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Finding:
    path: str
    line: int
    what: str
    found: str
    expected: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what} is {self.found}, standard is {self.expected}"


def relevant(path: str) -> bool:
    """Files drift and fan-out read."""
    name = PurePosixPath(path).name
    if path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
        return True
    return path in {HOOK, SETTINGS} or name in {"pyproject.toml", ".python-version", "ruff.toml",
                                                ".ruff.toml"}


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


REQUIRES_PYTHON = re.compile(r'(?m)^(requires-python\s*=\s*["\'][^"\']*?>=\s*)(3\.\d+)')
TARGET_VERSION = re.compile(r'(?m)^(target-version\s*=\s*["\']py)(3)(\d+)(["\'])')
MYPY_VERSION = re.compile(r'(?m)^(python_version\s*=\s*["\'])(3\.\d+)(["\'])')
REQUIRED_VERSION = re.compile(r'(?m)^(required-version\s*=\s*["\'][^"\']*?>=\s*)([\d.]+)')
# setup-python scalar: `python-version: "3.12"` (expressions such as ${{ matrix.python }} skip)
PY_SCALAR = re.compile(r'(?m)^(\s*-?\s*python(?:-version)?:\s*)(["\']?)(3\.\d+)(\2)(\s*(?:#.*)?)$')
# matrix flow list: `python: ['3.11', '3.12']`
PY_FLOW = re.compile(r'(?m)^(\s*python(?:-version)?:\s*)\[([^\]]*)\]')
# matrix block list: key on its own line, then `- "3.11"` items
PY_BLOCK = re.compile(r'(?m)^(\s*)python(?:-version)?:\s*\n((?:\1\s+-\s*["\']?3\.\d+["\']?\s*\n)+)')
# a Python named on a command line: `uv sync --python 3.12`, `uv python install 3.12`
PY_CLI = re.compile(r"(--python[ =]|\buv python install )(3\.\d+)\b")
SETUP_UV = re.compile(r"(?m)^(\s*)-?\s*uses:\s*astral-sh/setup-uv@.*$")


def _section(text: str, header: str) -> tuple[int, int] | None:
    """(start, end) offsets of a TOML table's body, e.g. header 'tool.uv'."""
    m = re.search(rf"(?m)^\[{re.escape(header)}\]\s*$", text)
    if not m:
        return None
    nxt = re.search(r"(?m)^\[", text[m.end():])
    return m.end(), (m.end() + nxt.start()) if nxt else len(text)


def drift_pyproject(path: str, text: str, std: Standards, repo: str, has_lock: bool) -> list[Finding]:
    want = std.python_for(repo, path)
    out = []
    m = REQUIRES_PYTHON.search(text)
    if m and m.group(2) != want:
        out.append(Finding(path, _line_of(text, m.start()), "requires-python floor", m.group(2), want))
    for sec, rx, what in (("tool.ruff", TARGET_VERSION, "ruff target-version"),
                          ("tool.mypy", MYPY_VERSION, "mypy python_version")):
        span = _section(text, sec)
        m = rx.search(text, *span) if span else None
        if not m:
            continue
        found = f"{m.group(2)}.{m.group(3)}" if rx is TARGET_VERSION else m.group(2)
        if found != want:
            out.append(Finding(path, _line_of(text, m.start()), what, found, want))
    span = _section(text, "tool.uv")
    m = REQUIRED_VERSION.search(text, *span) if span else None
    if m:
        if _pep440(m.group(2)) < _pep440(std.uv_minimum):
            out.append(Finding(path, _line_of(text, m.start()), "uv required-version",
                               f">={m.group(2)}", f">={std.uv_minimum}"))
    elif has_lock:
        out.append(Finding(path, 1, "uv required-version", "missing", f">={std.uv_minimum}"))
    return out


def drift_ruff(path: str, text: str, std: Standards, repo: str) -> list[Finding]:
    want = std.python_for(repo, path)
    m = TARGET_VERSION.search(text)
    if m and f"{m.group(2)}.{m.group(3)}" != want:
        return [Finding(path, _line_of(text, m.start()), "ruff target-version",
                        f"{m.group(2)}.{m.group(3)}", want)]
    return []


def drift_python_version(path: str, text: str, std: Standards, repo: str) -> list[Finding]:
    want = std.python_for(repo, path)
    m = re.match(r"\s*(3\.\d+)", text)
    if m and m.group(1) != want:
        return [Finding(path, 1, ".python-version", m.group(1), want)]
    return []


def _setup_uv_versions(text: str):
    """(match_start, version_start, version) for each `version:` pinned on a setup-uv step."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for i, line in enumerate(lines):
        m = SETUP_UV.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            stripped = nxt.lstrip()
            if stripped and (len(nxt) - len(stripped) <= indent or stripped.startswith("- ")):
                break  # next step or a shallower key
            v = re.match(r'(\s*version:\s*["\']?)([\d.]+)', nxt)
            if v:
                yield offsets[j], offsets[j] + len(v.group(1)), v.group(2)


def drift_workflow(path: str, text: str, std: Standards, repo: str) -> list[Finding]:
    want = std.python_for(repo, path)
    out = []
    for m in PY_SCALAR.finditer(text):
        if m.group(3) != want:
            out.append(Finding(path, _line_of(text, m.start()), "setup-python version", m.group(3), want))
    for m in PY_FLOW.finditer(text):
        found = re.findall(r"3\.\d+", m.group(2))
        if found and found != [want]:
            out.append(Finding(path, _line_of(text, m.start()), "Python matrix",
                               ", ".join(found), want))
    for m in PY_BLOCK.finditer(text):
        found = re.findall(r"3\.\d+", m.group(2))
        if found != [want]:
            out.append(Finding(path, _line_of(text, m.start()), "Python matrix",
                               ", ".join(found), want))
    for m in PY_CLI.finditer(text):
        if m.group(2) != want:
            out.append(Finding(path, _line_of(text, m.start()), "Python on a command line",
                               m.group(2), want))
    for start, _, version in _setup_uv_versions(text):
        if _pep440(version) < _pep440(std.uv_minimum):
            out.append(Finding(path, _line_of(text, start), "setup-uv version", version,
                               f">={std.uv_minimum}"))
    return out


def _has_hook_entry(settings_text: str) -> bool:
    try:
        entries = json.loads(settings_text).get("hooks", {}).get("SessionStart", [])
    except (ValueError, AttributeError):
        return False
    return any(HOOK in h.get("command", "") for e in entries for h in e.get("hooks", []))


def drift_cloud_hook(files: dict[str, str], std: Standards) -> list[Finding]:
    if not std.hook_template:
        return []
    out = []
    if HOOK not in files:
        out.append(Finding(HOOK, 1, "cloud uv hook", "missing", f"managed copy (uv {std.uv_cloud})"))
    elif files[HOOK] != std.ensure_uv():
        pins = dict(re.findall(r"(?m)^(MIN|PIN)=([\d.]+)", files[HOOK]))
        found = (f"MIN={pins.get('MIN', '?')} PIN={pins.get('PIN', '?')}"
                 if pins != {"MIN": std.uv_minimum, "PIN": std.uv_cloud}
                 else "edited away from the managed copy")
        out.append(Finding(HOOK, 1, "cloud uv hook", found,
                           f"managed copy, MIN={std.uv_minimum} PIN={std.uv_cloud}"))
    if not _has_hook_entry(files.get(SETTINGS, "{}")):
        out.append(Finding(SETTINGS, 1, "SessionStart hook for ensure-uv.sh", "missing", "present"))
    return out


def drift(files: dict[str, str], std: Standards, repo: str) -> list[Finding]:
    """Every disagreement between ``files`` ({path: text}) and the standard."""
    has_lock_paths = {str(PurePosixPath(p).parent) for p in files
                      if PurePosixPath(p).name == "uv.lock"}
    out: list[Finding] = drift_cloud_hook(files, std) if has_lock_paths else []
    for path, text in sorted(files.items()):
        name = PurePosixPath(path).name
        if path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
            out += drift_workflow(path, text, std, repo)
        elif name == "pyproject.toml":
            out += drift_pyproject(path, text, std, repo,
                                   str(PurePosixPath(path).parent) in has_lock_paths)
        elif name in {"ruff.toml", ".ruff.toml"}:
            out += drift_ruff(path, text, std, repo)
        elif name == ".python-version":
            out += drift_python_version(path, text, std, repo)
    return out


def local_files(root: Path) -> dict[str, str]:
    """The relevant tracked files of a checkout, plus its uv.lock files."""
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True,
                             check=True).stdout.splitlines()
    out = {}
    for p in tracked:
        if (relevant(p) or PurePosixPath(p).name == "uv.lock") and (root / p).exists():
            out[p] = (root / p).read_text()  # exists: a sparse checkout lists absent files
    return out


# ------------------------------------------------------------------------------------------
# rewrites: the same rules, applied (fan-out)
# ------------------------------------------------------------------------------------------


def rewrite_pyproject(text: str, want: str, uv_min: str, has_lock: bool,
                      add_fresh: set[str], drop_expired: set[str]) -> str:
    text = REQUIRES_PYTHON.sub(lambda m: m.group(1) + want, text)
    for sec, rx, repl in (
        ("tool.ruff", TARGET_VERSION, lambda m: f"{m.group(1)}3{want.split('.')[1]}{m.group(4)}"),
        ("tool.mypy", MYPY_VERSION, lambda m: f"{m.group(1)}{want}{m.group(3)}"),
    ):
        span = _section(text, sec)
        if span:
            a, b = span
            text = text[:a] + rx.sub(repl, text[a:b]) + text[b:]
    if not has_lock:
        return text
    if _section(text, "tool.uv") is None:
        text = text.rstrip("\n") + "\n\n[tool.uv]\n"
    a, b = _section(text, "tool.uv")
    body = text[a:b]
    m = REQUIRED_VERSION.search(body)
    if m:
        if _pep440(m.group(2)) < _pep440(uv_min):
            body = body[:m.start(2)] + uv_min + body[m.end(2):]
    else:
        body = f'\nrequired-version = ">={uv_min}"' + body
    body = _rewrite_exclude_newer_package(body, add_fresh, drop_expired)
    return text[:a] + body + text[b:]


ENP = re.compile(r"(?m)^exclude-newer-package\s*=\s*\{([^}\n]*)\}[ \t]*(#[^\n]*)?\n?")


def _rewrite_exclude_newer_package(body: str, add: set[str], drop: set[str]) -> str:
    """Add ``pkg = false`` for floors still in quarantine; drop them once they are a week old.

    Only touches the packages named; other entries stay as written."""
    m = ENP.search(body)
    entries: dict[str, str] = {}
    if m:
        for part in filter(None, (p.strip() for p in m.group(1).split(","))):
            k, _, v = part.partition("=")
            entries[k.strip()] = v.strip()
    new = {k: v for k, v in entries.items() if not (k in drop and v == "false")}
    for pkg in sorted(add):
        new.setdefault(pkg, "false")
    if new == entries:
        return body
    line = ("exclude-newer-package = { " + ", ".join(f"{k} = {v}" for k, v in new.items())
            + " }\n") if new else ""
    if m:
        return body[:m.start()] + line + body[m.end():]
    if not line:
        return body
    en = re.search(r"(?m)^exclude-newer\s*=.*\n", body)
    if not en:
        return body  # no quarantine configured, so no exception is needed
    return body[:en.end()] + line + body[en.end():]


def rewrite_python_version(text: str, want: str) -> str:
    return re.sub(r"^(\s*)3\.\d+(\.\d+)?", lambda m: m.group(1) + want, text, count=1)


def rewrite_ruff(text: str, want: str) -> str:
    return TARGET_VERSION.sub(lambda m: f"{m.group(1)}3{want.split('.')[1]}{m.group(4)}", text)


def rewrite_workflow(text: str, want: str, uv_min: str) -> str:
    text = PY_SCALAR.sub(lambda m: f"{m.group(1)}{m.group(2)}{want}{m.group(4)}{m.group(5)}", text)

    def flow(m):
        items = re.findall(r"(['\"]?)3\.\d+\1", m.group(2))
        quote = items[0] if items else "'"
        return f"{m.group(1)}[{quote}{want}{quote}]" if items else m.group(0)

    text = PY_FLOW.sub(flow, text)

    def block(m):
        first = m.group(2).splitlines(keepends=True)[0]
        return m.group(0).replace(m.group(2), re.sub(r"3\.\d+", want, first))

    text = PY_BLOCK.sub(block, text)
    text = PY_CLI.sub(lambda m: m.group(1) + want, text)
    for _, vstart, version in sorted(_setup_uv_versions(text), reverse=True):
        if _pep440(version) < _pep440(uv_min):
            text = text[:vstart] + uv_min + text[vstart + len(version):]
    return text


def apply(files: dict[str, str], std: Standards, repo: str, add_fresh: set[str],
          drop_expired: set[str]) -> dict[str, str]:
    """{path: new_text} for every relevant file the standard changes.

    A quarantine exception (``exclude-newer-package``) is added to a project only for the
    fresh floors its own lock contains."""
    dg = _guard()
    locked = {str(PurePosixPath(p).parent): {n.lower() for n, _ in dg.parse_uv(t).values()}
              for p, t in files.items() if PurePosixPath(p).name == "uv.lock"}
    out = {}
    if locked and std.hook_template:
        if files.get(HOOK) != std.ensure_uv():
            out[HOOK] = std.ensure_uv()
        if not _has_hook_entry(files.get(SETTINGS, "{}")):
            settings = json.loads(files.get(SETTINGS) or "{}")
            settings.setdefault("hooks", {}).setdefault("SessionStart", []).append(HOOK_ENTRY)
            out[SETTINGS] = json.dumps(settings, indent=2) + "\n"
    for path, text in files.items():
        name = PurePosixPath(path).name
        if path in {HOOK, SETTINGS}:
            continue
        want = std.python_for(repo, path)
        if path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
            new = rewrite_workflow(text, want, std.uv_minimum)
        elif name == "pyproject.toml":
            d = str(PurePosixPath(path).parent)
            new = rewrite_pyproject(text, want, std.uv_minimum, d in locked,
                                    add_fresh & locked.get(d, set()), drop_expired)
        elif name in {"ruff.toml", ".ruff.toml"}:
            new = rewrite_ruff(text, want)
        elif name == ".python-version":
            new = rewrite_python_version(text, want)
        else:
            continue
        if new != text:
            out[path] = new
    return out


# ------------------------------------------------------------------------------------------
# lockfiles
# ------------------------------------------------------------------------------------------


def below_floors(lock_text: str, ecosystem: str, std: Standards) -> list[tuple[str, str, Floor]]:
    """(name, pinned, floor) for each package in a lockfile pinned under its floor."""
    dg = _guard()
    out = []
    for name, version in sorted(set(dg.PARSERS[ecosystem](lock_text).values())):
        f = std.floor(ecosystem, name)
        if f and dg.version_key(ecosystem, version) < dg.version_key(ecosystem, f.version):
            out.append((name, version, f))
    return out


def lock_changes(old: str, new: str, ecosystem: str, targets: set[str]):
    """(expected, unexpected, removed): version moves in the target packages, anything else
    that moved or appeared, and packages that disappeared (allowed: a Python bump drops
    backports such as tomli)."""
    dg = _guard()
    parse = dg.PARSERS[ecosystem]
    a, b = parse(old), parse(new)
    expected, unexpected = [], []
    for key, (name, ver) in sorted(b.items()):
        before = a.get(key, (None, None))[1]
        if before == ver:
            continue
        (expected if name.lower() in targets else unexpected).append((name, before, ver))
    removed = sorted({a[k] for k in a.keys() - b.keys()})
    return expected, unexpected, removed


# ------------------------------------------------------------------------------------------
# GitHub (through gh)
# ------------------------------------------------------------------------------------------


def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout if r.returncode == 0 else ""


def gh_lines(path: str, jq: str) -> list[str]:
    return [x for x in gh("api", "--paginate", path, "--jq", jq).splitlines() if x]


@dataclasses.dataclass
class Target:
    repo: str
    branch: str
    reasons: list[str]


def ruleset_branches(patterns_in: list[str], patterns_out: list[str], branches: list[str],
                     default: str) -> set[str]:
    def matches(pattern: str, branch: str) -> bool:
        if pattern == "~ALL":
            return True
        if pattern == "~DEFAULT_BRANCH":
            return branch == default
        return fnmatch.fnmatchcase(branch, pattern.removeprefix("refs/heads/"))

    return {b for b in branches
            if any(matches(p, b) for p in patterns_in)
            and not any(matches(p, b) for p in patterns_out)}


def discover(std: Standards, repos: list[str] | None = None, log=print) -> list[Target]:
    """Every long-lived branch of every Python repository in the org."""
    d = std.discovery
    org = d.get("org", "Deliverome-Project")
    since = (date.today() - timedelta(days=int(d.get("window_days", 90)))).isoformat()
    min_prs = int(d.get("min_merged_prs", 2))
    listed = gh_lines(f"orgs/{org}/repos?per_page=100",
                      '.[] | select(.archived | not) | "\\(.name) \\(.default_branch)"')
    targets: list[Target] = []
    for row in sorted(listed):
        repo, default = row.split()
        if repos and repo not in repos:
            continue
        tree = gh("api", f"repos/{org}/{repo}/git/trees/{default}?recursive=1",
                  "--jq", ".tree[].path", check=False).splitlines()
        if not any(PurePosixPath(p).name in {"pyproject.toml", "uv.lock"} for p in tree):
            log(f"skip {repo}: no pyproject.toml or uv.lock on {default}")
            continue
        branches = gh_lines(f"repos/{org}/{repo}/branches?per_page=100", ".[].name")
        found: dict[str, list[str]] = {default: ["default"]}
        for b in gh_lines(f"repos/{org}/{repo}/branches?protected=true&per_page=100", ".[].name"):
            found.setdefault(b, []).append("protected")
        try:
            for rid in gh_lines(f"repos/{org}/{repo}/rulesets?includes_parents=true",
                                '.[] | select(.target == "branch" and .enforcement == "active") | .id'):
                rs = json.loads(gh("api", f"repos/{org}/{repo}/rulesets/{rid}"))
                cond = (rs.get("conditions") or {}).get("ref_name") or {}
                for b in ruleset_branches(cond.get("include", []), cond.get("exclude", []),
                                          branches, default):
                    found.setdefault(b, []).append(f"ruleset {rs.get('name')}")
        except RuntimeError as e:
            log(f"warn {repo}: rulesets unreadable ({e}); relying on the other signals")
        try:
            bases = Counter(gh("pr", "list", "-R", f"{org}/{repo}", "--state", "merged",
                               "--search", f"merged:>={since}", "--limit", "1000",
                               "--json", "baseRefName", "--jq", ".[].baseRefName").split())
        except RuntimeError as e:
            bases = Counter()
            log(f"warn {repo}: merged pull requests unreadable ({e}); unprotected "
                "integration branches will be missed")
        for b, n in bases.items():
            if n >= min_prs and b in branches:
                found.setdefault(b, []).append(f"base of {n} merged PRs since {since}")
        for b, why in sorted(found.items()):
            targets.append(Target(repo, b, why))
    for spec in d.get("include", []):
        repo, _, b = spec.partition(":")
        if not any(t.repo == repo and t.branch == b for t in targets):
            targets.append(Target(repo, b, ["standards.toml include"]))
    drop = set(d.get("exclude", []))
    return [t for t in targets if f"{t.repo}:{t.branch}" not in drop]


def remote_files(org: str, repo: str, branch: str) -> dict[str, str]:
    """Relevant files plus lockfiles of a branch, read through the API (no clone)."""
    tree = gh("api", f"repos/{org}/{repo}/git/trees/{branch}?recursive=1", "--jq",
              '.tree[] | select(.type == "blob") | .path').splitlines()
    wanted = [p for p in tree if relevant(p) or PurePosixPath(p).name in {"uv.lock", "Cargo.lock"}]

    def fetch(p):
        return p, gh("api", f"repos/{org}/{repo}/contents/{p}?ref={branch}",
                     "-H", "Accept: application/vnd.github.raw")

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(fetch, wanted))


def required_checks(org: str, repo: str, branch: str) -> list[str]:
    names = gh_lines(f"repos/{org}/{repo}/rules/branches/{branch}",
                     '.[] | select(.type == "required_status_checks") '
                     "| .parameters.required_status_checks[].context")
    prot = gh("api", f"repos/{org}/{repo}/branches/{branch}/protection/required_status_checks",
              "--jq", ".contexts[]", check=False)
    return sorted(set(names) | set(prot.split("\n")) - {""})


def stale_check_names(checks: list[str], python: str) -> list[str]:
    """Required checks whose name carries a Python version other than the standard's."""
    return [c for c in checks if any(v != python for v in re.findall(r"\b3\.\d+\b", c))]


def open_prs_touching(org: str, repo: str, branch: str) -> list[str]:
    """Open pull requests by people (not bots) that touch the files fan-out edits."""
    raw = gh("pr", "list", "-R", f"{org}/{repo}", "--base", branch, "--state", "open",
             "--json", "number,title,files,author", check=False)
    out = []
    for pr in json.loads(raw or "[]"):
        if (pr.get("author") or {}).get("is_bot"):
            continue
        files = [f["path"] for f in pr.get("files", [])]
        if any(relevant(p) or PurePosixPath(p).name in {"uv.lock", "Cargo.lock"} for p in files):
            out.append(f"#{pr['number']} {pr['title']}")
    return out


# ------------------------------------------------------------------------------------------
# floor ages: when a floor's exception can go
# ------------------------------------------------------------------------------------------


def floor_ages(std: Standards, now: datetime | None = None) -> dict[tuple[str, str], timedelta | None]:
    dg = _guard()
    now = now or datetime.now(tz=UTC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        times = list(pool.map(lambda f: dg.release_time(f.ecosystem, f.package, f.version),
                              std.floors))
    return {(f.ecosystem, f.package): (now - t if t else None)
            for f, t in zip(std.floors, times, strict=True)}


def quarantine_sets(std: Standards, ages) -> tuple[set[str], set[str]]:
    """(pypi floors still under a week old, pypi floors old enough to lose their exception)."""
    fresh, expired = set(), set()
    for f in std.floors:
        if f.ecosystem != "pypi":
            continue
        age = ages.get(("pypi", f.package))
        if age is None:
            continue
        (fresh if age < QUARANTINE else expired).add(f.package)
    return fresh, expired


def cloud_pin_problems(std: Standards) -> list[str]:
    """The cloud uv pin must be at least the minimum and out of its 7-day quarantine."""
    out = []
    if _pep440(std.uv_cloud) < _pep440(std.uv_minimum):
        out.append(f"[uv] cloud {std.uv_cloud} is below [uv] minimum {std.uv_minimum}")
    t = _guard().release_time("pypi", "uv", std.uv_cloud)
    if t and datetime.now(tz=UTC) - t < QUARANTINE:
        out.append(f"[uv] cloud {std.uv_cloud} is under 7 days old: cloud sessions would "
                   "install an unquarantined uv")
    return out


def stale_exceptions(files: dict[str, str], expired: set[str]) -> list[str]:
    out = []
    for path, text in files.items():
        if PurePosixPath(path).name != "pyproject.toml":
            continue
        span = _section(text, "tool.uv")
        m = ENP.search(text, *span) if span else None
        if not m:
            continue
        for part in m.group(1).split(","):
            k, _, v = part.partition("=")
            if k.strip() in expired and v.strip() == "false":
                out.append(f"{path}: exclude-newer-package {k.strip()} = false has outlived "
                           "the 7-day quarantine; remove it")
    return out


# ------------------------------------------------------------------------------------------
# report
# ------------------------------------------------------------------------------------------


def report_target(org: str, t: Target, std: Standards, expired: set[str]) -> dict:
    files = remote_files(org, t.repo, t.branch)
    findings = [str(f) for f in drift(files, std, t.repo)]
    for p, text in files.items():
        eco = {"uv.lock": "pypi", "Cargo.lock": "crates"}.get(PurePosixPath(p).name)
        if eco:
            findings += [f"{p}: {n} {v} is below the security floor {f.version} ({f.reason})"
                         for n, v, f in below_floors(text, eco, std)]
    notes = stale_exceptions(files, expired)
    notes += [f"required check '{c}' names another Python than {std.python}: rename the job "
              "or update the branch protection" for c in
              stale_check_names(required_checks(org, t.repo, t.branch), std.python)]
    prs = open_prs_touching(org, t.repo, t.branch)
    notes += [f"open PR already touches these files: {p}" for p in prs]
    return {"repo": t.repo, "branch": t.branch, "found_by": t.reasons,
            "drift": findings, "notes": notes}


def cmd_report(args, std: Standards) -> int:
    org = std.discovery.get("org", "Deliverome-Project")
    targets = discover(std, args.repo, log=lambda m: print(m, file=sys.stderr))
    for problem in cloud_pin_problems(std):
        print(f"warn: {problem}", file=sys.stderr)
    _, expired = quarantine_sets(std, floor_ages(std))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda t: report_target(org, t, std, expired), targets))
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(render_report(results, std))
    return 1 if any(r["drift"] for r in results) else 0


def render_report(results: list[dict], std: Standards) -> str:
    drifted = [r for r in results if r["drift"]]
    lines = [f"# Standards drift: {len(drifted)} of {len(results)} long-lived branches",
             "", f"Standard: Python {std.python}, uv >= {std.uv_minimum}, "
             f"{len(std.floors)} security floors.", ""]
    for r in results:
        status = "drift" if r["drift"] else "ok"
        lines.append(f"## {r['repo']} `{r['branch']}`: {status} ({'; '.join(r['found_by'])})")
        lines += [f"- {x}" for x in r["drift"]] + [f"- note: {x}" for x in r["notes"]]
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------
# fan-out
# ------------------------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False,
                          env={**os.environ, **(env or {})})


@dataclasses.dataclass
class Plan:
    target: Target
    edits: list[str] = dataclasses.field(default_factory=list)
    lock: list[str] = dataclasses.field(default_factory=list)
    checks: list[str] = dataclasses.field(default_factory=list)
    refusals: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    fresh: set[str] = dataclasses.field(default_factory=set)
    pr: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.edits or self.lock)


def relock(root: Path, std: Standards, plan: Plan, pyproject_changed: set[str]) -> set[str]:
    """Re-lock every uv and Cargo lock under ``root``; record and police what moved."""
    tracked = _run(["git", "ls-files"], root).stdout.splitlines()
    moved: set[str] = set()
    for lock in [p for p in tracked if PurePosixPath(p).name == "uv.lock"]:
        d = root / PurePosixPath(lock).parent
        old = (root / lock).read_text()
        low = below_floors(old, "pypi", std)
        project = str(PurePosixPath(lock).parent / "pyproject.toml").removeprefix("./")
        if not low and project not in pyproject_changed:
            continue
        if not shutil.which("uv"):
            plan.refusals.append(f"{lock}: uv is not installed")
            continue
        cmd = ["uv", "lock"]
        for name, _, f in low:
            cmd += ["--upgrade-package", f"{name}=={f.version}"]
        r = _run(cmd, d)
        if r.returncode != 0:
            plan.refusals.append(f"{lock}: `{' '.join(cmd)}` failed: {r.stderr.strip()[-400:]}")
            continue
        new = (root / lock).read_text()
        expected, unexpected, removed = lock_changes(old, new, "pypi", {n.lower() for n, _, _ in low})
        moved |= {n.lower() for n, _, _ in expected}
        plan.lock += [f"{lock}: {n} {a} -> {b}" for n, a, b in expected]
        plan.lock += [f"{lock}: dropped {n} {v}" for n, v in removed]
        for n, a, b in unexpected:
            plan.refusals.append(f"{lock}: {n} moved {a or '(new)'} -> {b}, which the standard "
                                 "did not ask for (the lock re-resolved)")
        for n, v, f in below_floors(new, "pypi", std):
            plan.refusals.append(f"{lock}: {n} still {v} after re-locking (floor {f.version})")
        chk = _run(["uv", "lock", "--check"], d)
        plan.checks.append(f"{lock}: uv lock --check {'passed' if chk.returncode == 0 else 'FAILED'}")
        if chk.returncode != 0:
            plan.refusals.append(f"{lock}: uv lock --check failed: {chk.stderr.strip()[-300:]}")
    for lock in [p for p in tracked if PurePosixPath(p).name == "Cargo.lock"]:
        old = (root / lock).read_text()
        low = below_floors(old, "crates", std)
        if not low:
            continue
        if not shutil.which("cargo"):
            plan.refusals.append(f"{lock}: needs `cargo update --precise` for "
                                 f"{', '.join(n for n, _, _ in low)}, and cargo is not installed")
            continue
        d = root / PurePosixPath(lock).parent
        for name, ver, f in low:
            r = _run(["cargo", "update", "-p", f"{name}@{ver}", "--precise", f.version], d)
            if r.returncode != 0:
                plan.refusals.append(f"{lock}: cargo update {name} failed: {r.stderr.strip()[-300:]}")
        new = (root / lock).read_text()
        expected, unexpected, removed = lock_changes(old, new, "crates", {n for n, _, _ in low})
        plan.lock += [f"{lock}: {n} {a} -> {b}" for n, a, b in expected]
        plan.refusals += [f"{lock}: {n} moved {a or '(new)'} -> {b} unasked" for n, a, b in unexpected]
    return moved


def fanout_target(org: str, t: Target, std: Standards, fresh: set[str], expired: set[str],
                  workdir: Path, dry_run: bool, std_rev: str) -> Plan:
    plan = Plan(t)
    root = workdir / f"{t.repo}@{t.branch.replace('/', '_')}"
    lfs = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    r = subprocess.run(["gh", "repo", "clone", f"{org}/{t.repo}", str(root), "--", "--depth", "1",
                        "--single-branch", "--branch", t.branch, "--filter=blob:none",
                        "--sparse", "-q"], capture_output=True, text=True, env=lfs)
    if r.returncode == 0:
        r = subprocess.run(["git", "sparse-checkout", "set", "--no-cone", *SPARSE], cwd=root,
                           capture_output=True, text=True, env=lfs)
    if r.returncode != 0:
        plan.refusals.append(f"clone failed: {r.stderr.strip()}")
        return plan
    files = local_files(root)
    edits = apply(files, std, t.repo, fresh, expired)
    for path, text in edits.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
        plan.edits.append(path)
    pyprojects = {p for p in edits if PurePosixPath(p).name == "pyproject.toml"}
    plan.fresh = relock(root, std, plan, pyprojects) & fresh
    remaining = drift(local_files(root), std, t.repo)
    plan.refusals += [f"still drifts after rewriting (fix by hand): {f}" for f in remaining]
    if not plan.changed:
        return plan
    env = {"BASE_SHA": "HEAD", "ALLOW_FRESH": ",".join(sorted(plan.fresh)), "PR_BODY": "",
           "STANDARDS_FILE": str(Path(os.environ.get("STANDARDS_FILE") or DEFAULT_STANDARDS))}
    for script in AGE_CHECKS:
        if (root / script).exists():
            c = _run([sys.executable, script], root, env)
            plan.checks.append(f"{script} {'passed' if c.returncode == 0 else 'FAILED'}")
            if c.returncode != 0:
                tail = (c.stdout + c.stderr).strip().splitlines()[-4:]
                plan.refusals.append(f"{script} failed: {' | '.join(tail)}")
    g = _run([sys.executable, str(HERE / "dependency_guard.py")], root,
             {**env, "GITHUB_REPOSITORY": f"{org}/{t.repo}", "GITHUB_STEP_SUMMARY": ""})
    plan.checks.append(f"shared dependency guard {'passed' if g.returncode == 0 else 'FAILED'}")
    if g.returncode != 0:
        plan.refusals.append("shared dependency guard failed: " + " | ".join(
            line for line in g.stdout.splitlines() if line.startswith("::error::")))
    plan.notes += [f"required check '{c}' names another Python than {std.python}: rename the "
                   "job or update the branch protection before merging" for c in
                   stale_check_names(required_checks(org, t.repo, t.branch), std.python)]
    plan.notes += [f"open PR already touches these files: {p}"
                   for p in open_prs_touching(org, t.repo, t.branch)]
    if dry_run or plan.refusals:
        return plan
    open_pr(org, root, plan, std, std_rev)
    return plan


def summary_line(plan: Plan, std: Standards) -> str:
    bits = []
    if any(PurePosixPath(e).name in {".python-version", "pyproject.toml"} or "workflows" in e
           for e in plan.edits):
        bits.append(f"Python {std.python}")
    bits += sorted({line.split(": ", 1)[1].split()[0] + " " + line.split()[-1]
                    for line in plan.lock if "->" in line})
    return ", ".join(bits) or "Deliverome standards"


def open_pr(org: str, root: Path, plan: Plan, std: Standards, std_rev: str) -> None:
    t = plan.target
    head = f"standards/{date.today().isoformat()}-{t.branch.replace('/', '-')}"
    existing = gh("pr", "list", "-R", f"{org}/{t.repo}", "--head", head, "--json", "url",
                  "--jq", ".[].url", check=False).strip()
    if existing:
        plan.pr = f"exists: {existing}"
        return
    title = f"build(deps): apply Deliverome standards ({summary_line(plan, std)})"
    body = "\n".join([
        "## Summary", "",
        f"Applies `standards.toml` from Deliverome-Project/.github@{std_rev[:12]} to `{t.branch}`.", "",
        *[f"- edit `{e}`" for e in plan.edits], *[f"- lock: {line}" for line in plan.lock], "",
        "## Reproducibility", "",
        f"`python scripts/standards.py fanout --repo {t.repo}` from Deliverome-Project/.github"
        f"@{std_rev[:12]}. Checks run before opening:", "",
        *[f"- {c}" for c in plan.checks], "",
        *[f"allow-fresh: {p}" for p in sorted(plan.fresh)],
    ])
    for cmd in (["git", "checkout", "-q", "-b", head], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", title], ["git", "push", "-q", "-u", "origin", head]):
        r = _run(cmd, root)
        if r.returncode != 0:
            plan.refusals.append(f"`{' '.join(cmd)}` failed: {r.stderr.strip()}")
            return
    plan.pr = gh("pr", "create", "-R", f"{org}/{t.repo}", "--base", t.branch, "--head", head,
                 "--title", title, "--body", body).strip()


def render_plan(p: Plan) -> str:
    t = p.target
    state = ("REFUSED" if p.refusals else "would open a PR" if p.changed and not p.pr
             else p.pr or "nothing to change")
    lines = [f"## {t.repo} `{t.branch}`: {state}", f"found by: {'; '.join(t.reasons)}"]
    lines += [f"- edit {e}" for e in p.edits] + [f"- lock {x}" for x in p.lock]
    lines += [f"- check {c}" for c in p.checks] + [f"- REFUSED: {r}" for r in p.refusals]
    lines += [f"- note: {n}" for n in p.notes]
    return "\n".join(lines) + "\n"


def cmd_fanout(args, std: Standards) -> int:
    org = std.discovery.get("org", "Deliverome-Project")
    targets = discover(std, args.repo, log=lambda m: print(m, file=sys.stderr))
    if args.branch:
        targets = [t for t in targets if t.branch in args.branch]
    problems = cloud_pin_problems(std)
    if problems:
        print("\n".join(f"Refusing: {x}" for x in problems))
        return 1
    print("Long-lived branches found:")
    for t in targets:
        print(f"- {t.repo}:{t.branch} ({'; '.join(t.reasons)})")
    print()
    fresh, expired = quarantine_sets(std, floor_ages(std))
    rev = _run(["git", "rev-parse", "HEAD"], HERE).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="standards-fanout-") as tmp:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            plans = list(pool.map(lambda t: fanout_target(org, t, std, fresh, expired, Path(tmp),
                                                          args.dry_run, rev), targets))
    for p in plans:
        print(render_plan(p))
    return 1 if any(p.refusals for p in plans) else 0


def cmd_drift(args, std: Standards) -> int:
    root = Path(args.root)
    repo = args.repo_name or (os.environ.get("GITHUB_REPOSITORY", "").rsplit("/", 1)[-1]
                              or root.resolve().name)
    found = drift(local_files(root), std, repo)
    for f in found:
        print(f"::error file={f.path},line={f.line}::{f}")
    print(f"Standards drift: {len(found)} finding(s) against Python {std.python}, "
          f"uv >= {std.uv_minimum}.")
    return 1 if found else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--standards", help="standards.toml (default: beside this script)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drift", help="check a local checkout")
    d.add_argument("--root", default=".")
    d.add_argument("--repo-name", help="repository name for [[exceptions]] (default: from env/dir)")
    r = sub.add_parser("report", help="check every long-lived branch through the API")
    r.add_argument("--repo", action="append", help="limit to these repositories")
    r.add_argument("--json", action="store_true")
    f = sub.add_parser("fanout", help="apply the standard and open one PR per branch")
    f.add_argument("--repo", action="append", help="limit to these repositories")
    f.add_argument("--branch", action="append", help="limit to these branch names")
    f.add_argument("--dry-run", action="store_true", help="print the plan; push nothing")
    f.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args(argv)
    std = load(args.standards)
    return {"drift": cmd_drift, "report": cmd_report, "fanout": cmd_fanout}[args.cmd](args, std)


if __name__ == "__main__":
    sys.exit(main())
