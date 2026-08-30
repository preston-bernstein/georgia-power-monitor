# Plan: Georgia Power Monitor

## Approach

A daily systemd-timer oneshot CLI (`gp-monitor poll`), structurally identical to `internal-monitor-service`
(`src/`, `systemd/`, `ops/`, `pyproject.toml`/setuptools, `config.example.yaml`, `.env.example`),
authenticates to Georgia Power's actual reverse-engineered JWT REST API — not a rendered web page —
via the real, maintained `southern-company-api` PyPI package (the literal upstream FR-1 references
by name; confirmed by pulling and reading its wheel directly). That library performs a plain
`aiohttp` login/token-exchange flow (verification token → `ScWebToken` → `SouthernJwtCookie` → JWT),
never a browser, so `scraper-commons`' browser-oriented modules (`stealth`, `fetch`, `login`) do not
apply here; this plan instead wraps the library with `scraper-commons`' **non-browser** primitives —
`rate.RateGovernor` (a coarse, once-per-poll-cycle throttle gated immediately before the login call,
not a pacer over every outbound HTTP call — see Architecture and Risk areas for why) and `cease`
(halt-on-notice registry, extended with a new platform id) — which satisfies FR-2's intent
(scraper-commons owns session/rate/legal discipline) without forcing an architecture mismatch onto a
target that isn't actually browser-gated. This integration deliberately does **not** route through
the shared VPN egress-isolation gateway (`scraper_commons.egress.assert_isolated_egress`) — see Risk
areas for the reasoning; the poll job authenticates and polls directly over the deploy host's normal
network path, the same as every other non-scraping process on that host. Retrieved usage/billing
figures are pushed to Home Assistant over its REST API with a long-lived token (no MQTT broker exists
in `internal-infra` today, so REST is the only viable path, not a stopgap).

## Architecture

```
systemd timer (daily, OnCalendar)
        │
        ▼
gp-monitor poll  (oneshot CLI, src/gp_monitor/cli.py)
        │
        ├─ 1. cease.is_halted("georgia_power")                         ── scraper-commons
        │     abort cycle immediately if a human has recorded a halt
        │
        ├─ 2. rate.RateGovernor(per_host_rps=0.05).acquire(host)       ── scraper-commons
        │     one coarse gate per poll cycle, immediately before step 3 — cannot pace the
        │     individual internal aiohttp calls SouthernCompanyAPI.connect() makes (no hook
        │     exists for external code to intervene between them); defense-in-depth against
        │     call-volume spikes, not per-request pacing
        │
        ├─ 3. auth.login(session, username, password)                  ── new local module,
        │     wraps southern_company_api.SouthernCompanyAPI(username, password, session),
        │     calls .connect(), returns (session, api, account) — api is the live
        │     SouthernCompanyAPI instance, not a bare jwt string, so collect.py can re-await
        │     the library's own auto-refreshing `api.jwt` property immediately before use
        │     bounded retry (config: max_login_attempts, default 2) on InvalidLogin/
        │     NoScTokenFound/NoJwtTokenFound/CantReachSouthernCompany — never on other exceptions
        │
        ├─ 4. collect.get_usage_and_billing(account, api)               ── new local module,
        │     `jwt = await api.jwt` first (fresh token via the library's own wall-clock
        │     auto-refresh, not a catch-and-retry after the fact), then
        │     account.get_month_data(jwt) → MonthlyUsage
        │     (total_kwh_used, dollars_to_date, period = first-of-month..today)
        │
        ├─ 5. publish.push_to_ha(usage, billing)                       ── new local module
        │     POST /api/states/<entity_id> per entity to <ha-host>:8123, Bearer token
        │     only reached if steps 1-4 all succeeded — never publishes partial/stale data
        │
        └─ 6. fleet_logging.log_event(...) at every step boundary + one closing outcome line
              + node-exporter textfile metrics (gp_monitor_last_run_*)
```

