"""Remind a repository when a newer stable Python is worth adopting.

Reads the Python the repository targets (``.python-version``, then ruff's
``target-version``, then the ``requires-python`` floor), compares it with the release
cycles on endoflife.date, and keeps one open issue titled ``Python upgrade: ...`` while the
repository is behind. A cycle only counts once it is ``MIN_CYCLE_AGE_DAYS`` old (default 90),
because compiled scientific packages take months to publish wheels for a new Python:
tiledbsoma had none for 3.14 a year after 3.14.0. The issue is also opened when the target's
end of life is under a year away.

Standard-library Python; ``gh`` does the issue writes. ``--dry-run`` prints instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import tomllib
import urllib.request
from pathlib import Path

CYCLES_URL = "https://endoflife.date/api/python.json"
TITLE_PREFIX = "Python upgrade:"
EOL_WARNING_DAYS = 365


def version_key(cycle: str) -> tuple[int, ...]:
    return tuple(int(part) for part in cycle.split("."))


def detect_target(root: Path) -> tuple[str | None, dict[str, str]]:
    """Return the targeted X.Y and every pin found, keyed by where it was found."""
    pins: dict[str, str] = {}
    python_version = root / ".python-version"
    if python_version.exists():
        match = re.match(r"\s*(3\.\d+)", python_version.read_text())
        if match:
            pins[".python-version"] = match.group(1)
    pyproject_path = root / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text()) if pyproject_path.exists() else {}
    ruff = pyproject.get("tool", {}).get("ruff", {})
    if not ruff.get("target-version") and (root / "ruff.toml").exists():
        ruff = tomllib.loads((root / "ruff.toml").read_text())
    match = re.fullmatch(r"py3(\d+)", ruff.get("target-version", ""))
    if match:
        pins["ruff target-version"] = f"3.{match.group(1)}"
    match = re.search(r">=\s*(3\.\d+)", pyproject.get("project", {}).get("requires-python", ""))
    if match:
        pins["requires-python floor"] = match.group(1)
    for source in (".python-version", "ruff target-version", "requires-python floor"):
        if source in pins:
            return pins[source], pins
    return None, pins


def assess(target: str, cycles: list[dict], today: dt.date, min_age_days: int) -> dict | None:
    """Return the reminder to raise, or None when the target is current."""
    by_cycle = {c["cycle"]: c for c in cycles}
    adoptable = [
        c for c in cycles
        if dt.date.fromisoformat(c["releaseDate"]) <= today - dt.timedelta(days=min_age_days)
    ]
    newest = max(adoptable, key=lambda c: version_key(c["cycle"]), default=None)
    current = by_cycle.get(target)
    eol = dt.date.fromisoformat(current["eol"]) if current and isinstance(current["eol"], str) else None
    behind = newest is not None and version_key(newest["cycle"]) > version_key(target)
    eol_soon = eol is not None and eol - today <= dt.timedelta(days=EOL_WARNING_DAYS)
    if not behind and not eol_soon:
        return None
    goal = newest["cycle"] if behind else None
    title = f"{TITLE_PREFIX} consider moving from Python {target} to {goal}" if goal else (
        f"{TITLE_PREFIX} Python {target} reaches end of life on {eol}"
    )
    return {"title": title, "target": target, "goal": goal, "current": current, "newest": newest,
            "eol_soon": eol_soon}


def render(reminder: dict, pins: dict[str, str], cycles: list[dict], min_age_days: int) -> str:
    lines = [
        f"This repository targets **Python {reminder['target']}**.",
        "",
        "| Cycle | Released | Bugfix support until | End of life |",
        "|---|---|---|---|",
    ]
    for c in sorted(cycles, key=lambda c: version_key(c["cycle"]), reverse=True):
        if version_key(c["cycle"]) < version_key(reminder["target"]):
            continue
        mark = " (this repo)" if c["cycle"] == reminder["target"] else ""
        lines.append(f"| {c['cycle']}{mark} | {c['releaseDate']} | {c['support']} | {c['eol']} |")
    lines += ["", "Python pins found:", ""]
    lines += [f"- {source}: {version}" for source, version in pins.items()]
    lines += [
        "",
        f"A cycle is only suggested once it is {min_age_days} days old, so compiled dependencies "
        "have had time to publish wheels for it.",
        "",
        "Before upgrading:",
        "",
        "- [ ] Re-lock with the new `requires-python` floor and check every locked package has a "
        "wheel for the new Python on macOS arm64 and Linux x86_64 (an sdist-only compiled "
        "package will try to build from source, or fail to install).",
        "- [ ] Move every pin together: `.python-version`, `requires-python`, ruff "
        "`target-version`, CI `python-version` / `uv python install`, Modal "
        "`debian_slim(python_version=...)` and Lambda runtimes.",
        "- [ ] Run the test suite on the new Python.",
        "",
        "This issue is kept up to date by the `python-release-check` workflow; close it once "
        "the repository has moved, or if you decide to stay.",
    ]
    return "\n".join(lines)


def open_reminders(repo: str) -> list[dict]:
    out = subprocess.run(
        ["gh", "issue", "list", "-R", repo, "--state", "open", "--search", f'"{TITLE_PREFIX}" in:title',
         "--json", "number,title,body"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [i for i in json.loads(out) if i["title"].startswith(TITLE_PREFIX)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--min-age-days", type=int, default=int(os.environ.get("MIN_CYCLE_AGE_DAYS", 90)))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    target, pins = detect_target(args.root)
    if target is None:
        print("No Python target found (.python-version, ruff target-version or requires-python).")
        return 1
    with urllib.request.urlopen(CYCLES_URL, timeout=30) as response:
        cycles = json.load(response)
    reminder = assess(target, cycles, dt.date.today(), args.min_age_days)
    if reminder is None:
        print(f"Python {target} is current; nothing to do.")
        return 0
    body = render(reminder, pins, cycles, args.min_age_days)
    if args.dry_run:
        print(reminder["title"], body, sep="\n\n")
        return 0

    repo = os.environ["GITHUB_REPOSITORY"]
    existing = open_reminders(repo)
    if existing:
        issue = existing[0]
        if issue["title"] != reminder["title"] or issue["body"] != body:
            subprocess.run(["gh", "issue", "edit", str(issue["number"]), "-R", repo,
                            "--title", reminder["title"], "--body", body], check=True)
        print(f"Updated #{issue['number']}: {reminder['title']}")
    else:
        subprocess.run(["gh", "issue", "create", "-R", repo, "--title", reminder["title"],
                        "--body", body], check=True)
        print(f"Opened: {reminder['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
