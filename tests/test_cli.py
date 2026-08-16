"""Orchestration tests for `gp_monitor.cli` (Steps 11a + 11b).

Mocks every upstream step (`config.load_config`, `auth.login`, `collect.get_usage_and_billing`,
`publish.push_to_ha`, `metrics.write_metrics`) at the point `cli.py` imports them, so no real
network call, no real credentials, and no real filesystem/HA/metrics side effect. Also mocks the
Step 11b guard machinery -- `Breaker.check_tripped`/`record_failure`/`record_success` (patched at
the class level, same technique `tests/test_breaker.py`'s own tests don't need but Step 11a's
now-obsolete guard-avoidance test used), `scraper_commons.cease.is_halted`, and
`scraper_commons.rate.RateGovernor` -- so no real breaker-state file, cease-registry file, or
real-time sleep ever happens in a test.

Covers:
  (a) full happy-path run with every upstream step + guard mocked -> exit code 0.
  (b) any upstream call raising -> the pipeline catches it and exits 1 (one scenario per step:
      config, auth, collect, publish, metrics).
  (c)-(j) Step 11b's guard-failure scenarios: breaker tripped, cease halted, rate governor
      throttles/permits, auth/collect/publish failures mapped to the correct `FailureReason`, and
      a successful cycle resetting the breaker's counter via `record_success`.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

import gp_monitor.breaker as breaker_module
import gp_monitor.cli as cli_module
import gp_monitor.metrics as metrics_module
import gp_monitor.publish as publish_module
from gp_monitor.auth import AuthFailedBounded
from gp_monitor.breaker import BreakerTripped, FailureReason
from gp_monitor.collect import CollectFailed, UsageAndBilling
from gp_monitor.config import Config
from gp_monitor.publish import HA_TOKEN_ENV_VAR, PublishFailed


def _usage_and_billing() -> UsageAndBilling:
    return UsageAndBilling(
        total_kwh_used=123.4,
        dollars_to_date=45.67,
        period_start="2026-08-01",
        period_end="2026-08-16",
    )


@pytest.fixture
def mocked_pipeline():
    """Patches every upstream step `cli.poll` calls, plus the Step 11b guard machinery, all
    succeeding/permitting by default."""
    with (
        patch.object(cli_module, "load_config", return_value=Config()) as m_config,
        patch.object(breaker_module.Breaker, "check_tripped", return_value=None) as m_check,
        patch.object(breaker_module.Breaker, "record_failure") as m_fail,
        patch.object(breaker_module.Breaker, "record_success") as m_success,
        patch.object(cli_module.cease, "is_halted", return_value=False) as m_cease,
        patch.object(cli_module, "RateGovernor") as m_rate_cls,
        patch.object(cli_module, "login", new_callable=AsyncMock) as m_login,
        patch.object(
            cli_module, "get_usage_and_billing", new_callable=AsyncMock
        ) as m_collect,
        patch.object(cli_module, "push_to_ha", return_value=True) as m_publish,
        patch.object(cli_module, "write_metrics", return_value="/tmp/gp_monitor.prom") as m_metrics,
    ):
        m_rate_instance = MagicMock()
        m_rate_cls.return_value = m_rate_instance
        m_login.return_value = (MagicMock(), MagicMock(), MagicMock())
        m_collect.return_value = _usage_and_billing()
        yield {
            "config": m_config,
            "check_tripped": m_check,
            "record_failure": m_fail,
            "record_success": m_success,
            "cease": m_cease,
            "rate_cls": m_rate_cls,
            "rate": m_rate_instance,
            "login": m_login,
            "collect": m_collect,
            "publish": m_publish,
            "metrics": m_metrics,
        }


# --- Step 11a happy-path / per-step-failure scenarios (updated for 11b's guard mocking) ---------


def test_poll_happy_path_exits_zero(mocked_pipeline):
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 0, result.output
    mocked_pipeline["config"].assert_called_once()
    mocked_pipeline["login"].assert_awaited_once()
    mocked_pipeline["collect"].assert_awaited_once()
    mocked_pipeline["publish"].assert_called_once()
    mocked_pipeline["metrics"].assert_called_once()
    mocked_pipeline["record_success"].assert_called_once()
    mocked_pipeline["record_failure"].assert_not_called()

    # `cli.auth_completed`/`cli.collect_completed` are only emitted from `_login_and_collect` --
    # confirm they're logged with the correct event name (not e.g. a mutated string constant) and
    # carry the run_id.
    events = _stderr_events(result.stderr)
    assert "cli.auth_completed" in events
    assert "cli.collect_completed" in events
    run_id = events["cli.poll_started"]["run_id"]
    assert events["cli.auth_completed"]["run_id"] == run_id
    assert events["cli.collect_completed"]["run_id"] == run_id
    assert events["cli.auth_completed"]["level"] == "info"
    assert events["cli.collect_completed"]["level"] == "info"


def test_poll_happy_path_passes_configured_max_login_attempts_to_login_and_collect(
    mocked_pipeline,
):
    """`login(session, config.max_login_attempts)` / `get_usage_and_billing(account, api,
    max_login_attempts=config.max_login_attempts)` -- pins the exact args passed through, not just
    that the mocks were called at all."""
    custom_config = Config(max_login_attempts=7)
    mocked_pipeline["config"].return_value = custom_config

    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 0, result.output
    login_call = mocked_pipeline["login"].await_args
    assert login_call.args[1] == 7
    collect_call = mocked_pipeline["collect"].await_args
    assert collect_call.kwargs["max_login_attempts"] == 7


def test_poll_dry_run_skips_publish_and_metrics(mocked_pipeline):
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll", "--dry-run"])

    assert result.exit_code == 0, result.output
    mocked_pipeline["login"].assert_awaited_once()
    mocked_pipeline["collect"].assert_awaited_once()
    mocked_pipeline["publish"].assert_not_called()
    mocked_pipeline["metrics"].assert_not_called()
    mocked_pipeline["record_success"].assert_called_once()

    # (c) auth + collect still ran for real (from `--dry-run`'s point of view -- they're mocked
    # here only to avoid a real network call in this unit test, but the pipeline itself invoked
    # them and got back real, non-None values) -- see the dedicated Step 11c test below for the
    # stronger version of this assertion where `push_to_ha`/`write_metrics` are also real.
    assert mocked_pipeline["login"].return_value is not None
    assert mocked_pipeline["collect"].return_value is not None


# --- Step 11c: --dry-run refinement (real publish/metrics, real httpx client) -----------------


def _stderr_events(stderr: str) -> dict[str, dict]:
    """Every JSON log line captured on stderr, keyed by `event` name (last write wins, matching
    `tests/test_log.py::_last_line`'s convention for this repo). `CliRunner.invoke`'s `Result`
    captures stdout/stderr separately (`result.stderr`) -- `log.py` pins its channel to
    `sys.stderr` (never `click.echo`'s stdout), so that's where every `log_event` call lands."""
    events: dict[str, dict] = {}
    for line in stderr.splitlines():
        if line.strip():
            parsed = json.loads(line)
            events[parsed["event"]] = parsed
    return events


@pytest.fixture
def mocked_pipeline_real_downstream(monkeypatch, tmp_path):
    """Like `mocked_pipeline`, but leaves `publish.push_to_ha` and `metrics.write_metrics` as the
    REAL functions (not mocked) -- `httpx.Client` is swapped for one backed by
    `httpx.MockTransport` (same technique as `tests/test_publish.py`) so a real POST attempt is
    observable, and the metrics textfile directory is pointed at `tmp_path` so a real file write is
    observable on disk. Only `load_config`/the guard machinery/`login`/`get_usage_and_billing` are
    mocked, same as `mocked_pipeline`.

    This proves Step 11c's `--dry-run` skip is real -- the pipeline never reaches `push_to_ha`'s
    HTTP call or `write_metrics`'s file write -- not just that a mock function wasn't invoked.
    """
    recorded_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv(HA_TOKEN_ENV_VAR, "test-ha-token")
    monkeypatch.setattr(
        publish_module.httpx,
        "Client",
        lambda *a, **kw: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv(metrics_module.TEXTFILE_DIR_ENV, str(tmp_path))

    with (
        patch.object(cli_module, "load_config", return_value=Config()) as m_config,
        patch.object(breaker_module.Breaker, "check_tripped", return_value=None) as m_check,
        patch.object(breaker_module.Breaker, "record_failure") as m_fail,
        patch.object(breaker_module.Breaker, "record_success") as m_success,
        patch.object(cli_module.cease, "is_halted", return_value=False) as m_cease,
        patch.object(cli_module, "RateGovernor") as m_rate_cls,
        patch.object(cli_module, "login", new_callable=AsyncMock) as m_login,
        patch.object(
            cli_module, "get_usage_and_billing", new_callable=AsyncMock
        ) as m_collect,
    ):
        m_rate_instance = MagicMock()
        m_rate_cls.return_value = m_rate_instance
        m_login.return_value = (MagicMock(), MagicMock(), MagicMock())
        m_collect.return_value = _usage_and_billing()
        yield {
            "config": m_config,
            "check_tripped": m_check,
            "record_failure": m_fail,
            "record_success": m_success,
            "cease": m_cease,
            "login": m_login,
            "collect": m_collect,
            "recorded_requests": recorded_requests,
            "textfile_dir": tmp_path,
        }


def test_poll_dry_run_makes_no_real_ha_post_and_writes_no_metrics_file(
    mocked_pipeline_real_downstream,
):
    """Step 11c (a)+(b)+(c)+(d)+(e), against the REAL `push_to_ha`/`write_metrics` functions:
    (a) no HTTP POST ever reaches the (mock-transported) httpx client used for Home Assistant;
    (b) no metrics textfile is written to disk;
    (c) auth + collect still ran and returned real, non-None values;
    (d) exit code is 0;
    (e) the log stream explicitly marks publish/metrics as `skipped` (outcome="skipped"), and
        never emits the `*_completed` events that a real publish/metrics run would produce.
    """
    fixture = mocked_pipeline_real_downstream
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll", "--dry-run"])

    # (d)
    assert result.exit_code == 0, result.output

    # (c)
    fixture["login"].assert_awaited_once()
    fixture["collect"].assert_awaited_once()
    collect_result = fixture["collect"].return_value
    assert collect_result is not None
    assert collect_result.total_kwh_used is not None
    assert collect_result.dollars_to_date is not None

    # (a) -- the real push_to_ha was never called, so its real httpx client never POSTed.
    assert fixture["recorded_requests"] == []

    # (b) -- the real write_metrics was never called, so no file exists at its real output path.
    metrics_path = os.path.join(str(fixture["textfile_dir"]), metrics_module.METRIC_FILE_NAME)
    assert not os.path.exists(metrics_path)
    assert os.listdir(str(fixture["textfile_dir"])) == []

    # (e)
    events = _stderr_events(result.stderr)
    assert events["cli.publish_skipped"]["outcome"] == "skipped"
    assert events["cli.publish_skipped"]["reason"] == "dry_run"
    assert events["cli.metrics_skipped"]["outcome"] == "skipped"
    assert events["cli.metrics_skipped"]["reason"] == "dry_run"
    assert "cli.publish_completed" not in events
    assert "cli.metrics_completed" not in events

    fixture["record_success"].assert_called_once()
    fixture["record_failure"].assert_not_called()


def test_poll_config_failure_exits_one(mocked_pipeline):
    mocked_pipeline["config"].side_effect = ValueError("config.invalid: boom")
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    mocked_pipeline["login"].assert_not_awaited()
    # Breaker never constructed -- config failed before it could read `max_consecutive_failures`.
    mocked_pipeline["record_failure"].assert_not_called()


def test_poll_metrics_failure_exits_one(mocked_pipeline):
    mocked_pipeline["metrics"].side_effect = RuntimeError("metrics blew up")
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    # `RuntimeError` from metrics.py isn't part of the guard/pipeline closed set -- no
    # `FailureReason` mapping exists for it, so `record_failure` is not called.
    mocked_pipeline["record_failure"].assert_not_called()


def test_version_flag_exits_zero():
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["--version"])

    assert result.exit_code == 0
    assert "gp-monitor" in result.output


# --- Step 11b guard-integration scenarios ---------------------------------------------------


def test_breaker_tripped_aborts_before_login(mocked_pipeline):
    """(a) Breaker already tripped -> cycle aborts before login, exit 1, logged as
    breaker_tripped. `record_failure` is not called (no `FailureReason` for this case)."""
    mocked_pipeline["check_tripped"].side_effect = BreakerTripped(
        5, "invalid_login", "2026-08-16T00:00:00+00:00"
    )
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    mocked_pipeline["login"].assert_not_awaited()
    mocked_pipeline["collect"].assert_not_awaited()
    mocked_pipeline["publish"].assert_not_called()
    mocked_pipeline["record_failure"].assert_not_called()
    mocked_pipeline["record_success"].assert_not_called()


def test_cease_halted_aborts_cycle(mocked_pipeline):
    """(b) cease-fire registry reports georgia_power halted -> cycle aborts, exit 1, logged as
    cease_halted, `record_failure(FailureReason.CEASE_HALTED)`."""
    mocked_pipeline["cease"].return_value = True
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    mocked_pipeline["cease"].assert_called_once_with("georgia_power")
    mocked_pipeline["login"].assert_not_awaited()
    mocked_pipeline["record_failure"].assert_called_once_with(FailureReason.CEASE_HALTED)


def test_rate_governor_throttles_aborts_cycle(mocked_pipeline):
    """(c) Rate governor denies admission (acquire() raises) -> cycle aborts, exit 1, logged as
    rate_limited, `record_failure(FailureReason.RATE_LIMITED)`."""
    mocked_pipeline["rate"].acquire.side_effect = RuntimeError("denied")
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    mocked_pipeline["login"].assert_not_awaited()
    mocked_pipeline["record_failure"].assert_called_once_with(FailureReason.RATE_LIMITED)


def test_rate_governor_permits_pipeline_continues(mocked_pipeline):
    """(d) Rate governor permits (acquire() returns normally) -> pipeline continues, and
    `RateGovernor.acquire()` is invoked exactly once, before the Georgia Power `login()` call."""
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 0, result.output
    mocked_pipeline["rate"].acquire.assert_called_once_with(cli_module._GP_RATE_HOST)
    mocked_pipeline["login"].assert_awaited_once()


def test_auth_failed_bounded_aborts_cycle(mocked_pipeline):
    """(e) Auth fails after exhausting retries -> cycle aborts, exit 1, logged as
    auth_failed_bounded (err_type), `record_failure(FailureReason.INVALID_LOGIN)`."""
    mocked_pipeline["login"].side_effect = AuthFailedBounded("auth.failed_bounded: invalid_login")
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    mocked_pipeline["collect"].assert_not_awaited()
    mocked_pipeline["publish"].assert_not_called()
    mocked_pipeline["record_failure"].assert_called_once_with(FailureReason.INVALID_LOGIN)


def test_collect_failed_aborts_cycle(mocked_pipeline):
    """(f) Collect fails -> cycle aborts, exit 1, logged as collect_failed,
    `record_failure(FailureReason.COLLECT_FAILED)`."""
    mocked_pipeline["collect"].side_effect = CollectFailed("collect.failed: boom")
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    mocked_pipeline["publish"].assert_not_called()
    mocked_pipeline["metrics"].assert_not_called()
    mocked_pipeline["record_failure"].assert_called_once_with(FailureReason.COLLECT_FAILED)


def test_publish_failed_aborts_cycle_last_poll_not_advanced(mocked_pipeline):
    """(g) Publish fails -> cycle aborts, exit 1, logged as publish_failed,
    `record_failure(FailureReason.PUBLISH_FAILED)`. `write_metrics` (which would follow a
    successful publish) is never called, and `push_to_ha` -- the only thing that could advance
    `sensor.georgia_power_last_poll` -- raised before returning, so nothing in `cli.py` treats the
    cycle as having advanced it."""
    mocked_pipeline["publish"].side_effect = PublishFailed("publish.failed: timeout")
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 1
    mocked_pipeline["publish"].assert_called_once()
    mocked_pipeline["metrics"].assert_not_called()
    mocked_pipeline["record_failure"].assert_called_once_with(FailureReason.PUBLISH_FAILED)
    mocked_pipeline["record_success"].assert_not_called()


def test_success_resets_breaker_counter(mocked_pipeline):
    """(h) On a fully successful cycle, `Breaker.record_success()` is called (which resets
    `consecutive_failures` to 0 -- see `tests/test_breaker.py::test_reset_after_success` for the
    breaker-level guarantee)."""
    runner = CliRunner()
    result = runner.invoke(cli_module.main, ["poll"])

    assert result.exit_code == 0, result.output
    mocked_pipeline["record_success"].assert_called_once()
    mocked_pipeline["record_failure"].assert_not_called()


# --- Direct `_login_and_collect` tests -------------------------------------------------------
#
# These bypass the CLI/Click layer and `poll`'s generic `except Exception` (which only logs
# `err_type`, never the raised exception's own message) so the exact args passed to `login`/
# `get_usage_and_billing`, and the exact message text of `RateLimited`/`cease.PlatformHalted`,
# are directly observable.


@pytest.mark.asyncio
async def test_login_and_collect_passes_exact_args_through(monkeypatch):
    config = Config()
    breaker = MagicMock()
    breaker.check_tripped.return_value = None

    sentinel_account = MagicMock(name="account")
    sentinel_api = MagicMock(name="api")
    login_mock = AsyncMock(return_value=(MagicMock(), sentinel_api, sentinel_account))
    collect_mock = AsyncMock(return_value=_usage_and_billing())

    monkeypatch.setattr(cli_module, "login", login_mock)
    monkeypatch.setattr(cli_module, "get_usage_and_billing", collect_mock)
    monkeypatch.setattr(cli_module.cease, "is_halted", lambda *_: False)
    rate_instance = MagicMock()
    monkeypatch.setattr(cli_module, "RateGovernor", lambda: rate_instance)

    result = await cli_module._login_and_collect(config, "run-direct", breaker)

    assert result == collect_mock.return_value
    # login(session, config.max_login_attempts) -- the real aiohttp session and the configured
    # max_login_attempts, not a mutated/dropped arg.
    login_call = login_mock.await_args
    assert login_call.args[1] == config.max_login_attempts
    # get_usage_and_billing(account, api, max_login_attempts=...) -- the exact account/api login()
    # returned, not swapped/dropped.
    collect_call = collect_mock.await_args
    assert collect_call.args[0] is sentinel_account
    assert collect_call.args[1] is sentinel_api
    assert collect_call.kwargs["max_login_attempts"] == config.max_login_attempts
    rate_instance.acquire.assert_called_once_with(cli_module._GP_RATE_HOST)


@pytest.mark.asyncio
async def test_login_and_collect_cease_halted_message_names_the_platform(monkeypatch):
    config = Config()
    breaker = MagicMock()
    breaker.check_tripped.return_value = None
    monkeypatch.setattr(cli_module.cease, "is_halted", lambda *_: True)

    with pytest.raises(cli_module.cease.PlatformHalted) as excinfo:
        await cli_module._login_and_collect(config, "run-direct", breaker)

    assert str(excinfo.value) == "georgia_power is halted, refusing to poll"


@pytest.mark.asyncio
async def test_login_and_collect_rate_limited_message(monkeypatch):
    config = Config()
    breaker = MagicMock()
    breaker.check_tripped.return_value = None
    monkeypatch.setattr(cli_module.cease, "is_halted", lambda *_: False)
    rate_instance = MagicMock()
    rate_instance.acquire.side_effect = RuntimeError("denied")
    monkeypatch.setattr(cli_module, "RateGovernor", lambda: rate_instance)

    with pytest.raises(cli_module.RateLimited) as excinfo:
        await cli_module._login_and_collect(config, "run-direct", breaker)

    assert str(excinfo.value) == "rate governor denied admission for this cycle"
