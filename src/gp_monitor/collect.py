"""Fetch this cycle's usage/billing figures from Georgia Power and shape them for HA publish.

Thin wrapper around `southern_company_api.Account.get_month_data(jwt)` (plan.md's Architecture step
4 / Integration points). Takes `(account, api)` — never a bare jwt string — where `api` is the live
`southern_company_api.SouthernCompanyAPI` instance handed back by `auth.login()`. `api.jwt` is an
`async def` `@property` that auto-refreshes on wall-clock expiry when awaited; this module always
does `jwt = await api.jwt` immediately before `account.get_month_data(jwt)` rather than catching a
stale-token failure after the fact and retrying with the same jwt (plan.md's Architecture / step 4
and Integration points sections are explicit about this ordering).

The jwt-fetch-and-fetch-data pair is retried as one bounded unit, up to `max_login_attempts` (same
counter/default as `auth.login()`'s own bounded retry — config.py's `Config.max_login_attempts`,
default 2): a JWT that looked fresh when awaited can still be rejected by the data endpoint (clock
skew between this host and Georgia Power, or a token invalidated between the awaited refresh and the
request landing); re-awaiting `api.jwt` on the next attempt gives the library's own auto-refresh
another chance to hand back a genuinely usable token. Only the library's own narrow login/data
exception set is retried (`InvalidLogin`, `NoScTokenFound`, `NoJwtTokenFound`, `NoRequestTokenFound`,
`CantReachSouthernCompany` from the jwt fetch; `UsageDataFailure`, `CantReachSouthernCompany` from
`get_month_data`) — anything else propagates immediately, unretried.

No raw exception text, HTTP response body, or credential/token is ever logged here — only the
exception's class name (a closed-set-ish diagnostic label, not response content) and small integers
(attempt/max_attempts) — see log.py's module docstring and plan.md's Data model section.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass

from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    InvalidLogin,
    NoJwtTokenFound,
    NoRequestTokenFound,
    NoScTokenFound,
    UsageDataFailure,
)

from .log import log_event

# Exceptions that can surface from `await api.jwt` re-triggering the library's internal login chain
# (request_token -> sc -> jwt) when the cached token has expired. Matches plan.md's Integration
# points / Architecture step 3 exception set, plus `NoRequestTokenFound` (raised deeper in the same
# chain by `get_request_verification_token`, per southern_company_api/parser.py).
_JWT_REFRESH_EXCEPTIONS = (
    InvalidLogin,
    NoScTokenFound,
    NoJwtTokenFound,
    NoRequestTokenFound,
    CantReachSouthernCompany,
)

# Exceptions `Account.get_month_data` itself raises (southern_company_api/account.py).
_DATA_EXCEPTIONS = (UsageDataFailure, CantReachSouthernCompany)

# Generous upper bounds for "plausible" -- guards against a malformed/buggy upstream response
# injecting an absurd value into HA's energy/monetary sensors (plan.md's API / interface contract
# section), not a real usage/billing limit. A residential Georgia Power account is not going to
# report >100,000 kWh or >$100,000 in a single billing month.
_MAX_PLAUSIBLE_KWH = 100_000.0
_MAX_PLAUSIBLE_DOLLARS = 100_000.0


class CollectFailed(Exception):
    """Raised when this cycle's usage/billing data could not be collected or failed validation.

    Covers: exhausting `max_login_attempts` re-fetching the JWT, `get_month_data` raising
    `UsageDataFailure`/`CantReachSouthernCompany`, or the response failing the positive/plausible
    value check. Never carries raw exception text -- see module docstring.
    """


@dataclass(frozen=True)
class UsageAndBilling:
    """The HA-publish-ready shape collect.py maps `MonthlyUsage` onto (plan.md's Architecture step 4
    and API / interface contract section)."""

    total_kwh_used: float
    dollars_to_date: float
    period_start: str  # ISO8601 date, e.g. "2026-08-01"
    period_end: str  # ISO8601 date, e.g. "2026-08-16"


def _billing_period() -> tuple[str, str]:
    """First-of-month..today, the same window `Account.get_month_data` queries internally
    (southern_company_api/account.py builds `startDate`/`endDate` this same way). The library's
    `MonthlyUsage` dataclass does not itself carry the period it queried, so collect.py recomputes
    it independently -- a race of at most the query's own round-trip, acceptable for a once-daily
    poll (plan.md's API / interface contract `period_start`/`period_end` fields)."""
    today = datetime.date.today()
    period_start = today.replace(day=1)
    return period_start.isoformat(), today.isoformat()


def _validate_reading(name: str, value: float, max_plausible: float) -> float:
    invalid_kind = None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or math.isnan(value) or math.isinf(value):
        invalid_kind = "not_finite"
    elif value < 0:
        invalid_kind = "negative"
    elif value > max_plausible:
        invalid_kind = "implausible"

    if invalid_kind is not None:
        log_event(
            "error",
            "collect.failed",
            reason="invalid_value",
            field=name,
            invalid_kind=invalid_kind,
        )
        raise CollectFailed(f"collect.failed: {name} failed validation ({invalid_kind})")
    return float(value)


def _check_monotonicity_best_effort(total_kwh_used: float) -> None:
    """Best-effort sanity check -- `total_kwh_used` shouldn't decrease within the same month (except
    near the 1st, when the billing period resets). Logs a WARNING, never aborts (plan.md's API /
    interface contract section).

    This repo has no local database (plan.md's Data model section) and the one local state file this
    module could reach, `data/breaker_state.json`, only tracks failure counters/reasons -- it never
    stores a prior `total_kwh_used` reading (see `breaker.py`'s schema). There is therefore no
    prior-cycle value accessible from this module to compare against. Implemented as a documented
    no-op rather than fabricating a comparison against something that isn't actually there; a real
    check requires wiring in a small local cache (or extending breaker.py's schema) -- left as a
    documented gap, not silently pretended-away.
    """
    return


def _build_result(total_kwh_used: float, dollars_to_date: float) -> UsageAndBilling:
    total_kwh_used = _validate_reading("total_kwh_used", total_kwh_used, _MAX_PLAUSIBLE_KWH)
    dollars_to_date = round(
        _validate_reading("dollars_to_date", dollars_to_date, _MAX_PLAUSIBLE_DOLLARS), 2
    )
    _check_monotonicity_best_effort(total_kwh_used)
    period_start, period_end = _billing_period()
    return UsageAndBilling(
        total_kwh_used=total_kwh_used,
        dollars_to_date=dollars_to_date,
        period_start=period_start,
        period_end=period_end,
    )


async def get_usage_and_billing(account, api, max_login_attempts: int = 2) -> UsageAndBilling:
    """Fetch and shape this cycle's usage/billing figures.

    `account` -- a `southern_company_api.Account` (from `auth.login()`'s returned tuple).
    `api` -- the live `southern_company_api.SouthernCompanyAPI` instance (same tuple) -- **not** a
    bare jwt string, so this function can re-await `api.jwt` itself if the token it gets back turns
    out not to work (see module docstring).
    `max_login_attempts` -- bounds the jwt-fetch-and-fetch-data retry loop; defaults to
    `config.py`'s `Config.max_login_attempts` default (2). Callers should pass the loaded config's
    value explicitly.

    Raises `CollectFailed` if every attempt fails, or if the response fails validation.
    """
    last_error: Exception | None = None

    for attempt in range(1, max_login_attempts + 1):
        try:
            jwt = await api.jwt
        except _JWT_REFRESH_EXCEPTIONS as exc:
            last_error = exc
            log_event(
                "warn",
                "collect.failed",
                reason="jwt_refresh_failed",
                err_type=type(exc).__name__,
                attempt=attempt,
                max_attempts=max_login_attempts,
            )
            continue

        try:
            monthly = await account.get_month_data(jwt)
        except _DATA_EXCEPTIONS as exc:
            last_error = exc
            log_event(
                "warn",
                "collect.failed",
                reason="get_month_data_failed",
                err_type=type(exc).__name__,
                attempt=attempt,
                max_attempts=max_login_attempts,
            )
            continue

        result = _build_result(monthly.total_kwh_used, monthly.dollars_to_date)
        log_event("info", "collect.completed", attempt=attempt)
        return result

    log_event(
        "error",
        "collect.failed",
        reason="attempts_exhausted",
        err_type=type(last_error).__name__ if last_error is not None else None,
        max_attempts=max_login_attempts,
    )
    raise CollectFailed(
        f"collect.failed: could not collect usage/billing data after {max_login_attempts} attempt(s)"
    ) from last_error
