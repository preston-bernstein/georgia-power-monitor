"""Bounded-retry login tests for `gp_monitor.auth`.

Mocks `southern_company_api.SouthernCompanyAPI` entirely -- no real network call, no real
credentials (per steps.md's Step 4 Test field: "Test with real credentials requires Preston to
provide `.env` at deploy time (not required for this step's local verification)"). Covers the three
scenarios called out there:

  (a) first attempt fails with `InvalidLogin`, second attempt succeeds -> returns (session, api,
      account)
  (b) both attempts fail -> raises `AuthFailedBounded`
  (c) a non-retryable exception (`ValueError`) is not caught -> propagates uncaught
"""

from __future__ import annotations

import json

import pytest
from southern_company_api.exceptions import (
    CantReachSouthernCompany,
    InvalidLogin,
    NoJwtTokenFound,
    NoScTokenFound,
)

import gp_monitor.auth as auth_module
from gp_monitor.auth import AuthFailedBounded, _reason_for, _select_account, login


class _FakeAccount:
    def __init__(self, primary: bool = True, number: str = "acct-1"):
        self.primary = primary
        self.number = number


class _FakeApi:
    """Stand-in for `SouthernCompanyAPI`. `auth.login` instantiates a fresh instance per attempt,
    so class-level state (set by the `fake_api` fixture below) tracks behavior across attempts.
    """

    connect_effects: list = []
    accounts_result: list = []
    instances: list = []

    def __init__(self, username, password, session):
        self.username = username
        self.password = password
        self.session = session
        type(self).instances.append(self)

    async def connect(self) -> None:
        effect = type(self).connect_effects.pop(0)
        if effect is not None:
            raise effect

    @property
    async def accounts(self):
        return type(self).accounts_result


@pytest.fixture
def fake_api(monkeypatch):
    _FakeApi.connect_effects = []
    _FakeApi.accounts_result = []
    _FakeApi.instances = []
    monkeypatch.setattr(auth_module, "SouthernCompanyAPI", _FakeApi)
    return _FakeApi


