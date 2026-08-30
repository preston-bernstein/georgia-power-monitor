# Requirements: Georgia Power Monitor

## Problem statement
Preston has no way to see his Georgia Power electricity usage and billing data alongside the rest of his home data. Georgia Power / Southern Company publishes no public API, so the only viable data path is an unofficial login to the residential customer portal (the same reverse-engineered approach as the community "southern-company-api" project) followed by scraping usage/billing data out of the portal. That data needs to land on his standalone Home Assistant instance (<ha-host>:8123) as sensors/entities so it's visible on a dashboard next to his other home telemetry, without requiring him to check a separate utility website. This matters now because Preston is standardizing his home-data footprint (fleet-logging, scraper-commons, algo-macro-monitor) and utility usage is a gap in that picture.

## Users / stakeholders
- Preston Bernstein — sole account holder, sole consumer of the Home Assistant dashboard, owner of the Georgia Power credentials being used.
- Home Assistant instance at <ha-host>:8123 — the downstream system that receives and renders the published data; not managed/SSH-able by Preston's fleet.
- The managed deploy host (<deploy-host-a> or <deploy-host-b>, selected per current load distribution) — runs the scheduled poll job.
- Georgia Power / Southern Company's residential customer portal — the unofficial, unauthorized-integration upstream data source; its ToS and anti-automation posture constrain polling behavior (ban risk).

