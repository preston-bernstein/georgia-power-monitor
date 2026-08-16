"""gp-monitor CLI — Click entry point.

Two commands: `gp-monitor poll` (the main pipeline entry point) and `gp-monitor --version`.

`poll` wires the pipeline (plan.md's Architecture): load config -> guard checks (breaker trip,
cease-fire, rate-limiter) -> auth.login() -> collect.get_usage_and_billing() -> publish.push_to_ha()
-> metrics.write_metrics() -- skipping the publish/metrics steps under `--dry-run` -- logging each
boundary via `fleet_logging` (log.py), and exiting 0 on success / 1 on any failure.

This is Step 11b of a 3-step build (steps.md): guard integration on top of Step 11a's happy-path
orchestration skeleton. Three guard checks run immediately before the first network attempt
(`auth.login`), all inside `_login_and_collect` at the point the Step 11a skeleton marked with
`# TODO(11b)` -- the same "before attempting login" ordering `breaker.py`'s own module docstring
documents:

1. `Breaker.check_tripped()` -- raises `BreakerTripped` if `consecutive_failures` has reached
   `config.max_consecutive_failures`. The cycle aborts without attempting login at all. This is
   handled as a special case in `poll` (not fed back into `breaker.record_failure` -- there is no
   `FailureReason` for "the breaker was already tripped", and re-recording a failure against an
   already-tripped breaker would be redundant).
2. `scraper_commons.cease.is_halted("georgia_power")` -- a human-recorded cross-consumer legal
   safety rail (shared with every other scraper-commons consumer). If halted, raises
   `cease.PlatformHalted` (the module's own documented "consumer's convenience" pattern), mapped to
   `FailureReason.CEASE_HALTED`.
3. `scraper_commons.rate.RateGovernor().acquire(_GP_RATE_HOST)` -- paces this cycle's Georgia Power
   traffic. Called exactly once per cycle here (not before each individual `southern_company_api`
   call inside `auth.py`/`collect.py`) now that the egress-isolation layer that used to do
   per-request pacing has been removed. Any exception `acquire()` raises is treated as this cycle
   being denied admission and re-raised as `RateLimited`, mapped to `FailureReason.RATE_LIMITED`.

Every other failure -- `AuthFailedBounded`, `CollectFailed`, `PublishFailed` -- is mapped to a
`FailureReason` and recorded via `Breaker.record_failure()` before the process exits 1
(`_FAILURE_REASON_MAP` below). On a fully successful cycle, `Breaker.record_success()` resets the
consecutive-failure counter to 0.

This is Step 11c of a 3-step build (steps.md): the final `--dry-run` refinement on top of Step
11a's orchestration skeleton and Step 11b's guard integration above. `--dry-run` skips only
`publish.push_to_ha()` and `metrics.write_metrics()` -- `auth.login()` and
`collect.get_usage_and_billing()` still run for real (see the comment at the `_login_and_collect`
call site in `poll` below): with egress isolation removed, there is no way to make a dry run fully
side-effect-free without also stubbing out auth/collect, which isn't this flag's job. Both skipped
steps are logged explicitly as `outcome="skipped"` (`cli.publish_skipped`/`cli.metrics_skipped`),
never just silently omitted, so a dry run's log stream can't be mistaken for a crash before that
step or for `outcome="success"`.
"""

from __future__ import annotations

import asyncio
import sys

import click
from aiohttp import ClientSession
from scraper_commons import cease
from scraper_commons.rate import RateGovernor

from . import __version__
from .auth import AuthFailedBounded, login
from .breaker import Breaker, BreakerTripped, FailureReason
from .collect import CollectFailed, UsageAndBilling, get_usage_and_billing
from .config import load_config
from .log import log_event, new_run_id
from .metrics import write_metrics
from .publish import PublishFailed, push_to_ha

# The platform name registered with scraper-commons' cross-consumer cease-fire registry (see
# `scraper_commons.cease.KNOWN_PLATFORMS`) -- must match exactly, `is_halted`/`PlatformHalted`
# validate against that closed set.
_CEASE_PLATFORM = "georgia_power"

# Rate-governor pacing key. Not tied to any single `southern_company_api` request URL -- this is
# the *once-per-cycle* admission gate for "all Georgia Power traffic this poll makes", so a single,
# stable host string is used regardless of which of southerncompany.com's several subdomains the
# login/collect steps end up hitting internally.
_GP_RATE_HOST = "southerncompany.com"

class RateLimited(Exception):
    """Raised when this cycle is denied admission by the rate governor -- i.e. `RateGovernor.acquire`
    raised. Wrapping the governor's own exception in this module-local type keeps the
    `_FAILURE_REASON_MAP` lookup a simple, closed `type(exc)` dict lookup rather than needing an
    `isinstance` fallback chain."""


# Exception type -> `FailureReason` for `Breaker.record_failure()`. `BreakerTripped` is
# deliberately absent -- it is handled separately in `poll` (see module docstring).
_FAILURE_REASON_MAP: dict[type[Exception], FailureReason] = {
    cease.PlatformHalted: FailureReason.CEASE_HALTED,
    RateLimited: FailureReason.RATE_LIMITED,
    AuthFailedBounded: FailureReason.INVALID_LOGIN,
    CollectFailed: FailureReason.COLLECT_FAILED,
    PublishFailed: FailureReason.PUBLISH_FAILED,
}


def _reason_for(exc: Exception) -> FailureReason | None:
    """Look up the `FailureReason` for `exc`'s exact type, or `None` if this exception type is not
    part of the guard/pipeline closed set (e.g. a config or metrics failure) -- callers must treat
    `None` as "do not call `Breaker.record_failure`"."""
    return _FAILURE_REASON_MAP.get(type(exc))