Each numbered step either completes or the whole cycle aborts with a logged failure (FR-12); there
is no partial-publish path. Re-authentication (FR-9) is "just run step 3 again next scheduled poll"
— the process is not long-lived (`Type=oneshot`), so there is no live session to expire mid-cycle;
the one exception is a JWT that expires between step 3 (auth) and step 4 (collect) within the same
run — handled naturally, since collect.py always awaits `api.jwt` fresh immediately before calling
`get_month_data`, and the library's own property auto-refreshes on wall-clock expiry, rather than
gp_monitor attempting to catch-and-retry a stale token after the fact.

## Data model

No relational database. This is a stateless poll-and-publish job — Home Assistant's own recorder is
the system of record for history, matching "no historical backfill" (out of scope) and keeping this
repo simpler than `internal-monitor-service` (which needs local SQLite because it correlates against a
separate snapshot over time; this repo does not).

One small local JSON state file, `data/breaker_state.json` (gitignored, mode 0600), tracks bounded
retry state across scheduled runs — mirrors the shape internal-monitor-app's Poshmark login circuit
breaker used for the same reason (a plain JSON file, not a database, is enough for one counter):

```
{
  "schema_version": 1,
  "consecutive_failures": <int>,
  "last_failure_at": "<ISO8601>",
  "last_failure_reason": "<short code, e.g. 'invalid_login' | 'no_jwt' | 'ha_unreachable'>"
}
```

No credential or credential-derived value (JWT, password) is ever written to this file — `reason`
is a closed set of short diagnostic codes, never a raw exception message (which could echo response
headers containing the JWT). After `max_consecutive_failures` (config, default 5) the cycle logs and
exits without attempting login at all, until a human clears the file — same one-way-trip shape as
`scraper-commons`' `admission` module, adapted locally rather than reused (see Technology choices).

`breaker.py` writes are atomic — write to a temp file in the same directory, then `os.rename` over
the real path, never an in-place write — so a process killed mid-write can't leave a corrupt/partial
JSON file. `schema_version: 1` is included from the start for future migration safety.
`last_failure_reason` is enforced via a real Python `Enum`/`Literal` at write time in `breaker.py`,
not just documented here as a closed set. A file lock (`flock` on the breaker file, or an equivalent
single-instance guard) prevents a manually-triggered `systemctl start gp-monitor-poll.service` from
racing a timer-triggered run and corrupting the breaker state.

This closed-set-code discipline applies fleet-wide for this repo: fleet-logging events, breaker
failure reasons, and preflight error messages must never include raw exception text or HTTP response
bodies — either could echo back the Georgia Power JWT/session token or the HA Bearer token — only
closed-set diagnostic codes and HTTP status codes are logged, anywhere in this repo.

## API / interface contract

**CLI** (`gp-monitor`, Click-based like `macro-monitor`):

```
gp-monitor poll [--config PATH] [--dry-run]     # the only command; --dry-run skips step 5 (HA publish)
gp-monitor --version
```

`--dry-run` skips only the Home Assistant publish step — it still performs a real Georgia Power
login and data fetch, so it is not credential-free and still carries the same lockout-risk exposure
as a normal poll cycle. (The mocked-everything version used in automated tests is a separate thing
from what `--dry-run` does against real infrastructure.)

**Home Assistant REST publish** (per entity, HA's existing, stable REST API — no new HA-side setup
beyond the long-lived access token):

```
POST http://<ha-host>:8123/api/states/sensor.georgia_power_usage_kwh
Authorization: Bearer <HA_LONG_LIVED_TOKEN>
Content-Type: application/json

{
  "state": "412.7",
  "attributes": {
    "unit_of_measurement": "kWh",
    "friendly_name": "Georgia Power Usage (Month to Date)",
    "period_start": "2026-08-01",
    "period_end": "2026-08-16",
    "device_class": "energy",
    "state_class": "total_increasing"
  }
}
```

Second entity, same shape: `sensor.georgia_power_bill_to_date` (state = `MonthlyUsage.dollars_to_date`,
`unit_of_measurement: "USD"`, `device_class: "monetary"`). A third, non-functional diagnostic entity,
`sensor.georgia_power_last_poll`, is set to the ISO timestamp of the last *successful* cycle
only (never touched on a failed cycle) so staleness is visible on the HA dashboard itself (partially
covers the "no separate alerting" out-of-scope note — visibility, not alerting).

