# Deliverome-Project/.github

Shared GitHub configuration for Deliverome repositories.

## Dependency guard

A reusable workflow that fails a pull request when it

1. **silently downgrades** a package (a new or re-pinned dependency caps a version and the
   resolver goes backwards; this took deliverome-analysis from pandas 3.0.3 to 2.3.3 on
   2026-09-10 without anyone noticing), or
2. **newly locks a version under 7 days old**, transitive dependencies included (supply-chain
   attacks are most dangerous in their first week; Dependabot's cooldown only covers the
   package it bumps, and Dependabot security updates skip the cooldown).

3. **keeps a known-vulnerable version** anywhere in a lockfile, whether or not the PR touched
   it. `SECURITY_FLOORS` in `scripts/dependency_guard.py` lists each floor with its reason;
   raising one there reaches every calling repo on its next pull request. A version exactly
   on a floor is exempt from the age rule, since the fix may be days old.

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

| Ecosystem | Package | Floor | Reason |
|---|---|---|---|
| PyPI | certifi | 2026.10.19 | eMudhra intermediate-CA key compromise, 2026-10-19 |
| PyPI | pip | 26.2.2 | same |
| PyPI | uv | 0.13.4 | same |
| crates.io | webpki-roots, webpki-root-certs | 1.0.10 | same |
| crates.io | rustls-webpki | 0.103.16 | same |

A Python repo using `exclude-newer = "7 days"` also needs
`exclude-newer-package = { certifi = false }` until the fix is a week old, or uv cannot
resolve it.

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