async def _login_and_collect(config, run_id: str, breaker: Breaker) -> UsageAndBilling:
    """The two async pipeline steps -- `auth.login`, `collect.get_usage_and_billing` -- run inside
    one `aiohttp.ClientSession` and one `asyncio.run()` call from the sync `poll` command below.

    Any exception raised by either step, or by the guard checks below, propagates uncaught to the
    caller, which is responsible for logging + the exit-1 boundary + `Breaker.record_failure`
    (`poll`'s single `except` chain below) -- kept here as a plain propagate rather than a local
    try/except so there is exactly one place in this module that decides "pipeline step failed ->
    exit 1".
    """
    async with ClientSession() as session:
        # Step 11b guards -- all run before `login()`, never after (breaker.py's module docstring).
        breaker.check_tripped()

        if cease.is_halted(_CEASE_PLATFORM):
            raise cease.PlatformHalted(f"{_CEASE_PLATFORM} is halted, refusing to poll")

        try:
            RateGovernor().acquire(_GP_RATE_HOST)
        except Exception as exc:
            raise RateLimited("rate governor denied admission for this cycle") from exc

        _session, api, account = await login(session, config.max_login_attempts)
        log_event("info", "cli.auth_completed", run_id=run_id)

        usage_and_billing = await get_usage_and_billing(
            account, api, max_login_attempts=config.max_login_attempts
        )
        log_event("info", "cli.collect_completed", run_id=run_id)
        return usage_and_billing


@click.group()
@click.version_option(__version__, prog_name="gp-monitor")
def main() -> None:
    """Georgia Power Monitor — polls usage/billing data and publishes to Home Assistant."""


@main.command("poll")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to config.yaml (default: ./config.yaml, see config.py's DEFAULT_CONFIG_PATH).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Run auth + collect but skip publishing to Home Assistant and writing metrics.",
)
def poll(config_path: str | None, dry_run: bool) -> None:
    """Run one poll cycle: login, collect usage/billing, publish to Home Assistant, write metrics."""
    run_id = new_run_id()
    log_event("info", "cli.poll_started", run_id=run_id, dry_run=dry_run)

    step = "config"
    breaker: Breaker | None = None
    try:
        config = load_config(config_path)
        breaker = Breaker(max_consecutive_failures=config.max_consecutive_failures)

        # `--dry-run` only skips the downstream publish/metrics steps below. `_login_and_collect`
        # above always runs for real -- a genuine Georgia Power login (`auth.login`) and a genuine
        # data fetch (`collect.get_usage_and_billing`) -- regardless of `dry_run`. There is no
        # network-observable-side-effect-free way to validate the pipeline end to end without
        # authenticating: egress isolation (which used to let a dry run fake this boundary) has
        # been removed, and `--dry-run` was never meant to stub out auth/collect -- only to let an
        # operator confirm login + collection still work without also writing to Home Assistant or
        # the metrics textfile (plan.md).
        step = "auth_and_collect"
        usage_and_billing = asyncio.run(_login_and_collect(config, run_id, breaker))

        if not dry_run:
            step = "publish"
            push_to_ha(
                config,
                usage_and_billing.total_kwh_used,
                usage_and_billing.dollars_to_date,
                usage_and_billing.period_start,
                usage_and_billing.period_end,
                run_id=run_id,
            )
            log_event("info", "cli.publish_completed", run_id=run_id)

            step = "metrics"
            write_metrics(
                success=True,
                work_quantity=usage_and_billing.total_kwh_used,
                work_available=usage_and_billing.dollars_to_date,
            )
            log_event("info", "cli.metrics_completed", run_id=run_id)
        else:
            # Explicit "skipped" markers (both in the event name and in `outcome`, matching the
            # `outcome="success"`/`outcome="failed"` fields logged elsewhere in this module) --
            # these steps must never be silently omitted from the log stream, since the absence of
            # a `cli.publish_completed`/`cli.metrics_completed` line is otherwise indistinguishable
            # from "the process crashed before reaching that step."
            log_event(
                "info", "cli.publish_skipped", run_id=run_id, outcome="skipped", reason="dry_run"
            )
            log_event(
                "info", "cli.metrics_skipped", run_id=run_id, outcome="skipped", reason="dry_run"
            )
    except BreakerTripped as exc:
        # Already tripped -- login was never attempted (see `_login_and_collect`). There is no
        # `FailureReason` for "the breaker was already tripped" and re-recording a failure here
        # would double-count against a breaker that is already open, so `record_failure` is
        # deliberately not called.
        log_event(
            "error",
            "cli.breaker_tripped",
            run_id=run_id,
            outcome="failed",
            step=step,
            reason="breaker_tripped",
            consecutive_failures=exc.consecutive_failures,
            last_failure_reason=exc.last_failure_reason,
        )
        sys.exit(1)
    except Exception as exc:
        reason = _reason_for(exc)
        if reason is not None and breaker is not None:
            breaker.record_failure(reason)
        log_event(
            "error",
            "cli.poll_failed",
            run_id=run_id,
            outcome="failed",
            step=step,
            err_type=type(exc).__name__,
        )
        sys.exit(1)

    breaker.record_success()
    log_event(
        "info",
        "cli.poll_completed",
        run_id=run_id,
        outcome="success",
        work_quantity=usage_and_billing.total_kwh_used,
        work_available=usage_and_billing.dollars_to_date,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
