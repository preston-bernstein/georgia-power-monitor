#!/usr/bin/env python3
"""check_deploy_clean.py -- refuse to let deploy.sh ship uncommitted files.

WHY THIS EXISTS. On 2026-08-29 a concurrent session's uncommitted, broken Alloy config was
rsynced to the desktop by a routine home-infra deploy and killed all Loki log shipping.
home-infra's deploy scripts gained a repo-side gate for this
(tools/config-drift/preflight.sh + drift_audit.py's --check-shipped-clean, see home-infra
ADR 0019 and PR #104) -- this repo's deploy.sh had no equivalent at all.

WHY A SEPARATE SCRIPT INSTEAD OF REUSING home-infra's drift_audit.py DIRECTLY. That tool's
fleet.json registry is a strict, all-repos-loaded-every-call contract: check_fleet_coverage()
loads and shape-validates every registered repo's manifest before any component-scoped
lookup happens (see its own module, load_fleet()/check_fleet_coverage()). Registering this
repo there would mean a coverage gap in ITS manifest -- a systemd unit added here without a
matching manifest entry -- could sys.exit(2) home-infra's OWN, unrelated deploys the next
time anyone runs a plain (non---check-shipped-clean) audit, since --component scoping is
applied only after that fleet-wide load. That is real coupling between repos that don't
share a deploy, own each other's release cadence, or review each other's changes -- the
opposite of "stay polyrepo" (home-infra CONVENTIONS.md #8: shared *libraries* are imported
and versioned from a dedicated lib repo; this repo has no such dependency on home-infra
today, and inventing one only to borrow ~20 lines of git-status logic is exactly the
speculative cross-repo coupling that convention warns against). A second, harder blocker:
algo-macro-monitor's and georgia-power-monitor's deploy.sh run directly ON the target host
(desktop-agent/xps-agent), which does not have home-infra checked out at all (verified
2026-08-29) -- a cross-repo call would simply fail there. Keeping the SAME small, dependency-
free check colocated in every repo it protects means one mechanism, no assumption about what
else is checked out on the box actually running the deploy, and no shared failure mode.

This keeps the two properties that make home-infra's gate work:
  - SCOPED, not repo-wide: only the paths given on argv are checked. deploy.sh passes
    exactly what it is about to ship (kept in sync by hand with its own rsync/install
    commands -- see the comment at the top of deploy.sh's gate step). Dirt anywhere else in
    the repo does not block, so this does not become something worth permanently skipping.
  - FAIL CLOSED: if git cannot answer (not a checkout, git missing, a git error), this exits
    2 -- "cannot tell" must never be read as "clean" (ADR 0019's rule, unchanged here).

Escape hatch: DEPLOY_GIT_GATE=skip, checked by deploy.sh itself (not here) -- loudly banners
so "I always set that" stays uncomfortable.

Usage: check_deploy_clean.py <repo-relative-path> [<path> ...]
Exit codes: 0 clean, 1 dirty (names every file), 2 undeterminable -- refuses to deploy blind.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def dirty_paths(repo_root: str, paths: list[str]) -> list[str] | None:
    """Repo-relative paths among `paths` that are uncommitted (staged, unstaged, or
    untracked) per plain local `git status`, or None if that cannot be determined.

    Mirrors home-infra's tools/config-drift/drift_audit.py:component_dirty_paths() --
    same porcelain-v1 parsing (two status chars, a space, then the path; a rename reports
    "ORIG -> NEW", and the destination is what would actually be shipped), same
    None-not-False-for-undeterminable contract.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "status", "--porcelain", "--", *paths],
            capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0:
        return None

    dirty = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        dirty.add(path.strip().strip('"'))
    return sorted(dirty)


def main(argv: list[str]) -> int:
    if not argv:
        print("check_deploy_clean.py: usage: check_deploy_clean.py <path> [<path> ...]",
              file=sys.stderr)
        return 2

    repo_root = str(Path(__file__).resolve().parent.parent)
    dirty = dirty_paths(repo_root, argv)

    if dirty is None:
        print(f"check_deploy_clean.py: could not read git status in {repo_root} -- "
              "cannot tell whether what this deploy ships is committed.", file=sys.stderr)
        return 2

    if dirty:
        print(f"UNCOMMITTED: {len(dirty)} file(s) this deploy would ship are not "
              f"committed in {repo_root}:")
        for path in dirty:
            print(f"    {path}")
        return 1

    print("    committed -- every file this deploy ships is in git")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
