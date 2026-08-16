"""Georgia Power login — thin wrapper around `southern_company_api.SouthernCompanyAPI` with
bounded retry (plan.md's Architecture step 3 / Integration points).

`login()` reads `GP_USERNAME`/`GP_PASSWORD` from the process environment (populated by systemd's
`EnvironmentFile=`, per FR-1/FR-11 — never via python-dotenv tree-walking, same posture as
`config.py`'s `_NO_DOTENV` guard) unless explicit `username`/`password` are passed (used by tests to
avoid touching the real environment).

Retries only on the library's own narrow, documented auth-failure exception set — `InvalidLogin`,
`NoScTokenFound`, `NoJwtTokenFound`, `CantReachSouthernCompany` (confirmed by reading
`southern_company_api/parser.py` directly) — up to `max_login_attempts` (config.yaml, default 2).
Any other exception (a bug, a `ValueError`, ...) is never caught here and propagates immediately —
retrying an unrelated failure would just mask it.

CRITICAL INTERFACE REQUIREMENT (plan.md, fixed during adversarial spec review): on success this
returns `(session, api, account)` where `api` is the *live* `SouthernCompanyAPI` instance, never a
bare jwt string. `SouthernCompanyAPI.jwt` is an `async def` `@property` that auto-refreshes on
wall-clock expiry when re-awaited — `collect.py` needs the live `api` object so it can re-await
`.jwt` itself immediately before each data call, not a string captured once at login time that could
go stale mid-cycle.

Never logs raw exception text, HTTP response bodies, or any credential/token value (plan.md's Data
model section — either could echo back the Georgia Power JWT/session token) — only the closed-set
diagnostic codes in `_REASON_CODES` below.
"""

from __future__ import annotations

import os

from aiohttp import ClientSession
from southern_company_api import Account, SouthernCompanyAPI
from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    InvalidLogin,
    NoJwtTokenFound,
    NoScTokenFound,
)

from .log import log_event

# Exceptions treated as a retryable login-attempt failure — see module docstring. Any exception not
# in this tuple (e.g. ValueError) propagates uncaught, never retried.
_RETRYABLE_EXCEPTIONS = (
    InvalidLogin,
    NoScTokenFound,
    NoJwtTokenFound,
    CantReachSouthernCompany,
)

# Closed-set diagnostic codes for fleet-logging — never the raw exception text (plan.md's Data model
# section). Keys are exact types from `_RETRYABLE_EXCEPTIONS`; `_reason_for` falls back to
# `"unknown"` for anything else (defensive — should be unreachable given the `except` clause below).
_REASON_CODES: dict[type[Exception], str] = {
    InvalidLogin: "invalid_login",
    NoScTokenFound: "no_sc_token",
    NoJwtTokenFound: "no_jwt_token",
    CantReachSouthernCompany: "cant_reach",
}


class AuthFailedBounded(Exception):
    """Raised when Georgia Power login has failed on every attempt up to `max_login_attempts`."""


def _reason_for(exc: Exception) -> str:
    return _REASON_CODES.get(type(exc), "unknown")


def _select_account(accounts: list[Account]) -> Account:
    """Pick the account `collect.py` operates on: the one flagged `primary`, or the first account
    if none is flagged. `accounts` is never empty here — callers only reach this after confirming
    at least one account came back.
    """
    for account in accounts:
        if getattr(account, "primary", False):
            return account
    return accounts[0]


async def login(
    session: ClientSession,
    max_login_attempts: int,
    username: str | None = None,
    password: str | None = None,
) -> tuple[ClientSession, SouthernCompanyAPI, Account]:
    """Log in to Georgia Power via `SouthernCompanyAPI`, retrying up to `max_login_attempts` times
    on the library's own narrow auth-failure exception set.

    `username`/`password` default to `GP_USERNAME`/`GP_PASSWORD` from the process environment when
    not passed explicitly (tests pass them directly instead of mutating `os.environ`).

    Returns `(session, api, account)` on success — see module docstring for why `api` (the live
    instance) is returned rather than a bare jwt string.

    Raises `AuthFailedBounded` once every attempt has failed. Raises immediately, uncaught, on any
    exception outside `_RETRYABLE_EXCEPTIONS` (e.g. a `ValueError`) — never retried.
    """
    resolved_username = username if username is not None else os.environ.get("GP_USERNAME")
    resolved_password = password if password is not None else os.environ.get("GP_PASSWORD")
    if not resolved_username or not resolved_password:
        log_event(
            "error",
            "auth.failed_bounded",
            "Georgia Power login has no credentials to try",
            reason="missing_credentials",
        )
        raise AuthFailedBounded("auth.failed_bounded: missing_credentials")

    last_reason = "unknown"
    for attempt in range(1, max_login_attempts + 1):
        api = SouthernCompanyAPI(resolved_username, resolved_password, session)
        try:
            await api.connect()
            accounts = await api.accounts
            if not accounts:
                raise CantReachSouthernCompany("no accounts returned")
        except _RETRYABLE_EXCEPTIONS as exc:
            last_reason = _reason_for(exc)
            log_event(
                "warn",
                "auth.attempt_failed",
                "Georgia Power login attempt failed",
                attempt=attempt,
                max_attempts=max_login_attempts,
                reason=last_reason,
            )
            continue

        account = _select_account(accounts)
        log_event(
            "info",
            "auth.succeeded",
            "Georgia Power login succeeded",
            attempt=attempt,
            max_attempts=max_login_attempts,
        )
        return session, api, account

    log_event(
        "error",
        "auth.failed_bounded",
        "Georgia Power login exhausted all attempts",
        max_attempts=max_login_attempts,
        reason=last_reason,
    )
    raise AuthFailedBounded(f"auth.failed_bounded: {last_reason}")
