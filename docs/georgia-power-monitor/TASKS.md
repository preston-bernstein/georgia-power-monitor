# Tasks: Georgia Power Monitor

Generated from: docs/georgia-power-monitor/ on 2026-08-16

## Status legend
- [ ] pending
- [>] in progress
- [x] done
- [!] blocked

## Tasks

### Task 1: Add "georgia_power" to scraper-commons KNOWN_PLATFORMS
**Status**: [x] done
**Files**: ~/dev/scraper-commons/src/scraper_commons/cease/registry.py
**Test**: `python3 -c "from scraper_commons import cease; assert 'georgia_power' in cease.KNOWN_PLATFORMS"`; commit+push to scraper-commons origin/main
**Depends on**: none
**Parallelizable**: No
**Notes**: Commit 36565de, pushed to origin/main (NAS + GitHub mirror).

### Task 2: Create pyproject.toml with dependencies
**Status**: [x] done
**Files**: pyproject.toml
**Test**: `pip install -e .` in a clean venv succeeds; import smoke test succeeds
**Depends on**: Task 1
**Parallelizable**: No
**Notes**: scraper-commons git-pinned to commit 36565de. Verified via pip install -e . + import smoke test.

### Task 2a: Create .gitignore
**Status**: [x] done
**Files**: .gitignore
**Test**: `git check-ignore .env data/breaker_state.json` confirms both ignored
**Depends on**: Task 2
**Parallelizable**: Yes
**Notes**:

### Task 3: Implement config.py and log.py wrappers
**Status**: [x] done
**Files**: src/gp_monitor/config.py, src/gp_monitor/log.py, src/gp_monitor/__init__.py
**Test**: config/log import + load_config()/log_event() smoke test; no plaintext credentials logged
**Depends on**: Task 2
**Parallelizable**: Yes
**Notes**:

### Task 4: Implement auth.py
**Status**: [x] done
**Files**: src/gp_monitor/auth.py
**Test**: pytest retry-behavior tests (bounded retry, AuthFailedBounded, non-retryable exceptions not caught)
**Depends on**: Task 3
**Parallelizable**: Yes
**Notes**:

### Task 5: Implement collect.py
**Status**: [x] done
**Files**: src/gp_monitor/collect.py
**Test**: pytest tests for return shape, JWT-expiry re-fetch via awaited api.jwt, CollectFailed on error
**Depends on**: Task 3
**Parallelizable**: Yes
**Notes**:

### Task 6: Implement publish.py
**Status**: [x] done
**Files**: src/gp_monitor/publish.py
**Test**: pytest httpx-mocked tests for 3-entity publish, PublishFailed on 4xx, ISO timestamp format
**Depends on**: Task 3
**Parallelizable**: Yes
**Notes**:

### Task 7: Implement breaker.py
**Status**: [x] done
**Files**: src/gp_monitor/breaker.py
**Test**: pytest tests for trip/reset, file perms 0600, atomic write, no credentials in dump
**Depends on**: Task 3
**Parallelizable**: Yes
**Notes**:

### Task 8: Implement metrics.py
**Status**: [x] done
**Files**: src/gp_monitor/metrics.py
**Test**: textfile metrics written in valid Prometheus format, mode 0644
**Depends on**: Task 3
**Parallelizable**: Yes
**Notes**:

### Task 9: Create systemd/gp-monitor-poll.service and .timer
**Status**: [x] done
**Files**: systemd/gp-monitor-poll.service, systemd/gp-monitor-poll.timer
**Test**: unit files parse; OnCalendar=daily confirmed
**Depends on**: Task 2
**Parallelizable**: Yes
**Notes**:

### Task 10: Create ops/preflight.py
**Status**: [x] done
**Files**: ops/preflight.py
**Test**: HA reachability + token validity checks; exits 1 on failure with clear message; never touches Georgia Power
**Depends on**: Task 3, Task 6
**Parallelizable**: Yes
**Notes**:

### Task 11a: Implement cli.py — orchestration skeleton
**Status**: [x] done
**Files**: src/gp_monitor/cli.py
**Test**: happy-path pipeline mocked end-to-end, exit 0; exception in any upstream call exits 1
**Depends on**: Task 3, Task 4, Task 5, Task 6, Task 8
**Parallelizable**: No
**Notes**:

### Task 11b: Implement cli.py — guard integration
**Status**: [x] done
**Files**: src/gp_monitor/cli.py
**Test**: breaker/cease/rate-limiter guard scenarios (a-h) all pass, mocked
**Depends on**: Task 11a, Task 7
**Parallelizable**: No
**Notes**:

### Task 11c: Implement cli.py — --dry-run mode
**Status**: [x] done
**Files**: src/gp_monitor/cli.py
**Test**: --dry-run skips HA publish + metrics write but still performs real auth/collect calls (mocked in test)
**Depends on**: Task 11b
**Parallelizable**: No
**Notes**:

### Task 12: Create scripts/deploy.sh
**Status**: [x] done
**Files**: scripts/deploy.sh
**Test**: service user created, venv+pip install succeeds, systemd units copied, timer enabled
**Depends on**: Task 9, Task 11c
**Parallelizable**: No
**Notes**:

### Task 13: Create config.example.yaml and .env.example
**Status**: [x] done
**Files**: config.example.yaml, .env.example
**Test**: git grep/git log confirm no real credentials ever committed
**Depends on**: Task 11c, Task 12
**Parallelizable**: Yes
**Notes**:

### Task 14: Deploy to selected host [HUMAN-GATED]
**Status**: [ ] pending
**Files**: (none; runs deploy.sh)
**Test**: service user + venv + systemd units in place; timer enabled; PASS only if .env with real secrets is provided — otherwise report blocked-on-Preston
**Depends on**: Task 12, Task 13
**Parallelizable**: No
**Notes**: Requires Preston-supplied real GP credentials + HA long-lived token. Cannot be completed autonomously.

### Task 15: Verify systemd timer runs successfully [HUMAN-GATED]
**Status**: [ ] pending
**Files**: (none; verification only)
**Test**: outcome=success in journalctl — PASS only on real success, not on expected auth failure with missing .env
**Depends on**: Task 14
**Parallelizable**: No
**Notes**: Requires Task 14 complete with real credentials first.

### Task 16: Run integration test with real credentials [HUMAN-GATED]
**Status**: [ ] pending
**Files**: (none; verification only)
**Test**: both HA entities show live values; last_poll timestamp fresh; outcome=success
**Depends on**: Task 15
**Parallelizable**: No
**Notes**: Final live-data confirmation, requires Preston's real account.

## Blocked / open
(populated during implementation)