## Functional requirements
1. The system shall authenticate to the Georgia Power residential customer portal using account credentials read from a service-user `.env` file, following the same login/session flow as the "southern-company-api" reference approach.
2. The system shall use scraper-commons' `cease` module (halt-on-notice registry) for legal/ethical scraping discipline, extended with a new `georgia_power` platform id.
3. The system shall retrieve electricity usage data (at minimum: usage amount and the period/date it covers) from the authenticated portal session.
4. The system shall retrieve a month-to-date cost-accrual figure from usage data (the upstream data source has no true account-balance/amount-due field) — this is a usage-cost estimate, not Preston's literal bill amount, and must be labeled as such in the published Home Assistant entity's friendly name/attributes.
5. The system shall run as a scheduled poll on a fixed interval, not continuously and not on-demand-only, with the interval configured in `config.yaml`.
6. The system shall determine the actual upstream data-refresh cadence (how often Georgia Power's portal updates usage/billing figures) before finalizing the poll interval, and shall not poll more frequently than that cadence justifies.
7. The system shall publish retrieved usage and billing data to Home Assistant at <ha-host>:8123 over its REST API using a long-lived access token (no MQTT broker exists in home-infra as of this build, so REST is the integration path, not a placeholder pending a future decision).
8. The system shall expose each published data point as a distinct Home Assistant entity (e.g., a usage sensor and a billing/balance sensor), each independently readable on the HA dashboard.
9. The system shall re-authenticate to the Georgia Power portal automatically when a session expires or a poll cycle's login attempt fails, without manual intervention, up to a bounded retry count (max_login_attempts, configurable, default 2).
10. The system shall log every poll cycle's outcome (success, auth failure, scrape failure, publish failure) using the fleet-logging shared JSON logging contract and config loader convention.
11. The system shall not log or persist Georgia Power account credentials or the Home Assistant access token in plaintext anywhere other than the service-user `.env` file.
12. The system shall fail a poll cycle safely (log the failure, skip publishing partial/stale data as if it were fresh, and retry on the next scheduled interval) when the portal login or scrape fails.
13. The system shall run under a dedicated systemd unit (in `systemd/`) on the selected managed host, started and supervised the same way as algo-macro-monitor's deploy unit.
14. The system shall provide a `config.example.yaml` and `.env.example` documenting every required configuration key and secret, with no real credentials committed.

## Non-functional requirements
- Poll interval must not exceed the actual upstream data-refresh rate; interval value is [threshold TBD] pending verification of how often Georgia Power's portal data updates (likely daily or a few-times-daily, not real-time).
- No secrets (Georgia Power credentials, HA long-lived access token) committed to git; both live only in a service-user `.env` file, matching the existing secret-handling convention used by sibling repos.
- The scraper must degrade gracefully under portal anti-automation defenses (CAPTCHA, rate limiting, layout change) — a failed cycle must not crash the systemd service or corrupt already-published HA state.
- Repo follows the algo-macro-monitor structural convention: `src/`, `systemd/`, `ops/`, `config.example.yaml`, `.env.example`, Python packaged via `pyproject.toml`/setuptools — not the fashion-monitor TypeScript monorepo pattern.
- Deploy host selection must account for current load: <deploy-host-b> already runs other unrelated home-lab services, so host choice is based on which managed host (<deploy-host-a> or <deploy-host-b>) has verified LAN reachability to <ha-host> and spare capacity, decided during the plan phase.
- Poll-cycle outcomes are exported as node-exporter textfile metrics, matching algo-macro-monitor's observability convention, so failures are visible in the existing Prometheus/Grafana stack, not just in logs.

## Constraints
- No official Georgia Power / Southern Company API exists; Southern Company's real API is partner-only. This feature is inherently built on an unofficial, reverse-engineered portal-login integration and inherits that source's fragility (breaks on portal changes, risk of account lockout/ban from excessive polling).
- Home Assistant at <ha-host>:8123 is not one of Preston's managed/SSH-able hosts (unlike the other managed hosts in his fleet) — all integration must happen over the LAN network via the HA REST API, never via local install or file-level access to the HA host.
- Must use scraper-commons' `cease` module for legal/ethical scraping discipline rather than reimplementing halt-registry logic from scratch.
- Must use fleet-logging's shared JSON logging contract and config loader convention.
- Must follow algo-macro-monitor's structural template (src/, systemd/, ops/, config.example.yaml, .env.example), not fashion-monitor's TypeScript monorepo pattern.
- Must store Georgia Power account credentials and the Home Assistant token the same way other repos store secrets: service-user `.env`, never committed.
- Deploy target is one of the existing managed hosts (<deploy-host-a> or <deploy-host-b>) — no new host is provisioned for this feature.
- The system shall NOT route Georgia Power portal traffic through the shared VPN egress-isolation gateway used by other scrapers — polls run from the deploy host's normal network path, because IP consistency is itself a trust signal for a real personal utility account, and isolating egress here would inherit unrelated scrapers' IP-reputation risk.
- MFA/CAPTCHA on the Georgia Power account is not supported by the underlying library; if Preston's account has MFA enabled, this integration will fail silently and permanently until he disables it or accepts standing manual-intervention risk — this must be verified with Preston before the live-credential steps, not assumed.
- This repo depends on one small prerequisite change in the sibling `scraper-commons` repo (registering a new platform id in its halt-on-notice registry) — a one-line, low-risk addition, but it is a dependency this repo's build order must respect.

## Out of scope
- Building or exposing any UI beyond what Home Assistant itself renders from the published entities — this repo does not ship its own dashboard or web UI.
- Historical backfill of usage/billing data prior to when the monitor is first deployed.
- Any official/partner Southern Company API integration (none is available to Preston).
- Bill payment, account management, or any write action against the Georgia Power portal — read-only data retrieval only.
- Local installation onto the Home Assistant host itself (<ha-host> is not managed/SSH-able).
- Real-time or sub-daily usage streaming — the feature is a scheduled poll, not a live feed.
- Alerting/notification logic beyond what Home Assistant's own automations can build on top of the published entities.
- Multi-account or multi-utility support — this is scoped to Preston's single Georgia Power residential account.

## Acceptance criteria
1. Given valid Georgia Power credentials in the service-user `.env`, a scheduled poll cycle successfully logs into the portal and retrieves both a usage figure and a billing figure without manual intervention.
2. Given a successful poll cycle, the corresponding usage and billing values appear as distinct entities in Home Assistant at <ha-host>:8123 within one poll interval of the scrape completing.
3. Given an expired or invalid session mid-cycle, the system re-authenticates automatically and either completes the cycle or logs a bounded-retry failure — it does not crash the systemd service.
4. Given a portal login or scrape failure, the poll cycle logs the failure via the fleet-logging contract and does not publish stale data to Home Assistant as if it were fresh.
5. Inspecting the repo's `.env.example` and `config.example.yaml` shows every required secret and config key documented, with no real credential values present anywhere in the git history.
6. Running `git log` / `git grep` over the repo confirms no Georgia Power password, account number, or Home Assistant long-lived access token is committed in plaintext.
7. The systemd unit in `systemd/` starts the monitor on the selected managed host, and the service remains running (not crash-looping) across at least one full poll interval.
8. The configured poll interval is documented alongside a stated justification tied to Georgia Power's actual data-refresh cadence, not an arbitrarily chosen value.
9. The repo's directory layout matches algo-macro-monitor's structure (`src/`, `systemd/`, `ops/`, `config.example.yaml`, `.env.example`, `pyproject.toml`) and does not use the fashion-monitor TypeScript monorepo layout.
10. The `cease.is_halted('georgia_power')` check is visibly present in the poll cycle's code path (import/call present) rather than reimplementing halt-registry logic from scratch.
11. Acceptance criteria 1 and 2 (real login + real HA publish) require Preston to personally supply real Georgia Power credentials and a Home Assistant long-lived access token, and to confirm his account does not have MFA enabled (or accept the standing risk if it does) — these are human-gated steps an autonomous build pipeline cannot complete on its own, and must be reported as a pending human action, not silently marked done.
