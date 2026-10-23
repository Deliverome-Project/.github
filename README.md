# Deliverome-Project/.github

Shared GitHub configuration for Deliverome repositories.

## Bump Python or pin a security fix: one change for every repo

`standards.toml` is the single place that says which Python every project targets, the
lowest uv allowed, and the security floors no lockfile may sit below.

1. Edit `standards.toml` (e.g. `[python] version = "3.15"`, or a new `[[floors]]` entry) in a
   pull request here, and merge it. From then on the dependency guard fails any pull request,
   in any repo, whose branch disagrees.
2. `python scripts/standards.py fanout --dry-run` lists every long-lived branch it found and,
   for each, what it would change, what the re-lock moved, and which checks passed.
3. `python scripts/standards.py fanout` opens one pull request per branch (`build(deps): apply
   Deliverome standards (...)`), with `allow-fresh:` lines for any floor still under a week old.
4. Merge them. `python scripts/standards.py report` shows what is left.

Fan-out runs locally with your `gh` login, `uv`, and `cargo` for Rust floors; it pushes only
`standards/<date>-<branch>` branches and never touches settings.

**What it changes.** The `requires-python` floor, `.python-version`, ruff `target-version`,
mypy `python_version`, `setup-python` versions and CI Python matrices (collapsed to the one
standard version), `[tool.uv] required-version`, `setup-uv` `version:` pins below the minimum,
and lockfiles below a floor (`uv lock --upgrade-package <pkg>==<floor>`, `cargo update
--precise`). While a floor is under 7 days old it adds `exclude-newer-package = { <pkg> = false
}` to the projects whose lock contains it, so `exclude-newer = "7 days"` lets uv pick it; once
the floor is a week old the next fan-out removes that exception.

It also keeps the Claude Code cloud hook in every repository with a `uv.lock`: cloud sessions
ship an older uv than `required-version` allows, so `.claude/hooks/ensure-uv.sh` (no-op locally)
installs the exact `[uv] cloud` release at session start. The script is rendered from
`managed/claude/ensure-uv.sh` with `MIN` = `[uv] minimum` and `PIN` = `[uv] cloud`, and its
SessionStart entry is added to `.claude/settings.json` if missing. Repositories keep a reviewed
copy rather than downloading the script at startup; drift fails a copy that is missing, edited,
or on other versions. `[uv] cloud` must be at least the minimum and at least 7 days old (unless it is
exactly the uv security floor), or fan-out refuses to run.

**What it refuses.** A branch is reported and left alone when the re-lock moves any package
other than the floors (a lock that re-resolves, as deliverome-internal's pandas/anndata did),
when `uv lock --check`, the repository's own age check (`scripts/check-fresh-deps.py`,
`scripts/check_dependency_age.py`) or the shared guard fails, or when a pin is in a form it
cannot rewrite. Packages that disappear are allowed, because a Python bump drops backports.

**What it only reports.** Required status checks whose names carry another Python version
(`test (3.12)`): rename the job or update the branch protection. Open pull requests by people
that already touch the same files.

**Which branches.** Discovered on every run, never listed: each Python repository's default
branch, protected branches, branches a ruleset covers, and any branch that was the base of at
least two merged pull requests in the last 90 days, which is what finds an unprotected
integration branch such as a `dev` that PRs merge into. `[discovery] include` / `exclude` in
`standards.toml` correct it by hand.

**Exceptions.** A project that must stay on another Python is an `[[exceptions]]` entry with
its reason (deliverome-internal `envs/census` stays on 3.13 until tiledbsoma ships 3.14 wheels).

**Quiet branches.** A branch nobody opens pull requests against never meets the guard, so
deliverome-internal runs `standards.py report` every Monday (`standards-drift.yml`) and emails
security@deliverome.org through the security digest's topic when anything drifts.