Before publishing, `collect.py`/`publish.py` apply: `dollars_to_date` is rounded to 2 decimal places
(`round(value, 2)`) before being formatted into the HA state string, to avoid float artifacts like
`"137.09999999999998"`; both `total_kwh_used` and `dollars_to_date` are validated as positive,
plausible floats before publishing, guarding against a malformed/buggy upstream response injecting
NaN, negative, or absurd values into HA's energy/monetary sensors; and `collect.py` runs a cheap
monotonicity sanity check — `total_kwh_used` shouldn't decrease within the same month (except near
the 1st, when the billing period resets) — logging a warning (not aborting) if it does, since HA's
`total_increasing` state class will silently absorb a decrease as an intentional meter reset, which
could otherwise mask a real parsing bug in the brittle reverse-engineered upstream.

**Error cases:**
- `cease.is_halted("georgia_power")` is `True` → cycle aborts immediately; logged as `cease.halted`;
  no login attempt at all.
- Login failure within `max_login_attempts` → each attempt logged as `auth.attempt_failed` with the
  closed reason code (never credentials/tokens); after the bound, `auth.failed_bounded`, cycle ends,
  nothing published.
- `get_month_data` raises `UsageDataFailure`/`CantReachSouthernCompany` → logged as `collect.failed`;
  cycle ends without publishing (never republishes the prior cycle's cached value as if fresh).
- HA REST call returns non-2xx or times out → logged as `publish.failed`; the *other* entity, if
  already published this cycle, is not retroactively un-published — but the cycle's outcome line is
  still `outcome=partial_failure`, and `sensor.georgia_power_last_poll` is deliberately NOT
  advanced on a partial-failure cycle (so partial data is visible in HA but is provably flagged stale
  by the untouched last-poll timestamp, satisfying "does not publish stale data as if it were fresh"
  even for the entity that did succeed this cycle).

## Integration points

- `src/gp_monitor/__init__.py` — Python package initialization (empty or minimal; created alongside config.py/log.py).
- `src/gp_monitor/cli.py` — Click CLI, `poll`/`--version`, mirrors `macro_monitor/cli.py`'s shape.
- `src/gp_monitor/auth.py` — **new**: wraps `southern_company_api.SouthernCompanyAPI`, bounded retry
  on the library's own narrow exception set (`InvalidLogin`, `NoScTokenFound`, `NoJwtTokenFound`,
  `CantReachSouthernCompany`), reads `GP_USERNAME`/`GP_PASSWORD` from process env (populated by
  systemd's `EnvironmentFile=`, per FR-1/FR-11 — never read via `python-dotenv` tree-walking, same
  guard as `macro_monitor/config.py`'s `_NO_DOTENV`). `login()` returns `(session, api, account)` —
  `api` is the live `SouthernCompanyAPI` instance, not a bare jwt string, because `SouthernCompanyAPI.jwt`
  is an `async def` `@property` that must be awaited and already auto-refreshes on wall-clock expiry;
  handing `collect.py` only a plain string would give it no way to re-await that property if the JWT
  expires mid-operation.
- `src/gp_monitor/collect.py` — **new**: does `jwt = await api.jwt` immediately before calling
  `account.get_month_data(jwt)`, so it always gets a fresh token via the library's own auto-refresh
  logic rather than catching-and-retrying after the fact; maps `MonthlyUsage` to the two
  publish-ready values (FR-3/FR-4), rounding `dollars_to_date` to 2 decimals, validating both values
  as positive/plausible, and logging a warning on a same-month `total_kwh_used` decrease outside the
  1st-of-month reset window (see API / interface contract).
- `src/gp_monitor/breaker.py` — **new**: the JSON circuit-breaker file (see Data model), same
  5-consecutive-failure trip shape as internal-monitor-app's `poshmark/login-circuit-breaker.ts`.
- `src/gp_monitor/publish.py` — **new**: thin `httpx` client for HA's `/api/states/<entity_id>`,
  reads `HA_BASE_URL` (config.yaml, not secret) and `HA_LONG_LIVED_TOKEN` (`.env`, secret, FR-11).
- `src/gp_monitor/log.py`, `src/gp_monitor/config.py` — thin wrappers around `fleet_logging`, copied
  verbatim from `internal-monitor-service/src/macro_monitor/{log,config}.py`'s pattern (stderr channel,
  `env_prefix="GP_MONITOR_"`, `_NO_DOTENV` guard) — not reinvented (FR-10).
