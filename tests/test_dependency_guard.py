"""dependency_guard: lockfile parsing, version ordering and the downgrade/fresh rules (offline)."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "dependency_guard", Path(__file__).resolve().parents[1] / "scripts" / "dependency_guard.py"
)
dg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dg)

UV = """version = 1
revision = 3
[[package]]
name = "pandas"
version = "{pandas}"
source = {{ registry = "https://pypi.org/simple" }}
[[package]]
name = "agentflow-cytometry"
version = "0.7.1"
source = {{ git = "https://github.com/Deliverome-Project/agentflow.git?rev=abc" }}
"""
CARGO = """version = 4
[[package]]
name = "rand"
version = "{rand}"
source = "registry+https://github.com/rust-lang/crates.io-index"
[[package]]
name = "rand"
version = "0.7.3"
source = "registry+https://github.com/rust-lang/crates.io-index"
[[package]]
name = "limpa-rs"
version = "0.1.0"
"""


def npm(lodash, nested="4.0.0"):
    return json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "site"},
        "node_modules/lodash": {"version": lodash,
                                "resolved": "https://registry.npmjs.org/lodash/-/x.tgz"},
        "node_modules/a/node_modules/lodash": {"version": nested,
                                               "resolved": "https://registry.npmjs.org/lodash/-/y.tgz"},
        "packages/local": {"version": "1.0.0"},
        "node_modules/local": {"link": True, "resolved": "packages/local"},
    }})


@pytest.mark.parametrize("eco, older, newer", [
    ("pypi", "2.3.3", "3.0.3"), ("pypi", "3.0.0rc1", "3.0.0"), ("pypi", "1.0", "1.0.post1"),
    ("npm", "4.17.9", "4.17.21"), ("npm", "1.0.0-beta.2", "1.0.0-beta.11"),
    ("npm", "1.0.0-rc.1", "1.0.0"), ("crates", "0.8.5", "0.9.0"),
])
def test_version_order(eco, older, newer):
    assert dg.version_key(eco, older) < dg.version_key(eco, newer)


def test_uv_registry_only_and_downgrade():
    base, head = dg.parse_uv(UV.format(pandas="3.0.3")), dg.parse_uv(UV.format(pandas="2.3.3"))
    assert set(head) == {"pandas"}  # the git-pinned package is not a registry version
    down, changed = dg.compare(base, head, "pypi")
    assert down == [("pandas", "3.0.3", "2.3.3")] and changed == [("pandas", "2.3.3")]


def test_cargo_compatibility_lines_compared_separately():
    base = dg.parse_cargo(CARGO.format(rand="0.8.5"))
    head = dg.parse_cargo(CARGO.format(rand="0.8.4"))
    assert set(head) == {"rand@0.8", "rand@0.7"}  # local crate skipped, 0.7 and 0.8 apart
    assert dg.compare(base, head, "crates")[0] == [("rand", "0.8.5", "0.8.4")]


def test_npm_paths_links_and_nested_copies():
    base, head = dg.parse_npm(npm("4.17.21")), dg.parse_npm(npm("4.17.21", nested="3.10.1"))
    assert set(head) == {"node_modules/lodash", "node_modules/a/node_modules/lodash"}
    assert dg.compare(base, head, "npm")[0] == [("lodash", "4.0.0", "3.10.1")]


def test_allow_lines(monkeypatch):
    monkeypatch.setenv("PR_BODY", "Bumps x.\nallow-downgrade: pandas, matplotlib\nallow-fresh: rand")
    assert dg._allowed("downgrade") == {"pandas", "matplotlib"}
    assert dg._allowed("fresh") == {"rand"}


def test_main_end_to_end(tmp_path, monkeypatch):
    import subprocess

    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "uv.lock").write_text(UV.format(pandas="3.0.3"))
    git("add", ".")
    git("commit", "-qm", "base")
    (tmp_path / "uv.lock").write_text(UV.format(pandas="2.3.3"))
    git("add", ".")
    git("commit", "-qm", "head")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BASE_SHA", "HEAD~1")
    monkeypatch.setattr(dg, "release_time", lambda *a: datetime.now(tz=UTC) - timedelta(days=400))
    assert dg.main() == 1  # silent downgrade fails
    monkeypatch.setenv("PR_BODY", "allow-downgrade: pandas")
    assert dg.main() == 0
    monkeypatch.setattr(dg, "release_time", lambda *a: datetime.now(tz=UTC) - timedelta(days=2))
    assert dg.main() == 1  # still under 7 days old
    monkeypatch.setenv("PR_BODY", "allow-downgrade: pandas\nallow-fresh: pandas")
    assert dg.main() == 0
