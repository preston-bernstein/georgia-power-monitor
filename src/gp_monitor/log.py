"""Canonical JSON log line — home-infra CONVENTIONS.md §18 (fleet logging contract).

Thin wrapper around the shared `fleet-logging` package (see the git-pinned dependency in
pyproject.toml). `fleet-logging` was built as a strict superset of `algo-macro-monitor`'s own
hand-rolled `macro_monitor/log.py` (among others) — see its README's "What it replaces" section —
so this wrapper follows that repo's pattern verbatim (`env_prefix`/`SERVICE` adapted to this repo),
copied rather than reinvented (plan.md's Integration points, FR-10): `log_event(level, event, msg,
**fields)`, and pins this package's channel to **stderr** — deliberately separate from
`click.echo`'s stdout, which stays human-facing/interactive output for `poll`/`--version`. This
module is the machine-readable channel: the thing a future Loki onboarding (host systemd unit —
see §18) would actually ship and query.

No ``logging.basicConfig``/handler configuration lives here or anywhere else in this package — §18
reserves output configuration for an application's own entry point, and this package has no
long-lived process to configure: every invocation is a single oneshot CLI command (`Type=oneshot`
systemd timer fire), so `fleet_logging.log_event(...)` printing straight to stderr *is* the entry
point's own, and only, output configuration.

Level is emitted in this package's own native spelling (``info``/``warn``/``error``) — the shared
Loki ``loki.process`` pipeline canonicalizes it; this module does not pre-canonicalize, and neither
does `fleet_logging.log_event`.

Credential safety: `fleet_logging.log_event` redacts a fleet-wide deny-list of field names
(`password`, `token`, `secret`, `authorization`, `session`, ...) before a line ever reaches
stdout/stderr (see `fleet_logging.redact`). This repo additionally never passes raw exception text
or HTTP response bodies as a field value anywhere (plan.md's Data model section) — either could
echo back the Georgia Power JWT/session token or the HA Bearer token even under an unlisted key
name, which is a gap that per-field redaction alone cannot close.
"""

from __future__ import annotations

import sys

from fleet_logging import log_event as _fleet_log_event
from fleet_logging import new_run_id as _fleet_new_run_id

SERVICE = "gp-monitor"


def new_run_id() -> str:
    """A run_id for one CLI invocation — one timer fire / one `gp-monitor poll` call (§18
    Correlation). Same time-prefixed-plus-random-suffix shape as `macro_monitor.log.new_run_id`
    (`fleet_logging.new_run_id` additionally reuses a pre-existing `$RUN_ID` from a parent process
    if one is set — this repo has no orchestrating shell script that sets one, so that extra
    behavior is a no-op here today).
    """
    return _fleet_new_run_id(prefix="run")


def log_event(level: str, event: str, msg: str | None = None, **fields) -> None:
    """Emit one canonical JSON log line to stderr.

    ``level`` — this package's own spelling (``debug``/``info``/``warn``/``error``/``critical``);
    the shared pipeline maps it to the fleet's canonical enum, never this module.
    ``event`` — a short, stable, dot-namespaced string (``auth.attempt_failed``, ``publish.failed``,
    ``cease.halted``) — the thing a dashboard panel or ``absent()`` alert actually filters on;
    ``msg`` is prose for a human in Grafana Explore and must never be what anything alerts on.
    ``fields`` — everything else (``run_id``, ``outcome``, work-quantity, closed-set diagnostic
    codes, ``duration_ms``, ...) per §18's canonical log line shape. Never pass a raw exception
    message, HTTP response body, credential, or token here — see module docstring.
    """
    _fleet_log_event(level, event, msg, service=SERVICE, stream=sys.stderr, **fields)
