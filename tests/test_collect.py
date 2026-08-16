"""Tests for `gp_monitor.collect`.

Mocks `account.get_month_data` and `api.jwt` entirely -- no real network call, no real credentials.
Covers steps.md's Step 5 Test field:

  (a) the return value contains the correct keys/types, mapped from `MonthlyUsage`
  (b) `dollars_to_date` is rounded to 2 decimal places
  (c) JWT expiry re-fetch: `api.jwt` returns an expired token on the first await, a fresh token on
      the second; `get_month_data` fails against the expired token and succeeds against the fresh
      one -- collect.py's bounded retry (same `max_login_attempts` counter as auth.py) re-awaits
      `api.jwt` and succeeds on the second attempt
  (d) `CollectFailed` is raised on `UsageDataFailure` / `CantReachSouthernCompany` once
      `max_login_attempts` is exhausted
  (e) `CollectFailed` is raised when the upstream response contains NaN/negative/absurd values
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import math

import pytest
from southern_company_api.account import MonthlyUsage
from southern_company_api.exceptions import CantReachSouthernCompany, UsageDataFailure

from gp_monitor.collect import CollectFailed, UsageAndBilling, get_usage_and_billing


def _monthly_usage(dollars_to_date=137.1, total_kwh_used=412.7) -> MonthlyUsage:
    return MonthlyUsage(
        dollars_to_date=dollars_to_date,
        total_kwh_used=total_kwh_used,
        average_daily_usage=25.8,
        average_daily_cost=8.57,
        projected_usage_low=700.0,
        projected_usage_high=820.0,
        projected_bill_amount_low=230.0,
        projected_bill_amount_high=270.0,
    )


class _FakeApi:
    """Stand-in for `SouthernCompanyAPI`. `jwt_values` is popped from (front) on every await of the
    `.jwt` property, so a test can script a sequence of tokens across attempts. An entry may also be
    an `Exception` instance, in which case it is raised instead of returned -- exercising collect.py's
    `jwt_refresh_failed` branch."""

    def __init__(self, jwt_values: list):
        self._jwt_values = list(jwt_values)
        self.jwt_await_count = 0

    @property
    async def jwt(self) -> str:
        self.jwt_await_count += 1
        value = self._jwt_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _FakeAccount:
    """Stand-in for `Account`. `effects` is popped from (front) on every call to
    `get_month_data` -- either a `MonthlyUsage` to return or an exception instance to raise."""

    def __init__(self, effects: list):
        self._effects = list(effects)
        self.calls_with: list[str] = []

    async def get_month_data(self, jwt: str) -> MonthlyUsage:
        self.calls_with.append(jwt)
        effect = self._effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


def _last_log_lines(capsys) -> list[dict]:
    captured = capsys.readouterr()
    return [json.loads(line) for line in captured.err.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_returns_expected_keys_and_types(capsys):
    api = _FakeApi(["fresh-jwt"])
    account = _FakeAccount([_monthly_usage(dollars_to_date=137.1, total_kwh_used=412.7)])

    result = await get_usage_and_billing(account, api)

    assert isinstance(result, UsageAndBilling)
    fields = {f.name for f in dataclasses.fields(result)}
    assert fields == {"total_kwh_used", "dollars_to_date", "period_start", "period_end"}
    assert isinstance(result.total_kwh_used, float)
    assert isinstance(result.dollars_to_date, float)
    assert isinstance(result.period_start, str)
    assert isinstance(result.period_end, str)
    assert result.total_kwh_used == 412.7
    assert result.dollars_to_date == 137.1

    today = datetime.date.today()
    assert result.period_start == today.replace(day=1).isoformat()
    assert result.period_end == today.isoformat()

    # Success is logged, never as a failure event.
    lines = _last_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert events == ["collect.completed"]
    assert lines[0]["level"] == "info"
    assert lines[0]["attempt"] == 1


@pytest.mark.asyncio
async def test_dollars_to_date_is_rounded_to_two_decimal_places():
    api = _FakeApi(["fresh-jwt"])
    account = _FakeAccount([_monthly_usage(dollars_to_date=137.09999999999998)])

    result = await get_usage_and_billing(account, api)

    assert result.dollars_to_date == 137.1
    assert str(result.dollars_to_date) != "137.09999999999998"


@pytest.mark.asyncio
async def test_jwt_expiry_triggers_refetch_and_retry_succeeds(capsys):
    """First attempt: `api.jwt` hands back an already-stale token; `get_month_data` rejects it the
    way the live endpoint would (raises `UsageDataFailure`). Second attempt: collect.py re-awaits
    `api.jwt` (the library's own auto-refresh gives back a fresh token this time) and
    `get_month_data` succeeds against it."""
    api = _FakeApi(["expired-token", "fresh-token"])
    account = _FakeAccount([UsageDataFailure("stale token rejected"), _monthly_usage()])

    result = await get_usage_and_billing(account, api, max_login_attempts=2)

    assert isinstance(result, UsageAndBilling)
    assert api.jwt_await_count == 2
    assert account.calls_with == ["expired-token", "fresh-token"]

    lines = _last_log_lines(capsys)
    events = [line["event"] for line in lines]
    assert events == ["collect.failed", "collect.completed"]
    assert lines[0]["level"] == "warn"
    assert lines[0]["reason"] == "get_month_data_failed"
    assert lines[0]["err_type"] == "UsageDataFailure"
    assert lines[1]["level"] == "info"


@pytest.mark.asyncio
async def test_collect_failed_raised_on_usage_data_failure_after_exhausting_attempts(capsys):
    api = _FakeApi(["t1", "t2"])
    account = _FakeAccount([UsageDataFailure("boom"), UsageDataFailure("boom again")])

    with pytest.raises(CollectFailed):
        await get_usage_and_billing(account, api, max_login_attempts=2)

    lines = _last_log_lines(capsys)
    assert lines[-1]["level"] == "error"
    assert lines[-1]["reason"] == "attempts_exhausted"
    events = [line["event"] for line in lines]
    assert events == ["collect.failed", "collect.failed", "collect.failed"]
    assert events[-1] == "collect.failed"

    # Never the raw exception text.
    dumped = json.dumps(_last_log_lines(capsys))
    assert "boom" not in dumped


@pytest.mark.asyncio
async def test_collect_failed_raised_on_cant_reach_southern_company():
    api = _FakeApi(["t1"])
    account = _FakeAccount([CantReachSouthernCompany("unreachable")])

    with pytest.raises(CollectFailed):
        await get_usage_and_billing(account, api, max_login_attempts=1)


@pytest.mark.parametrize(
    "dollars_to_date,total_kwh_used",
    [
        (float("nan"), 100.0),
        (100.0, float("nan")),
        (float("inf"), 100.0),
        (-5.0, 100.0),
        (100.0, -5.0),
        (10_000_000.0, 100.0),
        (100.0, 10_000_000.0),
    ],
)
@pytest.mark.asyncio
async def test_collect_failed_raised_on_invalid_upstream_values(dollars_to_date, total_kwh_used):
    api = _FakeApi(["fresh-jwt"])
    account = _FakeAccount(
        [_monthly_usage(dollars_to_date=dollars_to_date, total_kwh_used=total_kwh_used)]
    )

    with pytest.raises(CollectFailed):
        await get_usage_and_billing(account, api)


@pytest.mark.asyncio
async def test_non_retried_exception_propagates_uncaught():
    """A non-retryable exception (not in the library's login/data exception set) must not be
    silently swallowed into a bounded retry."""

    api = _FakeApi(["fresh-jwt"])

    class _FakeAccountRaisesValueError:
        async def get_month_data(self, jwt: str) -> MonthlyUsage:
            raise ValueError("not a southern_company_api exception")

    with pytest.raises(ValueError, match="not a southern_company_api exception"):
        await get_usage_and_billing(_FakeAccountRaisesValueError(), api, max_login_attempts=2)


def test_validate_reading_rejects_bad_data_directly():
    """Unit-level check on the validation helper itself, independent of the retry loop."""
    from gp_monitor.collect import _validate_reading

    with pytest.raises(CollectFailed):
        _validate_reading("total_kwh_used", math.nan, 100_000.0)
    with pytest.raises(CollectFailed):
        _validate_reading("total_kwh_used", -1.0, 100_000.0)
    with pytest.raises(CollectFailed):
        _validate_reading("total_kwh_used", 1_000_000.0, 100_000.0)
    assert _validate_reading("total_kwh_used", 412.7, 100_000.0) == 412.7


def test_validate_reading_boundary_exactly_at_max_plausible_is_valid():
    """`value > max_plausible` -- the boundary value itself must be accepted, not rejected. Pins
    `>` against a mutant flipping it to `>=`."""
    from gp_monitor.collect import _validate_reading

    assert _validate_reading("total_kwh_used", 100_000.0, 100_000.0) == 100_000.0


def test_validate_reading_boundary_just_over_max_plausible_is_rejected():
    from gp_monitor.collect import _validate_reading

    with pytest.raises(CollectFailed):
        _validate_reading("total_kwh_used", 100_000.01, 100_000.0)


def test_validate_reading_boundary_zero_is_valid():
    """`value < 0` -- exactly 0 must be accepted, not rejected. Pins `<` against a mutant flipping
    it to `<=`."""
    from gp_monitor.collect import _validate_reading

    assert _validate_reading("dollars_to_date", 0.0, 100_000.0) == 0.0


def test_validate_reading_rejects_bool_even_though_bool_is_an_int_subclass():
    """`isinstance(value, bool)` is explicitly excluded even though `bool` is a subclass of `int` --
    without that guard, `True`/`False` would silently pass through the `isinstance(value, (int,
    float))` check."""
    from gp_monitor.collect import _validate_reading

    with pytest.raises(CollectFailed):
        _validate_reading("total_kwh_used", True, 100_000.0)


@pytest.mark.parametrize(
    "value,max_plausible,expected_kind",
    [
        (math.nan, 100_000.0, "not_finite"),
        (math.inf, 100_000.0, "not_finite"),
        (-1.0, 100_000.0, "negative"),
        (200_000.0, 100_000.0, "implausible"),
    ],
)
def test_validate_reading_reports_the_correct_invalid_kind_and_field_name(
    capsys, value, max_plausible, expected_kind
):
    """Each rejection reason must log its own distinct `invalid_kind`, and the `field` name passed
    in must be threaded through unchanged -- not just "some error was raised"."""
    from gp_monitor.collect import _validate_reading

    with pytest.raises(CollectFailed):
        _validate_reading("dollars_to_date", value, max_plausible)

    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    assert lines[-1]["invalid_kind"] == expected_kind
    assert lines[-1]["field"] == "dollars_to_date"
    assert lines[-1]["reason"] == "invalid_value"
    assert lines[-1]["level"] == "error"
    assert lines[-1]["event"] == "collect.failed"


@pytest.mark.asyncio
async def test_get_month_data_failure_logs_err_type_and_attempt_fields(capsys):
    """`get_month_data_failed` must log the exception's own class name (not a hardcoded string)
    and the correct 1-based `attempt` number -- pins both against mutants that swap in the wrong
    field/value."""
    api = _FakeApi(["expired-token", "fresh-token"])
    # `_FakeApi.jwt` never raises (only `get_month_data` does in this fixture design) -- use
    # CantReachSouthernCompany raised by `get_month_data` to pin err_type/attempt on the
    # `get_month_data_failed` branch.
    account = _FakeAccount([CantReachSouthernCompany("unreachable"), _monthly_usage()])

    result = await get_usage_and_billing(account, api, max_login_attempts=2)

    assert isinstance(result, UsageAndBilling)
    lines = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()
    ]
    failed_line = lines[0]
    assert failed_line["event"] == "collect.failed"
    assert failed_line["reason"] == "get_month_data_failed"
    assert failed_line["err_type"] == "CantReachSouthernCompany"
    assert failed_line["attempt"] == 1
    assert failed_line["max_attempts"] == 2
    assert failed_line["level"] == "warn"


@pytest.mark.asyncio
async def test_attempts_exhausted_log_line_carries_last_err_type(capsys):
    api = _FakeApi(["t1", "t2"])
    account = _FakeAccount([UsageDataFailure("boom"), CantReachSouthernCompany("boom again")])

    with pytest.raises(CollectFailed):
        await get_usage_and_billing(account, api, max_login_attempts=2)

    lines = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()
    ]
    exhausted = lines[-1]
    assert exhausted["reason"] == "attempts_exhausted"
    assert exhausted["err_type"] == "CantReachSouthernCompany"
    assert exhausted["max_attempts"] == 2
    assert exhausted["level"] == "error"
    assert exhausted["event"] == "collect.failed"


def test_build_result_threads_the_correct_field_name_into_validation(capsys):
    """`_build_result` calls `_validate_reading` with a literal field name per value
    ("total_kwh_used"/"dollars_to_date") -- pins those exact strings via the logged `field`, since
    the field name has no effect on the return value/exception raised, only on what's logged."""
    from gp_monitor.collect import _build_result

    with pytest.raises(CollectFailed):
        _build_result(total_kwh_used=math.nan, dollars_to_date=10.0)
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert lines[-1]["field"] == "total_kwh_used"

    with pytest.raises(CollectFailed):
        _build_result(total_kwh_used=10.0, dollars_to_date=math.nan)
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert lines[-1]["field"] == "dollars_to_date"


def test_build_result_rounds_dollars_but_not_kwh():
    from gp_monitor.collect import _build_result

    result = _build_result(total_kwh_used=412.756, dollars_to_date=137.09999999999998)
    assert result.dollars_to_date == 137.1
    assert result.total_kwh_used == 412.756


def test_build_result_rounds_dollars_to_exactly_two_decimal_places():
    """Pins `round(..., 2)` exactly -- a mutant rounding to 3 decimals would still pass a
    2-decimal-input test, so this uses a value whose 2- and 3-decimal roundings differ."""
    from gp_monitor.collect import _build_result

    result = _build_result(total_kwh_used=100.0, dollars_to_date=100.126)
    assert result.dollars_to_date == 100.13


def test_validate_reading_error_message_names_field_and_kind():
    from gp_monitor.collect import _validate_reading

    with pytest.raises(CollectFailed) as excinfo:
        _validate_reading("total_kwh_used", -5.0, 100_000.0)
    assert str(excinfo.value) == "collect.failed: total_kwh_used failed validation (negative)"


@pytest.mark.asyncio
async def test_jwt_refresh_failure_retries_and_logs_err_type(capsys):
    """`await api.jwt` itself raising one of `_JWT_REFRESH_EXCEPTIONS` (the token's own auto-refresh
    chain failing) must be caught, logged as `jwt_refresh_failed` with the exception's class name,
    and retried -- this is a previously-untested branch (the other jwt-retry test only covers
    `get_month_data` rejecting an otherwise-successfully-fetched token)."""
    from southern_company_api.exceptions import NoJwtTokenFound

    api = _FakeApi([NoJwtTokenFound("no jwt"), "fresh-token"])
    account = _FakeAccount([_monthly_usage()])

    result = await get_usage_and_billing(account, api, max_login_attempts=2)

    assert isinstance(result, UsageAndBilling)
    assert api.jwt_await_count == 2
    # get_month_data was only ever called once -- with the fresh token from the second attempt.
    assert account.calls_with == ["fresh-token"]

    lines = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()
    ]
    failed_line = lines[0]
    assert failed_line["event"] == "collect.failed"
    assert failed_line["reason"] == "jwt_refresh_failed"
    assert failed_line["err_type"] == "NoJwtTokenFound"
    assert failed_line["attempt"] == 1
    assert failed_line["max_attempts"] == 2
    assert failed_line["level"] == "warn"
    assert lines[-1]["event"] == "collect.completed"


@pytest.mark.asyncio
async def test_jwt_refresh_failure_exhausts_attempts_raises_collect_failed():
    from southern_company_api.exceptions import CantReachSouthernCompany as JwtCantReach

    api = _FakeApi([JwtCantReach("unreachable"), JwtCantReach("unreachable again")])
    account = _FakeAccount([])

    with pytest.raises(CollectFailed):
        await get_usage_and_billing(account, api, max_login_attempts=2)

    # get_month_data was never reached -- both attempts failed at the jwt-fetch stage.
    assert account.calls_with == []


@pytest.mark.asyncio
async def test_get_usage_and_billing_default_max_login_attempts_is_two():
    """Pins the `max_login_attempts: int = 2` default directly -- every other test passes it
    explicitly, so a mutant changing the default alone would otherwise survive."""
    api = _FakeApi(["t1", "t2"])
    account = _FakeAccount([UsageDataFailure("boom"), UsageDataFailure("boom again")])

    with pytest.raises(CollectFailed):
        await get_usage_and_billing(account, api)  # no max_login_attempts passed

    assert api.jwt_await_count == 2
    assert len(account.calls_with) == 2


@pytest.mark.asyncio
async def test_get_usage_and_billing_exhausted_exception_chains_the_real_last_error():
    """`raise CollectFailed(...) from last_error` -- and `last_error = exc` on the jwt-refresh
    branch specifically (not `get_month_data`'s branch, already covered elsewhere) -- pins that the
    real exception instance is chained, not dropped to `None`."""
    from southern_company_api.exceptions import NoJwtTokenFound

    api = _FakeApi([NoJwtTokenFound("no jwt"), NoJwtTokenFound("still no jwt")])
    account = _FakeAccount([])

    with pytest.raises(CollectFailed) as excinfo:
        await get_usage_and_billing(account, api, max_login_attempts=2)

    assert isinstance(excinfo.value.__cause__, NoJwtTokenFound)


@pytest.mark.asyncio
async def test_get_usage_and_billing_exhausted_message_names_the_attempt_count():
    api = _FakeApi(["t1", "t2", "t3"])
    account = _FakeAccount(
        [UsageDataFailure("a"), UsageDataFailure("b"), UsageDataFailure("c")]
    )

    with pytest.raises(CollectFailed) as excinfo:
        await get_usage_and_billing(account, api, max_login_attempts=3)

    assert (
        str(excinfo.value)
        == "collect.failed: could not collect usage/billing data after 3 attempt(s)"
    )


@pytest.mark.asyncio
async def test_max_login_attempts_of_one_makes_exactly_one_attempt():
    """Pins the `range(1, max_login_attempts + 1)` boundary directly -- with
    `max_login_attempts=1`, exactly one attempt must be made, not zero and not two."""
    api = _FakeApi(["t1"])
    account = _FakeAccount([UsageDataFailure("boom")])

    with pytest.raises(CollectFailed):
        await get_usage_and_billing(account, api, max_login_attempts=1)

    assert api.jwt_await_count == 1
    assert len(account.calls_with) == 1