**Order the first time.** The guard workflow reads `standards.toml` from `main`, but its code
comes from the `v1` tag. After this lands: run fan-out, merge its pull requests, then move `v1`
so the drift check starts failing pull requests. Later edits to `standards.toml` need no tag
move.

## Dependency guard

A reusable workflow that fails a pull request when it

1. **silently downgrades** a package (a new or re-pinned dependency caps a version and the
   resolver goes backwards; this took deliverome-analysis from pandas 3.0.3 to 2.3.3 on
   2026-09-10 without anyone noticing), or
2. **newly locks a version under 7 days old**, transitive dependencies included (supply-chain
   attacks are most dangerous in their first week; Dependabot's cooldown only covers the
   package it bumps, and Dependabot security updates skip the cooldown).

3. **keeps a known-vulnerable version** anywhere in a lockfile, whether or not the PR touched
   it. `[[floors]]` in `standards.toml` lists each floor with its reason; raising one there
   reaches every calling repo on its next pull request. A version exactly on a floor is
   exempt from the age rule, since the fix may be days old.
4. **drifts from the standard Python or uv** (see above). `with: check-standards: false`
   turns this off for a repository that cannot follow it yet.

It reads every `uv.lock` (PyPI), `Cargo.lock` (crates.io) and `package-lock.json` (npm) in
the repository, compares each with the pull request's base, and looks up release dates from
the registries. Standard-library Python; no dependencies.

### Use

```yaml
# .github/workflows/dependency-guard.yml in the calling repo
name: dependency-guard
on:
  pull_request:
    types: [opened, synchronize, reopened, edited]  # edited: re-check after an allow line
permissions:
  contents: read
jobs:
  guard:
    uses: Deliverome-Project/.github/.github/workflows/dependency-guard.yml@v1
```

Make `guard / dependency-guard` a required status check on `main` so a failure blocks
merging.

### Deliberate exceptions

Write them in the pull request description, one per line; the check re-runs when the
description is edited:

```
allow-downgrade: pandas
allow-fresh: openssl-sys
```

### Security floors in force

See `[[floors]]` in `standards.toml`; each entry carries its reason.

### Run locally

```bash
BASE_SHA=origin/main python scripts/dependency_guard.py
```

### Policy alongside it

- Dependabot version updates: weekly, grouped minor/patch, majors separate, `cooldown:
  default-days: 7`.
- Python resolvers: `[tool.uv] exclude-newer = "7 days"` so uv itself never picks a version
  under a week old.

## Trust-store check (pre-push hook)

`scripts/trust_store_check.py` blocks a push while the machine still trusts a compromised CA
root (matched by SHA-256, in the macOS, Homebrew, Linux and repo `.venv` certifi bundles) or
runs a uv older than its fixed release. It prints the fix for each finding. Add it to a
repo's `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/Deliverome-Project/.github
  rev: <commit sha>  # v1
  hooks:
    - id: trust-store-check
```

and make sure contributors run `pre-commit install --hook-type pre-push`.

## Python release reminder

A reusable workflow that keeps one open issue, titled `Python upgrade: ...`, in the calling
repository while it targets an older Python than the newest release cycle that is at least
90 days old, or while its own cycle is under a year from end of life. The 90 days give
compiled scientific packages time to publish wheels: tiledbsoma still had none for 3.14 a
year after 3.14.0. The target is read from `.python-version`, then ruff's `target-version`,
then the `requires-python` floor; release dates come from endoflife.date. The issue lists the
cycles' support dates, every pin found, and a checklist for the move.

```yaml
# .github/workflows/python-release-check.yml in the calling repo
name: python-release-check
on:
  schedule:
    - cron: "0 14 1 * *"  # monthly
  workflow_dispatch:
permissions:
  contents: read
  issues: write
jobs:
  python-release:
    uses: Deliverome-Project/.github/.github/workflows/python-release-check.yml@<commit sha>
```

```bash
python scripts/python_release_check.py --dry-run --root ../some-repo
```
