# Georgia Power Monitor

A small Python systemd service that logs into Georgia Power's residential customer
portal (via the unofficial, community-maintained `southern-company-api` client —
Southern Company/Georgia Power has no public API) once a day, retrieves the current
month's electricity usage and a usage-cost estimate, and publishes them as sensors to
a local Home Assistant instance over its REST API. Read-only; makes no writes to the
utility account.

All host/IP/port values (Home Assistant's address, deploy target) are config- or
env-driven with safe loopback defaults — see `config.example.yaml` and `.env.example`.
Copy both, drop in real values, and never commit `config.yaml` or `.env`.

## Status: not installable as-is outside the author's own GitHub account

`pyproject.toml` pins two of this repo's dependencies (`fleet-logging`,
`scraper-commons`) via `git+ssh://` to private repos. `pip install -e .` will fail
with an auth error for anyone who isn't Preston. This repo is shared as a reference
implementation, not a drop-in install, until those two libraries (or the small slice
of each this repo actually uses) are published separately.
