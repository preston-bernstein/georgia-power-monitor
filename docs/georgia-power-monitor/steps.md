# Steps: Georgia Power Monitor

## Prerequisites

1. **Verify Georgia Power account MFA status** — Preston must personally verify whether his Georgia Power account has MFA enabled (the `southern-company-api` library doesn't support MFA/CAPTCHA at all) and either disable it or accept standing manual-intervention risk. This is a human decision about his own account security — not an automatable build step. Confirm with Preston before Steps 15-16 (the live-credential steps).

2. **Select managed deploy host** — Decide between desktop (10.0.0.243) or xps-agent (10.0.0.244) based on current load, LAN reachability to Home Assistant (10.0.0.5), and spare capacity. xps-agent currently runs arr-stack + fashion-monitor; desktop is the preferred default if it has capacity. This choice determines where step 14 installs the service user and systemd units.

3. **Verify Home Assistant accessibility** — Confirm 10.0.0.5:8123 is reachable from the selected deploy host over the LAN (no proxy/firewall blocking HTTP to the HA port). No long-lived access token yet (it will be provided by Preston at deploy time).

## Implementation steps

### Step 1: Add "georgia_power" to scraper-commons KNOWN_PLATFORMS
**What**: Add `"georgia_power"` to the closed set of platforms in `scraper-commons/src/scraper_commons/cease/registry.py`'s `KNOWN_PLATFORMS` dict (which is re-exported by `cease/__init__.py`) so that `cease.is_halted("georgia_power")` calls in this repo do not raise `ValueError`.

**Files**: `~/dev/scraper-commons/src/scraper_commons/cease/registry.py`

**Test**: Run `python3 -c "from scraper_commons import cease; assert 'georgia_power' in cease.KNOWN_PLATFORMS"` on the desktop. Should complete without error. Then verify the change is committed and pushed to the scraper-commons remote (`git log -1 --oneline`, `git remote -v`, confirm origin/main is updated).

**Depends on**: None.

**Parallelizable**: No (must complete before step 2 can be committed; while work-independent, git history depends on sequencing).

**Rollback**: `cd ~/dev/scraper-commons && git revert -n <commit_hash> && git commit` (revert the addition, push the revert to origin/main).

### Step 2: Create pyproject.toml with dependencies
**What**: Create the root `pyproject.toml` scaffold with dependencies on `southern-company-api`, `scraper-commons` (git-pinned, `rate` + `cease` modules only, no browser extras), `fleet-logging` (git-pinned), `click`, `httpx`, `pydantic`, and test/dev tooling (`pytest`, `pytest-asyncio`).

**Files**: `pyproject.toml`

**Test**: Run `pip install -e .` in the repo root in a clean venv; should complete without dependency conflicts. Then `python3 -c "import southern_company_api, scraper_commons.rate, scraper_commons.cease, fleet_logging, click, httpx, pydantic"` should complete without import errors.

**Depends on**: Step 1 (the git-pinned scraper-commons ref will include "georgia_power" in KNOWN_PLATFORMS; step 1 must be pushed before this repo pins the SHA).

**Parallelizable**: No (prerequisite for any Python development steps).

### Step 2a: Create .gitignore
**What**: Create `.gitignore` covering `.env`, `data/`, `venv/`/`.venv/`, `__pycache__/`, `*.pyc`, and `.pytest_cache/` so that credentials and runtime state are never committed.

**Files**: `.gitignore`

**Test**: `git status` after creating dummy `.env` and `data/breaker_state.json` files shows them as untracked/ignored, not staged. Run `git check-ignore .env data/breaker_state.json` to verify both are in the ignore list.

**Depends on**: Step 2.

**Parallelizable**: Yes.

### Step 3: Implement config.py and log.py wrappers
**What**: Create `src/gp_monitor/config.py` and `src/gp_monitor/log.py` as thin wrappers around `fleet-logging`, copied verbatim from `algo-macro-monitor/src/macro_monitor/{config,log}.py` and adapted with `env_prefix="GP_MONITOR_"` and appropriate module names. Both modules implement the same stealth-credential guard (`_NO_DOTENV`) to prevent `.env` file tree-walking.

**Files**: `src/gp_monitor/config.py`, `src/gp_monitor/log.py`, `src/gp_monitor/__init__.py`

**Test**: `python3 -c "from gp_monitor.config import settings, load_config; from gp_monitor.log import log_event; load_config(); log_event('test', phase='config')"` should complete without errors (requires a valid `config.yaml` to exist or be mocked; use `pytest` to test with fixtures). Verify no plaintext credentials are logged by inspecting stderr.

**Depends on**: Step 2.

**Parallelizable**: Yes (both files are independent thin wrappers; can be implemented and tested in parallel).

### Step 4: Implement auth.py
**What**: Create `src/gp_monitor/auth.py` — a wrapper around `southern_company_api.SouthernCompanyAPI(username, password, session)` that performs login with bounded retry. Reads `GP_USERNAME` and `GP_PASSWORD` from process environment (populated by systemd's `EnvironmentFile=`). Catches `InvalidLogin`, `NoScTokenFound`, `NoJwtTokenFound`, `CantReachSouthernCompany` exceptions and retries up to `max_login_attempts` (from `config.yaml`, default 2). Raises `AuthFailedBounded` on exhaustion. Returns a tuple `(session, api, account)` where `api` is the live `SouthernCompanyAPI` instance on success.

**Files**: `src/gp_monitor/auth.py`

**Test**: Write a pytest test that mocks `southern_company_api.SouthernCompanyAPI` and verifies retry behavior: (a) first attempt fails with `InvalidLogin`, second attempt succeeds, function returns session/api/account; (b) both attempts fail, function raises `AuthFailedBounded`; (c) non-retryable exception (`ValueError`) is not caught. Test with real credentials requires Preston to provide `.env` at deploy time (not required for this step's local verification).

**Depends on**: Step 3.

**Parallelizable**: Yes (independent of collect.py, publish.py, etc.).

### Step 5: Implement collect.py
**What**: Create `src/gp_monitor/collect.py` — a wrapper around `account.get_month_data(jwt)` that retrieves `MonthlyUsage` and extracts `(total_kwh_used, dollars_to_date, period_start, period_end)`. Before calling `get_month_data(jwt)`, check if the JWT is expired and re-fetch via `await api.jwt` (the property auto-refreshes on expiry when awaited), bounded by the same `max_login_attempts` counter. Maps the raw library output to the HA publish shape. Raises `CollectFailed` on error.

**Files**: `src/gp_monitor/collect.py`

**Test**: Write a pytest test that mocks `account.get_month_data` and verifies the return tuple contains the correct keys and types. Test JWT expiry re-fetch by mocking `api.jwt` as an awaitable property returning expired token on first call, fresh on second; verify the second call to `get_month_data` succeeds with the fresh token. Test `CollectFailed` is raised on `UsageDataFailure` / `CantReachSouthernCompany`.

**Depends on**: Step 3.

**Parallelizable**: Yes (independent of auth.py, publish.py, etc.).

### Step 6: Implement publish.py
**What**: Create `src/gp_monitor/publish.py` — an `httpx`-based client that POSTs usage and billing data to Home Assistant's `/api/states/<entity_id>` endpoint. Publishes three entities: `sensor.georgia_power_usage_kwh` (state = `total_kwh_used`, unit `"kWh"`, device_class `"energy"`), `sensor.georgia_power_bill_to_date` (state = `dollars_to_date`, unit `"USD"`, device_class `"monetary"`), `sensor.georgia_power_last_poll` (state = ISO timestamp of *successful* cycles only). Reads `HA_BASE_URL` from config (e.g., `"http://10.0.0.5:8123"`) and `HA_LONG_LIVED_TOKEN` from env. Each POST includes period_start/period_end/friendly_name attributes. Raises `PublishFailed` on non-2xx response or timeout. Never publishes partial data — if any entity POST fails, the cycle is marked `partial_failure` and `sensor.georgia_power_last_poll` is not advanced (so staleness is visible in HA).

**Files**: `src/gp_monitor/publish.py`

**Test**: Write pytest tests using `httpx` mocking (`pytest-httpx` or manual mock): (a) successful POST to all three entities returns 2xx, function returns True; (b) one entity POST returns 4xx, function raises `PublishFailed` (simulating partial failure); (c) verify that `last_poll` timestamp is ISO-formatted; (d) with `--dry-run` flag, the publish step is skipped entirely (verify in cli.py integration test).

**Depends on**: Step 3.

**Parallelizable**: Yes (independent of auth.py, collect.py, etc.).

### Step 7: Implement breaker.py
**What**: Create `src/gp_monitor/breaker.py` — a JSON circuit breaker that tracks `consecutive_failures`, `last_failure_at` (ISO8601), and `last_failure_reason` (closed set: `"cease_halted"`, `"invalid_login"`, `"no_jwt"`, `"no_token"`, `"cant_reach_gp"`, `"collect_failed"`, `"publish_failed"`, `"publish_partial"`, `"rate_limited"`). File lives at `data/breaker_state.json` (gitignored, mode 0600). After `max_consecutive_failures` (config, default 5) failures, the breaker is "tripped" and the next cycle aborts before attempting login (check breaker state first, raise `BreakerTripped` if tripped). On success, reset counters to 0. On failure, increment counter and record reason. Never log or persist credentials/tokens in this file.

**Files**: `src/gp_monitor/breaker.py`

**Test**: Write pytest tests: (a) trip and check breaker state (4 failures in a row, 5th attempt raises `BreakerTripped`); (b) reset after success (failure counter goes to 1, success resets to 0); (c) file is created with correct perms (mode 0o600); (d) verify no credentials appear in the dumped JSON.

**Depends on**: Step 3.

**Parallelizable**: Yes (independent of other modules).

### Step 8: Implement metrics.py
**What**: Create `src/gp_monitor/metrics.py` — a node-exporter textfile writer that outputs Prometheus metrics to `/var/lib/node_exporter/textfile_collectors/gp_monitor.prom` (on the selected deploy host, owned by the service user). Metrics: `gp_monitor_last_run_timestamp_seconds` (only updated on *successful* cycles; never touched on failure — staleness is visible in Prometheus), `gp_monitor_last_run_success` (1 or 0), `gp_monitor_work_quantity` (total kwh), `gp_monitor_work_available` (billing dollars), each with label `phase="poll"`. Copied verbatim from `algo-macro-monitor/src/macro_monitor/metrics.py` with minor adaptations.

**Files**: `src/gp_monitor/metrics.py`

**Test**: Run a test cycle and inspect `/var/lib/node_exporter/textfile_collectors/gp_monitor.prom` (or mock the path in pytest) to verify all four metrics are written in valid Prometheus format (HELP, TYPE, data lines). Verify file has mode 0o644 and is readable by Prometheus scraper.

**Depends on**: Step 3.

**Parallelizable**: Yes (independent of other modules).

### Step 9: Create systemd/gp-monitor-poll.service and .timer
**What**: Create two systemd unit files: `systemd/gp-monitor-poll.service` (Type=oneshot, ExecStart=/home/gp-monitor/venv/bin/gp-monitor poll --config /home/gp-monitor/app/config.yaml, EnvironmentFile=/home/gp-monitor/app/.env, security hardening block identical to `algo-macro-monitor`, WorkingDirectory=/home/gp-monitor/app) and `systemd/gp-monitor-poll.timer` (OnCalendar=daily, Persistent=true, StartLimitIntervalSec=86400, StartLimitBurst=1). When enabled, timer runs the service once per 24 hours.

**Files**: `systemd/gp-monitor-poll.service`, `systemd/gp-monitor-poll.timer`

**Test**: Copy both files to `/etc/systemd/system/` on the deploy host, run `sudo systemctl daemon-reload`, `sudo systemctl status gp-monitor-poll.timer`, verify state is "enabled" and "active" (or "inactive" if it's before the next scheduled run). Verify `systemctl cat gp-monitor-poll.timer` shows the correct `OnCalendar=daily` setting. No actual execution needed at this step (step 15-16 will verify it runs).

**Depends on**: Step 2 (pyproject.toml must exist to define the CLI).

**Parallelizable**: Yes (standalone config files, don't depend on other code being implemented).

### Step 10: Create ops/preflight.py
**What**: Create a standalone Python script `ops/preflight.py` that performs deployment-time validation without touching Georgia Power: (a) GET `http://<HA_BASE_URL>/api/` and verify 2xx response (HA is reachable); (b) GET with Bearer token to `/api/states` and verify 2xx (token is valid). Log each check result. Exit 0 if all pass, 1 if any fail. Designed to be run manually after deploy (step 14) and auto-invoked by `scripts/deploy.sh`'s final smoke step.

**Files**: `ops/preflight.py`

**Test**: Run `python ops/preflight.py --config config.example.yaml --env-file .env.example` (with real HA URL and token from Preston); should report each check. Run with broken HA URL/token and verify it exits 1 with clear error messages. Verify it does NOT attempt to log into Georgia Power or call `get_month_data`.

**Depends on**: Steps 3, 6 (imports config, log, and httpx patterns).

**Parallelizable**: Yes (standalone script, independent of other modules).

### Step 11a: Implement cli.py — orchestration skeleton
**What**: Create the happy-path pipeline scaffold in `src/gp_monitor/cli.py` — a Click CLI with two commands: `gp-monitor poll [--config PATH] [--dry-run]` (the main entry point) and `gp-monitor --version`. The `poll` command wires the core pipeline: (1) load config, (2) call `auth.login()`, (3) call `collect.get_usage_and_billing()`, (4) call `publish.push_to_ha()` (skip if `--dry-run`), (5) call `metrics.write()` (skip if `--dry-run`), (6) log each boundary with `fleet_logging`, (7) exit 0 on success, 1 on any failure. Guard checks (breaker, cease, rate-limiter) are stubbed out as no-ops for this step; they are added in Step 11b.

**Files**: `src/gp_monitor/cli.py` (core orchestration only)

**Test**: Write a pytest test that verifies the happy-path wiring: (a) successful full run with all upstream steps mocked, exit code 0; (b) if any upstream call raises an exception, pipeline catches it and exits 1. Verify no calls to breaker-check, cease-check, or rate-limiter at this step.

**Depends on**: Steps 3, 4, 5, 6, 8 (config/log + auth + collect + publish + metrics).

**Parallelizable**: No (depends on core modules existing).

### Step 11b: Implement cli.py — guard integration
**What**: Extend `src/gp_monitor/cli.py` to add guard checks into the pipeline: (1) check `breaker.is_tripped()` before login; (2) check `cease.is_halted("georgia_power")` before login; (3) call `rate.RateGovernor(...).acquire()` once before the Georgia Power call (not before individual internal library calls — the rate-limiter can only gate once per cycle now that egress isolation is removed); (4) on failure from any step, record the failure reason in the breaker state and exit 1. Map exception types to reason codes: `cease_halted`, `invalid_login`, `no_jwt`, `no_token`, `cant_reach_gp`, `collect_failed`, `publish_failed`, `publish_partial`, `rate_limited`.

**Files**: `src/gp_monitor/cli.py` (extended with guard logic)

**Test**: Write pytest tests for each guard failure scenario: (a) breaker is tripped, cycle aborts before login (exit 1, logged as `breaker_tripped`); (b) cease halted, cycle aborts (exit 1, logged as `cease_halted`); (c) rate governor throttles, cycle aborts (exit 1, logged as `rate_limited`); (d) rate governor permits, pipeline continues (verify `RateGovernor.acquire()` is invoked once before the Georgia Power call); (e) auth fails after retries, cycle aborts (exit 1, logged as `auth_failed_bounded`); (f) collect fails, cycle aborts (exit 1, logged as `collect_failed`); (g) publish fails, cycle aborts (exit 1, logged as `publish_failed`, `last_poll` not advanced); (h) on success, breaker counter is reset to 0. All mocked.

**Depends on**: Step 11a, Step 7 (breaker).

**Parallelizable**: No (depends on 11a being complete).

### Step 11c: Implement cli.py — `--dry-run` mode
**What**: Add `--dry-run` flag handling to `src/gp_monitor/cli.py`: when set, skip the `publish.push_to_ha()` and `metrics.write()` steps (but still perform a REAL Georgia Power login and data fetch with `auth.login()` and `collect.get_usage_and_billing()`). Note that since egress isolation is removed, `--dry-run` can only skip downstream work; it cannot skip the network-observable side effect of authenticating to Georgia Power.

**Files**: `src/gp_monitor/cli.py` (flag handling)

**Test**: Write a pytest test that runs the pipeline with `--dry-run`: (a) verify no POST request is made to Home Assistant (by mocking httpx); (b) verify no metrics file is written (by mocking metrics.write()); (c) verify auth and collect are still called (by verifying they return non-None values); (d) exit code is 0 if all upstream steps succeed; (e) the log output explicitly marks the publish and metrics steps as `skipped`, not `succeeded`.

**Depends on**: Step 11b.

**Parallelizable**: No (depends on 11b being complete).

### Step 12: Create scripts/deploy.sh
**What**: Create `scripts/deploy.sh` — a deployment script that mirrors `algo-macro-monitor/scripts/deploy.sh`. It (1) verifies the service user `gp-monitor` exists (or creates it: `sudo useradd -r -s /sbin/nologin gp-monitor`), (2) creates `/home/gp-monitor/app` and `/home/gp-monitor/venv` directories with correct ownership, (3) rsync's `src/`, `pyproject.toml`, `ops/` from the local repo to the host (or uses local paths if running on the same host), (4) creates a Python venv and runs `pip install -e .`, (5) copies systemd unit files to `/etc/systemd/system/` (requires `sudo`), (6) runs `sudo systemctl daemon-reload`, (7) runs `ops/preflight.py` as a smoke test (requires Preston-provided `.env` at `/home/gp-monitor/app/.env` with real credentials), (8) enables and starts the timer via `sudo systemctl enable gp-monitor-poll.timer`.

**Files**: `scripts/deploy.sh`

**Test**: Run the script on the selected deploy host (desktop or xps-agent) with a mock/example `.env` file (can use empty values for smoke test; preflight will skip HA token validation if token is missing, or Preston provides real token). Verify: (a) service user exists, (b) venv is created and pip install succeeds, (c) systemd units are copied and daemon-reload succeeds, (d) preflight output shows at least the connectivity checks (HA URL check), (e) timer is enabled (`systemctl is-enabled gp-monitor-poll.timer` returns `enabled`).

**Depends on**: Steps 9, 11c.

**Parallelizable**: No (depends on systemd units and cli.py).

### Step 13: Create config.example.yaml and .env.example
**What**: Create two documentation files: (1) `config.example.yaml` documenting every config key: `ha_base_url`, `poll_interval_seconds` (default 86400, one day, justified by GA Power's 24-48h publication lag per plan), `max_login_attempts`, `max_consecutive_failures`, entity ID/friendly name overrides; (2) `.env.example` documenting every secret: `GP_USERNAME=`, `GP_PASSWORD=`, `HA_LONG_LIVED_TOKEN=`. No real credentials in either file.

**Files**: `config.example.yaml`, `.env.example`

**Test**: Verify `.env.example` contains no live credentials by running `git log -p -- .env.example | grep -i "password\|token" | grep -v "^-"` (should find only the example strings, never actual values). Same for `config.example.yaml`. Run `git grep "GP_USERNAME.*=" | grep -v example.env` should return nothing (no real username committed).

**Depends on**: Steps 11c, 12 (documentation should reflect what's actually implemented).

**Parallelizable**: Yes (both are standalone doc files).

### Step 14: Deploy to selected host
**HUMAN-GATED STEP**: This step requires Preston to personally supply real Georgia Power credentials and/or a Home Assistant long-lived access token. An autonomous build pipeline can prepare everything up to this point (code complete, locally verified with mocks) but cannot complete this step on its own — it must be reported as a pending action for Preston, not silently marked done.

**What**: Run `scripts/deploy.sh` on the selected managed host (desktop or xps-agent, decided in Prerequisites). Provide a real `.env` file (or allow Preston to provide one at `/home/gp-monitor/app/.env` after the script completes). If provided, preflight.py will validate HA token is valid and reachable. If not provided at deploy time, the first scheduled poll will fail with auth error (expected; Preston supplies secrets at deploy time, not before).

**Files**: (no new files; run existing deploy.sh)

**Test**: After script completes, verify: (a) `sudo -u gp-monitor test -d /home/gp-monitor/app` succeeds, (b) `cat /home/gp-monitor/app/.env` shows the service user can read the env file (or is empty if Preston hasn't provided it yet), (c) `systemctl cat gp-monitor-poll.timer` shows the correct unit file, (d) `sudo systemctl is-enabled gp-monitor-poll.timer` returns `enabled`, (e) preflight.py smoke tests pass (if `.env` is provided with real HA token). **PASS only when step completes without errors; if `.env` isn't provided yet, this step is NOT complete — report as blocked-on-Preston, don't mark done.**

**Depends on**: Steps 12, 13 (deploy.sh and config examples must exist).

**Parallelizable**: No (deployment step).

### Step 15: Verify systemd timer runs successfully
**HUMAN-GATED STEP**: This step requires Preston to personally supply real Georgia Power credentials and/or a Home Assistant long-lived access token (if not already provided at Step 14). An autonomous build pipeline can prepare everything up to this point but cannot complete a successful verification run on its own.

**What**: After deployment, verify the timer executes at least once. Either (a) wait for the next daily scheduled run (check `systemctl status gp-monitor-poll.timer` to see when), or (b) manually trigger a test run with `sudo systemctl start gp-monitor-poll.service` (one-off, not scheduled). Inspect the output: `sudo systemctl status gp-monitor-poll.service` should show `Active: inactive (dead)` with exit code 0 on success, non-zero on failure. Check logs: `sudo journalctl -u gp-monitor-poll.service -n 50` should show fleet-logging JSON lines for each phase (cease check, auth attempt, collect, publish, metrics, outcome). If `.env` was not provided at step 14, the first run will fail with `auth_failed_bounded` (expected); provide the `.env` and re-run step 15 for a successful cycle.

**Files**: (no new files; verify existing systemd units)

**Test**: Run the service manually: `sudo systemctl start gp-monitor-poll.service`, then `sudo systemctl status gp-monitor-poll.service` should show exit code 0. Inspect `sudo journalctl -u gp-monitor-poll.service -1 -o json-pretty` and verify the JSON log contains a final `outcome="success"` line. **PASS only when `outcome=success` appears in the log; if `.env` isn't provided yet, this step is NOT complete — report as blocked-on-Preston, don't mark done.**

**Depends on**: Step 14 (deployment must be complete).

**Parallelizable**: No (verification step).

### Step 16: Run integration test with real credentials
**HUMAN-GATED STEP**: This step requires Preston to personally supply real Georgia Power credentials and/or a Home Assistant long-lived access token. An autonomous build pipeline can prepare everything up to this point but cannot complete a full integration test on its own.

**What**: Provide Preston-supplied Georgia Power credentials in `/home/gp-monitor/app/.env` (if not already provided at step 14). Run `sudo systemctl start gp-monitor-poll.service` to execute a full cycle with real authentication and data retrieval. Verify that both `sensor.georgia_power_usage_kwh` and `sensor.georgia_power_bill_to_date` entities appear in Home Assistant at 10.0.0.5:8123 with current values. Check the HA Developer Tools > States view to confirm entity IDs and attribute values are as documented in the plan. Verify `sensor.georgia_power_last_poll` is set to the ISO timestamp of this run.

**Files**: (no new files; run existing deployment with real credentials)

**Test**: After running the service with real credentials: (a) `curl -H "Authorization: Bearer <HA_LONG_LIVED_TOKEN>" http://10.0.0.5:8123/api/states/sensor.georgia_power_usage_kwh | jq .state` should return a number (e.g., `"412.7"`); (b) same for `sensor.georgia_power_bill_to_date` (should return a dollar amount); (c) `sensor.georgia_power_last_poll` should be an ISO timestamp younger than 5 minutes; (d) systemd service status should show exit code 0 and `"outcome=success"` in journalctl logs. **PASS only when all four checks pass and `outcome=success` is confirmed in journalctl.**

**Depends on**: Step 15 (timer verification must pass before full integration).

**Parallelizable**: No (final validation step).

## Rollback plan

**Step 1 (cross-repo change to scraper-commons)**: A single commit. Revert via `git revert -n <commit_hash> && git commit && git push origin main` in `scraper-commons`. No lasting deployment state to clean up.

**Steps 2-13 (in-repo code and config)**: All reversible via `git revert` or `git reset --hard origin/main` since no secrets are committed. If a branch was created, delete the branch; if commits were made to main (not recommended), revert them.

**Prerequisite #1 (MFA)**: If MFA was disabled on Preston's Georgia Power account for this integration, re-enabling it is Preston's own action outside this repo's control — document the standing risk if he chooses not to re-enable it.

**Steps 14-16 (deployment to managed host)**: To fully rollback:
1. Disable the timer: `sudo systemctl disable gp-monitor-poll.timer`
2. Stop the service: `sudo systemctl stop gp-monitor-poll.service`
3. Remove systemd units: `sudo rm /etc/systemd/system/gp-monitor-poll.{service,timer}`
4. Reload systemd: `sudo systemctl daemon-reload`
5. Delete the service user's app directory and venv: `sudo rm -rf /home/gp-monitor`
6. (Optionally) remove the service user: `sudo userdel gp-monitor`
7. Remove the textfile metrics: `sudo rm /var/lib/node_exporter/textfile_collectors/gp_monitor.prom`

All in-repo changes (steps 2-13) are already rolled back by reverting the git commits (step above).

No Home Assistant entities need manual removal; they persist even if the service is disabled, but `sensor.georgia_power_last_poll` will not advance and will appear stale (which is the desired "safe degradation" per FR-12).