def _last_log_lines(capsys) -> list[dict]:
    captured = capsys.readouterr()
    return [json.loads(line) for line in captured.err.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_login_retries_then_succeeds(fake_api, capsys):
    account = _FakeAccount()
    fake_api.connect_effects = [InvalidLogin(), None]
    fake_api.accounts_result = [account]

    session = object()
    result_session, result_api, result_account = await login(
        session, max_login_attempts=2, username="preston", password="hunter2"
    )

    assert result_session is session
    assert isinstance(result_api, _FakeApi)
    assert result_account is account
    # A fresh api instance is constructed per attempt -- the second (successful) attempt is the one
    # returned.
    assert len(fake_api.instances) == 2
    assert result_api is fake_api.instances[-1]
    # `SouthernCompanyAPI(username, password, session)` -- the real session object must be threaded
    # through to every constructed instance, not dropped/replaced.
    for instance in fake_api.instances:
        assert instance.session is session

    lines = _last_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert events == ["auth.attempt_failed", "auth.succeeded"]
    failed_line = lines[0]
    assert failed_line["reason"] == "invalid_login"
    assert failed_line["level"] == "warn"
    assert failed_line["msg"] == "Georgia Power login attempt failed"
    success_line = lines[1]
    assert success_line["level"] == "info"
    assert success_line["msg"] == "Georgia Power login succeeded"
    # Never the raw exception text/credentials.
    dumped = json.dumps(failed_line)
    assert "hunter2" not in dumped


@pytest.mark.asyncio
async def test_login_raises_auth_failed_bounded_after_exhausting_attempts(fake_api, capsys):
    fake_api.connect_effects = [InvalidLogin(), CantReachSouthernCompany()]

    with pytest.raises(AuthFailedBounded) as excinfo:
        await login(object(), max_login_attempts=2, username="preston", password="hunter2")

    assert str(excinfo.value) == "auth.failed_bounded: cant_reach"
    assert len(fake_api.instances) == 2
    lines = _last_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert events == ["auth.attempt_failed", "auth.attempt_failed", "auth.failed_bounded"]
    assert lines[-1]["reason"] == "cant_reach"
    assert lines[-1]["level"] == "error"
    assert lines[-1]["msg"] == "Georgia Power login exhausted all attempts"
    assert lines[-1]["max_attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [InvalidLogin(), NoScTokenFound(), NoJwtTokenFound(), CantReachSouthernCompany()],
)
async def test_login_retryable_exception_set_matches_library(fake_api, exc):
    """Each of the four named exceptions is individually retryable (not just InvalidLogin)."""
    fake_api.connect_effects = [exc, None]
    fake_api.accounts_result = [_FakeAccount()]

    _, api, _ = await login(object(), max_login_attempts=2, username="u", password="p")
    assert isinstance(api, _FakeApi)


@pytest.mark.asyncio
async def test_login_does_not_catch_non_retryable_exception(fake_api):
    fake_api.connect_effects = [ValueError("boom")]

    with pytest.raises(ValueError, match="boom"):
        await login(object(), max_login_attempts=2, username="preston", password="hunter2")

    # No retry attempted for a non-retryable exception.
    assert len(fake_api.instances) == 1


@pytest.mark.asyncio
async def test_login_missing_credentials_raises_without_attempting(fake_api, monkeypatch, capsys):
    monkeypatch.delenv("GP_USERNAME", raising=False)
    monkeypatch.delenv("GP_PASSWORD", raising=False)

    with pytest.raises(AuthFailedBounded) as excinfo:
        await login(object(), max_login_attempts=2, username=None, password=None)

    assert str(excinfo.value) == "auth.failed_bounded: missing_credentials"
    assert len(fake_api.instances) == 0

    lines = _last_log_lines(capsys)
    assert len(lines) == 1
    line = lines[0]
    assert line["level"] == "error"
    assert line["event"] == "auth.failed_bounded"
    assert line["msg"] == "Georgia Power login has no credentials to try"
    assert line["reason"] == "missing_credentials"


@pytest.mark.asyncio
async def test_login_reads_credentials_from_process_environment(fake_api, monkeypatch):
    monkeypatch.setenv("GP_USERNAME", "env-user")
    monkeypatch.setenv("GP_PASSWORD", "env-pass")
    fake_api.connect_effects = [None]
    fake_api.accounts_result = [_FakeAccount()]

    await login(object(), max_login_attempts=1)

    assert fake_api.instances[0].username == "env-user"
    assert fake_api.instances[0].password == "env-pass"


@pytest.mark.asyncio
async def test_login_missing_only_username_raises_without_attempting(fake_api, monkeypatch):
    """`not resolved_username or not resolved_password` -- either alone missing must still raise.
    Guards against an `or`/`and` mix-up: with `and`, a missing username but present password would
    wrongly be treated as having credentials to try."""
    monkeypatch.delenv("GP_USERNAME", raising=False)
    monkeypatch.setenv("GP_PASSWORD", "hunter2")

    with pytest.raises(AuthFailedBounded):
        await login(object(), max_login_attempts=2, username=None, password=None)

    assert len(fake_api.instances) == 0


@pytest.mark.asyncio
async def test_login_missing_only_password_raises_without_attempting(fake_api, monkeypatch):
    monkeypatch.setenv("GP_USERNAME", "preston")
    monkeypatch.delenv("GP_PASSWORD", raising=False)

    with pytest.raises(AuthFailedBounded):
        await login(object(), max_login_attempts=2, username=None, password=None)

    assert len(fake_api.instances) == 0


@pytest.mark.asyncio
async def test_login_logs_full_attempt_and_max_attempts_fields(fake_api, capsys):
    """Pins the exact `attempt`/`max_attempts` integers logged at each stage, not just the event
    names -- a mutant that swaps `attempt` for `max_attempts` (or vice versa) in a `log_event` call
    must fail this."""
    fake_api.connect_effects = [InvalidLogin(), None]
    fake_api.accounts_result = [_FakeAccount()]

    await login(object(), max_login_attempts=2, username="preston", password="hunter2")

    lines = _last_log_lines(capsys)
    failed_line, success_line = lines
    assert failed_line["attempt"] == 1
    assert failed_line["max_attempts"] == 2
    assert success_line["attempt"] == 2
    assert success_line["max_attempts"] == 2


@pytest.mark.asyncio
async def test_login_exhausted_log_line_carries_final_reason_and_max_attempts(fake_api, capsys):
    fake_api.connect_effects = [InvalidLogin(), CantReachSouthernCompany()]

    with pytest.raises(AuthFailedBounded):
        await login(object(), max_login_attempts=2, username="preston", password="hunter2")

    lines = _last_log_lines(capsys)
    exhausted = lines[-1]
    assert exhausted["event"] == "auth.failed_bounded"
    assert exhausted["reason"] == "cant_reach"
    assert exhausted["max_attempts"] == 2


def test_select_account_returns_non_first_account_flagged_primary():
    """A `primary=True` account that is *not* first in the list must still be selected -- catches a
    mutant that always returns `accounts[0]` or that short-circuits the loop's condition to always
    False."""
    first = _FakeAccount(primary=False, number="acct-1")
    second = _FakeAccount(primary=True, number="acct-2")
    third = _FakeAccount(primary=False, number="acct-3")

    assert _select_account([first, second, third]) is second


def test_select_account_falls_back_to_first_when_none_primary():
    first = _FakeAccount(primary=False, number="acct-1")
    second = _FakeAccount(primary=False, number="acct-2")

    assert _select_account([first, second]) is first


def test_select_account_first_account_flagged_primary_is_selected():
    first = _FakeAccount(primary=True, number="acct-1")
    second = _FakeAccount(primary=False, number="acct-2")

    assert _select_account([first, second]) is first


@pytest.mark.asyncio
async def test_login_zero_max_attempts_never_loops_reason_stays_unknown(fake_api, capsys):
    """`max_login_attempts=0` -- `range(1, 1)` is empty, so the retry loop body never runs and
    `last_reason` is never reassigned from its initial `"unknown"` value. Pins that initial value
    directly, since no retryable exception ever fires to set it otherwise."""
    with pytest.raises(AuthFailedBounded) as excinfo:
        await login(object(), max_login_attempts=0, username="preston", password="hunter2")

    assert str(excinfo.value) == "auth.failed_bounded: unknown"
    assert len(fake_api.instances) == 0
    lines = _last_log_lines(capsys)
    assert lines[-1]["reason"] == "unknown"


def test_select_account_object_missing_primary_attribute_is_treated_as_not_primary():
    """`getattr(account, "primary", False)` -- pins the `False` default exactly, which only
    matters when an account object has no `primary` attribute at all (a real library response
    shape this repo can't fully control). Puts the attribute-less object *first* and the genuinely
    primary account second: a mutant defaulting to `True` (or omitting the default entirely) would
    wrongly select the attribute-less first account instead of the real primary."""

    class _AccountWithoutPrimaryAttr:
        number = "acct-no-primary"

    first = _AccountWithoutPrimaryAttr()
    second = _FakeAccount(primary=True, number="acct-2")

    assert _select_account([first, second]) is second


@pytest.mark.parametrize(
    "exc,expected",
    [
        (InvalidLogin(), "invalid_login"),
        (NoScTokenFound(), "no_sc_token"),
        (NoJwtTokenFound(), "no_jwt_token"),
        (CantReachSouthernCompany(), "cant_reach"),
    ],
)
def test_reason_for_maps_each_retryable_exception_type(exc, expected):
    assert _reason_for(exc) == expected


def test_reason_for_unknown_exception_falls_back_to_unknown():
    assert _reason_for(ValueError("not in the closed set")) == "unknown"
