"""standards: drift detection, rewrites, lock policing and branch discovery (offline)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import standards as st  # noqa: E402

STD = st.Standards(
    python="3.14",
    uv_minimum="0.13.4",
    floors=(st.Floor("pypi", "certifi", "2026.10.19", "eMudhra"),
            st.Floor("pypi", "pip", "26.2.2", "eMudhra"),
            st.Floor("crates", "webpki-roots", "1.0.10", "eMudhra")),
    exceptions=({"repo": "deliverome-internal", "path": "envs/census", "python": "3.13",
                 "reason": "tiledbsoma"},),
    discovery={},
)

PYPROJECT = """[project]
name = "x"
requires-python = ">=3.12"

[tool.uv]
required-version = ">=0.11.10"
exclude-newer = "7 days"

[tool.ruff]
target-version = "py312"
"""

WORKFLOW = """jobs:
  test:
    strategy:
      matrix:
        python: ['3.11', '3.12']
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          version: "0.11.10"
      - run: echo version: 0.1
  other:
    strategy:
      matrix:
        python-version:
          - "3.11"
          - "3.12"
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - uses: astral-sh/setup-uv@v7
"""


def lock(**pins):
    blocks = "".join(f'[[package]]\nname = "{n}"\nversion = "{v}"\n'
                     'source = { registry = "https://pypi.org/simple" }\n' for n, v in pins.items())
    return "version = 1\nrevision = 3\n" + blocks


def test_shipped_standards_file_loads():
    std = st.load(ROOT / "standards.toml")
    assert std.python and std.uv_minimum and std.floors
    assert std.python_for("deliverome-internal", "envs/census/pyproject.toml") == "3.13"


def test_exception_covers_path_and_below_only():
    assert STD.python_for("deliverome-internal", "envs/census/.python-version") == "3.13"
    assert STD.python_for("deliverome-internal", "envs/census") == "3.13"
    assert STD.python_for("deliverome-internal", "envs/census2/pyproject.toml") == "3.14"
    assert STD.python_for("other", "envs/census/pyproject.toml") == "3.14"


def test_drift_finds_every_pin():
    files = {"pyproject.toml": PYPROJECT, "uv.lock": lock(), ".python-version": "3.12\n",
             ".github/workflows/ci.yml": WORKFLOW}
    found = {(f.path, f.what, f.found) for f in st.drift(files, STD, "agentflow")}
    assert found == {
        ("pyproject.toml", "requires-python floor", "3.12"),
        ("pyproject.toml", "uv required-version", ">=0.11.10"),
        ("pyproject.toml", "ruff target-version", "3.12"),
        (".python-version", ".python-version", "3.12"),
        (".github/workflows/ci.yml", "Python matrix", "3.11, 3.12"),
        (".github/workflows/ci.yml", "setup-python version", "3.12"),
        (".github/workflows/ci.yml", "setup-uv version", "0.11.10"),
    } | {(".github/workflows/ci.yml", "Python matrix", "3.11, 3.12")}
    assert sum(f.what == "Python matrix" for f in st.drift(files, STD, "agentflow")) == 2


def test_missing_required_version_only_matters_with_a_lock():
    text = '[project]\nrequires-python = ">=3.14"\n'
    assert st.drift({"pyproject.toml": text}, STD, "r") == []
    found = st.drift({"pyproject.toml": text, "uv.lock": lock()}, STD, "r")
    assert [(f.what, f.found) for f in found] == [("uv required-version", "missing")]


def test_conforming_tree_and_exception_have_no_drift():
    census = PYPROJECT.replace(">=3.12", ">=3.13,<3.14").replace("0.11.10", "0.13.4") \
        .replace("py312", "py313")
    files = {"envs/census/pyproject.toml": census, "envs/census/uv.lock": lock(),
             "envs/census/.python-version": "3.13\n"}
    assert st.drift(files, STD, "deliverome-internal") == []
    assert st.drift(files, STD, "flashdia")  # the exception is per repository


def test_rewrite_workflow_scalars_matrices_and_uv_pins():
    out = st.rewrite_workflow(WORKFLOW, "3.14", "0.13.4")
    assert "python: ['3.14']" in out
    assert 'python-version: "3.14"' in out
    assert 'version: "0.13.4"' in out
    assert "echo version: 0.1" in out  # a run line is not a setup-uv pin
    assert '- "3.14"\n' in out and '"3.11"' not in out and '"3.12"' not in out
    assert "${{ matrix.python-version }}" in out
    assert st.drift({".github/workflows/ci.yml": out}, STD, "r") == []


def test_rewrite_workflow_keeps_newer_uv_pin():
    text = "    steps:\n      - uses: astral-sh/setup-uv@v7\n        with:\n          version: '0.14.0'\n"
    assert st.rewrite_workflow(text, "3.14", "0.13.4") == text


def test_rewrite_pyproject_and_quarantine_exceptions():
    out = st.rewrite_pyproject(PYPROJECT, "3.14", "0.13.4", True, {"certifi"}, set())
    assert 'requires-python = ">=3.14"' in out
    assert 'required-version = ">=0.13.4"' in out
    assert 'target-version = "py314"' in out
    assert 'exclude-newer = "7 days"\nexclude-newer-package = { certifi = false }\n' in out
    # a week later the exception goes; unrelated entries stay as written
    later = out.replace("{ certifi = false }", "{ certifi = false, torch = false }")
    after = st.rewrite_pyproject(later, "3.14", "0.13.4", True, set(), {"certifi"})
    assert "exclude-newer-package = { torch = false }" in after
    gone = st.rewrite_pyproject(out, "3.14", "0.13.4", True, set(), {"certifi"})
    assert "exclude-newer-package" not in gone
    assert st.drift({"pyproject.toml": out, "uv.lock": lock()}, STD, "r") == []


def test_rewrite_pyproject_adds_required_version_when_missing():
    text = '[project]\nrequires-python = ">=3.14"\n'
    out = st.rewrite_pyproject(text, "3.14", "0.13.4", True, set(), set())
    assert out.endswith('[tool.uv]\n\nrequired-version = ">=0.13.4"')
    assert st.rewrite_pyproject(text, "3.14", "0.13.4", False, set(), set()) == text


def test_rewrite_python_version_keeps_patch_form():
    assert st.rewrite_python_version("3.12.4\n", "3.14") == "3.14\n"
    assert st.rewrite_python_version("3.12\n", "3.14") == "3.14\n"


def test_apply_adds_exception_only_where_the_lock_has_the_package():
    files = {"pyproject.toml": PYPROJECT, "uv.lock": lock(certifi="2026.5.20"),
             "sub/pyproject.toml": PYPROJECT, "sub/uv.lock": lock(numpy="2.0.0")}
    out = st.apply(files, STD, "r", {"certifi", "pip"}, set())
    assert "exclude-newer-package = { certifi = false }" in out["pyproject.toml"]
    assert "exclude-newer-package" not in out["sub/pyproject.toml"]
    assert "uv.lock" not in out


def test_apply_respects_exception():
    census = PYPROJECT.replace(">=3.12", ">=3.13,<3.14")
    out = st.apply({"envs/census/pyproject.toml": census}, STD, "deliverome-internal", set(), set())
    assert 'requires-python = ">=3.13,<3.14"' in out["envs/census/pyproject.toml"]


def test_below_floors_and_lock_changes():
    old = lock(certifi="2026.5.20", numpy="2.0.0", tomli="2.0.1")
    assert [(n, v) for n, v, _ in st.below_floors(old, "pypi", STD)] == [("certifi", "2026.5.20")]
    good = lock(certifi="2026.10.19", numpy="2.0.0")
    assert st.lock_changes(old, good, "pypi", {"certifi"}) == (
        [("certifi", "2026.5.20", "2026.10.19")], [], [("tomli", "2.0.1")])
    drifted = lock(certifi="2026.10.19", numpy="2.1.0", pytz="2025.1")
    _, unexpected, _ = st.lock_changes(old, drifted, "pypi", {"certifi"})
    assert unexpected == [("numpy", "2.0.0", "2.1.0"), ("pytz", None, "2025.1")]


@pytest.mark.parametrize("include, exclude, expect", [
    (["~DEFAULT_BRANCH"], [], {"main"}),
    (["refs/heads/main", "refs/heads/dev"], [], {"main", "dev"}),
    (["refs/heads/release/*"], [], {"release/1"}),
    (["~ALL"], ["refs/heads/claude/*"], {"main", "dev", "release/1"}),
])
def test_ruleset_branches(include, exclude, expect):
    branches = ["main", "dev", "release/1", "claude/x"]
    assert st.ruleset_branches(include, exclude, branches, "main") == expect


def test_discover_finds_unprotected_integration_branch(monkeypatch):
    """agentflow dev: unprotected, not default, but the base of merged PRs."""
    def fake_gh(*args, check=True):
        a = " ".join(args)
        if "git/trees/main" in a:
            return "pyproject.toml\nuv.lock\n"
        if a.startswith("pr list"):
            return "main\nmain\ndev\ndev\nstacked\n"
        return ""

    def fake_lines(path, jq):
        if path.startswith("orgs/"):
            return ["agentflow main"]
        if "protected=true" in path:
            return ["main"]
        if "/branches" in path:
            return ["main", "dev", "stacked", "claude/x"]
        return []

    monkeypatch.setattr(st, "gh", fake_gh)
    monkeypatch.setattr(st, "gh_lines", fake_lines)
    std = st.Standards("3.14", "0.13.4", (), (), {"min_merged_prs": 2, "include": ["agentflow:x"],
                                                  "exclude": ["agentflow:x"]})
    got = {t.branch: t.reasons for t in st.discover(std, log=lambda m: None)}
    assert set(got) == {"main", "dev"}
    assert got["main"][:2] == ["default", "protected"]
    assert got["dev"][0].startswith("base of 2 merged PRs")


def test_stale_exceptions():
    text = PYPROJECT.replace('exclude-newer = "7 days"\n',
                             'exclude-newer = "7 days"\nexclude-newer-package = { certifi = false }\n')
    assert st.stale_exceptions({"pyproject.toml": text}, {"certifi"})
    assert not st.stale_exceptions({"pyproject.toml": text}, {"pip"})


def test_guard_reads_floors_and_reports_drift(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(PYPROJECT)
    (repo / "uv.lock").write_text(lock(certifi="2026.10.19"))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GITHUB_REPOSITORY", "Deliverome-Project/r")
    spec = importlib.util.spec_from_file_location("dg2", ROOT / "scripts" / "dependency_guard.py")
    dg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dg)
    assert dg.SECURITY_FLOORS[("pypi", "certifi")][0] == st.load(ROOT / "standards.toml").floor(
        "pypi", "certifi").version
    errors = dg.standards_drift()
    assert any("requires-python floor is 3.12" in e for e in errors)
