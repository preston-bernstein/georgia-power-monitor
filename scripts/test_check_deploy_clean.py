"""pytest suite for check_deploy_clean.py: dirty_paths() and the --check-shipped-clean-
style CLI it backs.

Mirrors internal-infra's tools/config-drift/test_shipped_clean_gate.py -- same fixture shape,
same two properties pinned, adapted to this repo's simpler path-list API (no manifest
component dict, since this repo does not register with internal-infra's fleet.json -- see the
module docstring in check_deploy_clean.py for why).

The false-block half is as load-bearing as the block half: a gate that fires on unrelated
dirt gets bypassed with DEPLOY_GIT_GATE=skip every time and then protects nothing -- see
test_unrelated_dirty_file_does_not_block.

No SSH, no network: every test builds a real throwaway git repo under tmp_path and runs
real local `git` against it, mirroring how the gate runs in deploy.sh (local only).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_deploy_clean as gate  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed git repo with a shipped file, a shipped directory, and unrelated
    content that no deploy ships."""
    r = tmp_path / "repo"
    (r / "shipped_dir").mkdir(parents=True)
    (r / "shipped_dir" / "a.txt").write_text("a\n")
    (r / "shipped_file.txt").write_text("shipped\n")
    (r / "unrelated_dir").mkdir()
    (r / "unrelated_dir" / "notes.md").write_text("not shipped by anything\n")
    (r / "unrelated_file.txt").write_text("also not shipped\n")

    git(r.parent, "init", "-q", "repo")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    return r


SHIPPED = ["shipped_dir", "shipped_file.txt"]


# --- dirty_paths() -----------------------------------------------------------

def test_clean_repo_reports_no_dirty_paths(repo: Path):
    assert gate.dirty_paths(str(repo), SHIPPED) == []


def test_modified_shipped_file_is_reported(repo: Path):
    (repo / "shipped_file.txt").write_text("changed\n")
    assert gate.dirty_paths(str(repo), SHIPPED) == ["shipped_file.txt"]


def test_untracked_file_under_a_shipped_dir_is_reported(repo: Path):
    (repo / "shipped_dir" / "new.txt").write_text("new\n")
    assert gate.dirty_paths(str(repo), SHIPPED) == ["shipped_dir/new.txt"]


def test_staged_but_uncommitted_change_is_reported(repo: Path):
    (repo / "shipped_file.txt").write_text("staged\n")
    git(repo, "add", "shipped_file.txt")
    assert gate.dirty_paths(str(repo), SHIPPED) == ["shipped_file.txt"]


def test_unrelated_dirty_file_does_not_block(repo: Path):
    """The anti-cry-wolf property. Dirt outside what this deploy ships must never block,
    or the gate gets DEPLOY_GIT_GATE=skip'd permanently within a week."""
    (repo / "unrelated_file.txt").write_text("edited while another session works\n")
    (repo / "unrelated_dir" / "notes.md").write_text("edited too\n")
    assert gate.dirty_paths(str(repo), SHIPPED) == []


def test_multiple_dirty_shipped_paths_are_all_reported_sorted(repo: Path):
    (repo / "shipped_file.txt").write_text("changed\n")
    (repo / "shipped_dir" / "a.txt").write_text("changed\n")
    assert gate.dirty_paths(str(repo), SHIPPED) == [
        "shipped_dir/a.txt", "shipped_file.txt"]


def test_non_git_directory_is_undeterminable_not_clean(tmp_path: Path):
    """ADR 0019's rule, unchanged here: unable-to-detect must never read as no-drift."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gate.dirty_paths(str(plain), SHIPPED) is None


def test_git_binary_missing_is_undeterminable(repo: Path, monkeypatch):
    def boom(*a, **k):
        raise OSError("git not found")
    monkeypatch.setattr(gate.subprocess, "run", boom)
    assert gate.dirty_paths(str(repo), SHIPPED) is None


# --- CLI ----------------------------------------------------------------------
# check_deploy_clean.py resolves its own repo root from its OWN file location (it is
# meant to live at <repo>/scripts/check_deploy_clean.py and check that same repo, not an
# arbitrary target passed on argv), so CLI-level tests copy the real script into a
# throwaway repo's scripts/ dir rather than pointing it at one via an argument.

def run_cli(repo: Path, *paths: str) -> subprocess.CompletedProcess:
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    script_copy = scripts_dir / "check_deploy_clean.py"
    script_copy.write_text(Path(gate.__file__).read_text())
    return subprocess.run([sys.executable, str(script_copy), *paths],
                           capture_output=True, text=True)


def test_cli_exits_0_when_shipped_paths_are_clean(repo: Path):
    proc = run_cli(repo, *SHIPPED)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_exits_1_and_names_the_file_when_dirty(repo: Path):
    (repo / "shipped_dir" / "a.txt").write_text("broken\n")
    proc = run_cli(repo, *SHIPPED)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "shipped_dir/a.txt" in (proc.stdout + proc.stderr)


def test_cli_exits_2_with_no_args(repo: Path):
    proc = run_cli(repo)
    assert proc.returncode == 2, proc.stdout + proc.stderr