- `src/gp_monitor/metrics.py` — node-exporter textfile writer, same four-metric shape as
  `macro_monitor/metrics.py` (`gp_monitor_last_run_timestamp_seconds`, `_last_run_success`,
  `_work_quantity`, `_work_available`), one `phase="poll"` label.
- `pyproject.toml` — setuptools, `src/` layout, `southern-company-api` + `scraper-commons` (`rate`,
  `cease` — no `stealth`/`sidecar`/`browser`/`egress` extras needed) + pinned `fleet-logging`, same
  git-pin convention as `internal-monitor-service/pyproject.toml`.
- `systemd/gp-monitor-poll.service` + `.timer` — `Type=oneshot`, `EnvironmentFile=/home/gp-monitor/app/.env`
  (mode 600, root-owned, service-user-readable only), same hardening block as
  `internal-monitor-service/systemd/macro-monitor-collect.service` (`ProtectSystem=strict`, `NoNewPrivileges`,
  etc.), `OnCalendar` daily (see Risk areas / poll-interval justification below) (FR-5/FR-13). Deploy
  host (<deploy-host-a> vs. <deploy-host-b>) is a pure load/capacity decision — there is no proxy-colocation
  requirement now that egress isolation is out of scope (see Risk areas).
- `config.example.yaml` — `ha_base_url`, `poll interval config key`, `max_login_attempts`,
  `max_consecutive_failures`, entity-id/friendly-name overrides (FR-14). All three numeric config
  values (`poll_interval_seconds`, `max_login_attempts`, `max_consecutive_failures`) are validated as
  positive at load time in `config.py` (a 0 or negative value must fail fast, not crash or infinite-loop
  later). `ha_base_url` is validated against a hostname allowlist/format check at load time, so a
  misconfigured value can't silently exfiltrate the Bearer token to an unintended host; entity-id
  overrides are validated against Home Assistant's allowed entity-id character set.
- `.env.example` — `GP_USERNAME=`, `GP_PASSWORD=`, `HA_LONG_LIVED_TOKEN=`, documented, no real values
  (FR-14/FR-11).
- `scripts/deploy.sh` — mirrors `internal-monitor-service/scripts/deploy.sh`: verify service user exists,
  rsync `src`/`pyproject.toml`, venv + pip install, install systemd units, enable timer.
- `ops/preflight.py` — **new**, small standalone script (not the poll path itself): checks HA
  reachability (`GET /api/`) and long-lived token validity, *without* touching Georgia Power — run
  manually after deploy and from `scripts/deploy.sh`'s final smoke step, same spirit as
  `macro-monitor --version` being the deploy smoke check. (No egress/proxy check — egress isolation
  is out of scope for this repo; see Risk areas.)
