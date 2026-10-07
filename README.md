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

### Run locally

```bash
BASE_SHA=origin/main python scripts/dependency_guard.py
```

### Policy alongside it

- Dependabot version updates: weekly, grouped minor/patch, majors separate, `cooldown:
  default-days: 7`.
- Python resolvers: `[tool.uv] exclude-newer = "7 days"` so uv itself never picks a version
  under a week old.
