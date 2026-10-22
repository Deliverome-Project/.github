"""python_release_check: target detection and when a reminder is raised (offline)."""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "python_release_check", Path(__file__).resolve().parents[1] / "scripts" / "python_release_check.py"
)
prc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prc)

CYCLES = [
    {"cycle": "3.15", "releaseDate": "2026-10-09", "support": "2028-10-09", "eol": "2031-10-31"},
    {"cycle": "3.14", "releaseDate": "2025-10-07", "support": "2027-10-01", "eol": "2030-10-31"},
    {"cycle": "3.13", "releaseDate": "2024-10-07", "support": "2026-10-01", "eol": "2029-10-31"},
    {"cycle": "3.12", "releaseDate": "2023-10-02", "support": "2025-04-02", "eol": "2028-10-31"},
]


def test_python_version_file_wins_over_other_pins(tmp_path):
    (tmp_path / ".python-version").write_text("3.14.9\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n[tool.ruff]\ntarget-version = "py313"\n'
    )
    target, pins = prc.detect_target(tmp_path)
    assert target == "3.14"
    assert pins == {".python-version": "3.14", "ruff target-version": "3.13", "requires-python floor": "3.12"}


def test_ruff_toml_and_requires_python_fallbacks(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')
    assert prc.detect_target(tmp_path)[0] == "3.11"
    (tmp_path / "ruff.toml").write_text('target-version = "py312"\n')
    assert prc.detect_target(tmp_path)[0] == "3.12"


def test_no_pins_means_no_target(tmp_path):
    assert prc.detect_target(tmp_path) == (None, {})


def test_a_new_cycle_is_ignored_until_it_is_old_enough():
    assert prc.assess("3.14", CYCLES, dt.date(2026, 10, 22), 90) is None
    reminder = prc.assess("3.14", CYCLES, dt.date(2027, 1, 8), 90)
    assert reminder["goal"] == "3.15"
    assert reminder["title"] == "Python upgrade: consider moving from Python 3.14 to 3.15"


def test_a_repo_behind_by_several_cycles_is_pointed_at_the_newest_adoptable_one():
    assert prc.assess("3.12", CYCLES, dt.date(2026, 10, 22), 90)["goal"] == "3.14"


def test_end_of_life_within_a_year_raises_a_reminder_even_without_a_newer_cycle():
    reminder = prc.assess("3.15", CYCLES, dt.date(2031, 1, 1), 90)
    assert reminder["goal"] is None
    assert reminder["title"] == "Python upgrade: Python 3.15 reaches end of life on 2031-10-31"


def test_render_lists_pins_and_newer_cycles_only():
    reminder = prc.assess("3.13", CYCLES, dt.date(2026, 10, 22), 90)
    body = prc.render(reminder, {".python-version": "3.13"}, CYCLES, 90)
    assert "| 3.13 (this repo) |" in body and "| 3.14 |" in body
    assert "| 3.12 |" not in body
    assert "- .python-version: 3.13" in body