- **`~/dev/scraper-commons` (sibling repo, cross-repo prerequisite, not part of this repo's diff)** —
  `cease.KNOWN_PLATFORMS` lives in `scraper-commons/src/scraper_commons/cease/registry.py` (`cease`
  is a package; `cease/__init__.py` re-exports `KNOWN_PLATFORMS` from `registry.py` — there is no
  top-level `cease.py`). It is currently the closed set `{"youtube", "reddit", "tiktok", "x"}`; add
  `"georgia_power"` before this repo's `cease.halt()`/`is_halted()` calls can be used at all
  (`ValueError` otherwise). Small, one-line, matches the module's own "extracted on first real need"
  convention already used for every other platform in that set.

## Technology choices

- **`southern-company-api`** (PyPI, MIT, actively maintained, `aiohttp`-based) — the actual FR-1
  reference implementation; verified by downloading and reading the wheel directly (not assumed from
  its README). It performs Georgia Power's real login/JWT flow already; reimplementing that flow by
  hand inside this repo would duplicate work `scraper-commons`' own "extract only on first need"
  philosophy argues against, and would fail the requirement to follow the reference's login/session
  shape.
- **`scraper-commons` `rate`/`cease` only, not `stealth`/`fetch`/`login`/`identity`/`egress`** — the
  target is a JSON/JWT REST API, not a rendered, JS-gated page; the browser-oriented modules solve a
  problem this integration doesn't have, and (independently) `scraper_commons.login.authenticated_login()`
  is explicitly documented as pass-cli/ProtonPass-gated and "NOT expected to run headless, from cron,
  or under a dedicated service user" — incompatible with FR-1's `.env`-sourced credentials and FR-13's
  systemd deployment regardless of target shape. This is a deliberate, evidence-backed scope decision,
  not an oversight — confirmed by reading `scraper-commons/CONTRACT.md`'s `login` module section.
  `egress` is dropped entirely (not just deferred) — see Risk areas for the reasoning.
- **systemd `EnvironmentFile=` (plain root-owned `.env`), not `systemd-creds`/`LoadCredential=`** —
  matches the rest of the fleet's existing credential-storage convention. `systemd-creds`/
  `LoadCredential=` (systemd ≥250) was considered for stronger at-rest credential protection given
  this account's real personal consequences if compromised/locked out; deferred in favor of matching
  the fleet's existing `.env` convention rather than introducing a one-off pattern — worth revisiting
  if this pattern gets reused for other high-value personal accounts.
- **HA REST API with a long-lived token, not MQTT discovery** — confirmed no MQTT broker exists
  anywhere in `internal-infra` (`docker-compose.yml`, ADR list, and a code search all came up empty).
  REST is not a stopgap here; it is what the fleet actually has today, per FR-7's own stated fallback.
- **`httpx`** for the HA REST client — already a dependency pattern in `internal-monitor-service`
  (`pyproject.toml`), no new HTTP library introduced fleet-wide.
- **`fleet_logging`** (git-pinned) + the thin `log.py`/`config.py` wrapper pattern — copied from
  `internal-monitor-service`, not reimplemented, per the explicit FR-10 constraint.
- **Click** for the CLI — matches `macro-monitor`'s existing CLI framework choice, no new pattern.
- **Plain JSON circuit-breaker file**, not `scraper-commons`' `admission` module — `admission` solves
  quarantine-to-trust promotion for *new sources*, a different shape than "stop retrying after N
  failures then require a human"; internal-monitor-app already established this exact plain-JSON pattern
  for the same login-failure-bound need (Poshmark), so this repo mirrors that, not `admission`.

## Risk areas

1. **`cease.KNOWN_PLATFORMS` needs a small cross-repo change before this repo can be fully wired (see
   Integration points).** It's a one-line prerequisite owned by `scraper-commons`, not this repo —
   build order matters, and a build that skips it silently degrades to "no cease protection," which
   must fail loudly (raise/abort), never silently no-op.
2. **Egress isolation is deliberately dropped from this integration, not deferred by oversight.**
   Adversarial review surfaced three independent problems with routing this through the shared
   VPN egress-isolation gateway (`scraper_commons.egress.assert_isolated_egress`): (a) Georgia
   Power / utility-style bot detection more plausibly treats a *consistent residential IP* as a
   trust signal than a risk factor — this is Preston's own real account, not an anonymous scrape
   target, so isolating egress is the wrong mitigation and may actively increase lockout risk, since
   routing through a shared VPN exit IP that other, more aggressive scrapers also use means this
   account inherits their IP-reputation risk; (b) the planned rate-limiting can't actually pace the
   upstream library's internal HTTP calls anyway (see Risk area 5), so egress isolation wasn't
   pairing with a real per-call pacing story in the first place; (c) reusing the shared
   the shared VPN-egress container by changing its `ports:` block would recreate the container,
   which has a documented history (a comment in the shared internal-infra compose file) of silently
   breaking existing container-network attachments — currently used by other unrelated services
   on <deploy-host-a> and <deploy-host-b> — a
   real, avoidable blast-radius risk to production services for a mitigation that's the wrong shape
   for this target anyway. The poll job authenticates directly over the deploy host's normal network
   path.
3. **`southern-company-api` has no CAPTCHA/2FA handling of any kind** — confirmed by reading its
   source directly: the only auth-failure exceptions are `InvalidLogin`, `NoScTokenFound`,
   `NoJwtTokenFound`, `CantReachSouthernCompany`, none of which distinguish "wrong password" from
   "portal now requires MFA" from "portal changed its login-page markup" (the token-scraping regexes
   over HTML/redirect headers are brittle by construction). If Georgia Power's real portal has MFA
   enabled on this account, or adds a CAPTCHA, this integration goes silently, permanently inert
   (bounded-retry failures every cycle) until a human investigates — there is no automated recovery
   path, by design (FR-12 says fail safely, not fail invisibly forever). Verify at build time whether
   Preston's account has MFA; disable it for this account if Georgia Power allows, or accept this as
   a standing manual-intervention risk.
4. **The unofficial API can change or break without notice** — it's reverse-engineered (regexes over
   HTML comments and `Set-Cookie` headers, per the library's own source), maintained by one outside
   developer, with no SLA. A Georgia Power-side change breaks this repo's login or `get_month_data`
   call with no advance warning; the bounded-retry + circuit-breaker design (FR-9/FR-12) prevents a
   crash loop but does not prevent extended silent data staleness beyond what
   `sensor.georgia_power_last_poll` surfaces on the HA dashboard.
5. **Ban/lockout risk from automated login is real and only partially mitigated.** `rate.RateGovernor`
   reduces (not eliminates) the chance Georgia Power flags this as bot traffic, but only as a coarse
   once-per-poll-cycle throttle — it cannot pace the individual HTTP calls inside
   `SouthernCompanyAPI.connect()`, which gp_monitor doesn't own and has no hook into (see Architecture
   and Risk area 2). Egress isolation is deliberately not part of the mitigation here (Risk area 2);
   the working assumption is that a consistent residential IP is itself a mitigation, not a gap. This
   is a real personal account, not a scraping target that can be walked away from if it gets banned —
   an account lockout has a real-world consequence (losing normal web/app access to the bill). Daily
   polling (see below) keeps call volume minimal, which is the primary mitigation.
