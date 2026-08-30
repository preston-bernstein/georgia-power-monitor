# Spec Challenge Notes

## Agents run
- Requirements Auditor (haiku): 6 issues found, 6 accepted
- Scope & Dependency Auditor (sonnet): 9 issues found, 7 accepted
- Design Devil's Advocate (sonnet): 8 issues found, 6 accepted
- Implementation Realist (sonnet): 12 issues found, 8 accepted
- Steps & Sequencing Critic (sonnet): 20 issues found, 15 accepted
- Data Model Critic (sonnet): 12 issues found, 10 accepted
- Security/Threat Auditor (haiku): 13 issues found, 4 accepted (rest deferred as beyond this build's scope, noted below)

## Changes made
- **Dropped VPN/egress isolation from the integration entirely.** Two independent agents found that routing a real personal utility-account login through the shared VPN-egress gateway likely *increases* lockout risk (a consistent residential IP is a trust signal for this kind of account, not a liability) and risks breaking other unrelated services' existing container attachments if the shared gateway gets recreated. The poll job now runs directly from the deploy host's normal network path. This also removed the home-infra cross-repo prerequisite entirely.
- **Fixed a real interface bug before it shipped**: `auth.login()` was specified to return a bare JWT string, but the upstream library's `.jwt` is an async property that must be re-awaited to get its own auto-refresh — collect.py had no way to do that with just a string. Now `auth.login()` returns the live `SouthernCompanyAPI` instance so collect.py can re-await `.jwt` itself.
- **Fixed a wrong file path**: the scraper-commons cross-repo change was targeting `cease.py`, which doesn't exist — `cease` is a package, the real file is `cease/registry.py`.
- **Resolved the FR-2/AC-10 contradiction**: requirements said "use scraper-commons' login/session/stealth machinery," but the real upstream is a plain JWT-REST API, not a browser-gated page — scraper-commons' browser modules don't apply. Rewrote both to reference the `cease` halt-registry module, which is what's actually used.
- **Added explicit human-gate markers** to the three steps that need Preston's real Georgia Power credentials and Home Assistant token — an autonomous build pipeline can get everything else code-complete and mock-verified, but these three steps must be reported as pending, never silently marked done. Also flagged MFA verification as Preston's own account-security decision, not an automatable step.
- **Fixed data-integrity gaps**: added rounding for the monetary figure (avoids float artifacts like `137.09999999999998`), a monotonicity sanity check (HA's `total_increasing` state class would otherwise silently absorb a data-parsing bug as a "meter reset"), atomic writes + file locking + a schema version on the circuit-breaker JSON file, and input validation on config values and `ha_base_url`.
- **Split the oversized cli.py step** (10 orchestrated behaviors, 8 test scenarios in one step) into three independently verifiable sub-steps: orchestration skeleton, guard integration, and dry-run mode.
- Added a missing `.gitignore` step — acceptance criteria depended on `.env` and the breaker-state file never being committed, but no step created one.

## Critiques rejected
- Suggestion to add a formal `schema_version` migration framework for the breaker file beyond a single version field — over-engineered for one JSON file with three fields.
- Suggestion to switch credential storage to `systemd-creds`/`LoadCredential=` — noted as a considered-but-deferred alternative in plan.md's Risk areas rather than adopted now, since it would diverge from the fleet's existing `.env` convention; worth revisiting if this pattern gets reused for other high-value personal accounts.
- Most of the Security Auditor's findings (TLS for the LAN HTTP call, supply-chain pinning/hashing for the upstream PyPI package, timing-attack resistance on token validation) were judged disproportionate to a single-user LAN-only home service and deferred rather than built into this pass — noted as accepted risk, matching the existing fleet convention of trusting the LAN perimeter.
- Suggestion to promote the JSON circuit-breaker pattern into a shared `scraper-commons` module (since this is now the third repo hand-rolling the same shape) — a real observation, but out of scope for this repo's own build; left as a note for a future scraper-commons extraction, not actioned here.

## Open questions requiring human input
- **MFA status on Preston's real Georgia Power account is unknown and must be checked before the live-credential steps** — the upstream library has no MFA/CAPTCHA support at all; if MFA is on, this integration goes silently and permanently inert until Preston disables it or accepts standing manual-intervention risk.
- **Deploy host (<deploy-host-a> vs. <deploy-host-b>) is not yet decided** — now a pure load/capacity call since the egress-colocation constraint was removed; needs Preston's or the build pipeline's decision at deploy time based on current load.
- **Real credentials (Georgia Power username/password, Home Assistant long-lived access token) must come from Preston** — nothing downstream of spec can fabricate these; the final three build steps stay blocked on him regardless of how clean the rest of the implementation is.