6. **`MonthlyUsage.dollars_to_date` is a proxy for "billing," not a true account balance/amount-due
   field** — the reference library exposes month-to-date cost accrual from usage data, not a
   dedicated "current balance owed" endpoint (none was found in its source). FR-4 ("current balance
   and/or most recent bill amount") is satisfiable by `dollars_to_date` literally, but it will read
   differently from the number on an actual Georgia Power bill (which includes fixed charges, prior
   balance, taxes) — worth setting Preston's expectations on before this ships, not after.
7. **Poll-interval justification (AC-8), stated plainly:** the reference library's own source
   comments read `get_daily_data`: "Available 24 hours after" and `get_hourly_data`: "Available 48
   hours after" — Georgia Power's AMI meter data has a built-in 24-48h publication lag regardless of
   how often this service polls. A **daily** `OnCalendar` timer (once every 24h) is therefore the
   floor of what's useful and the ceiling of what's justified by the upstream refresh cadence — polling
   more often would retrieve the same figures repeatedly for no benefit while adding needless login
   volume against risk #5. This satisfies NFR "must not exceed the actual upstream data-refresh rate."
8. **A dead-man's-switch Alertmanager rule against `gp_monitor_last_run_timestamp_seconds` would close
   the "silent timer failure" gap cheaply using infrastructure the fleet already has** — Alertmanager +
   ntfy are already wired up fleet-wide, and this plan's own node-exporter metric could trivially back
   an alert rule (`time() - gp_monitor_last_run_timestamp_seconds > threshold`) with zero new
   infrastructure. Not required for initial ship (the `sensor.georgia_power_last_poll` HA entity
   covers passive visibility), but a natural follow-up.
