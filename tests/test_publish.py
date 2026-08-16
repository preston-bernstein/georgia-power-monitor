"""Tests for `gp_monitor.publish` — the Home Assistant REST publisher.

Uses `httpx.MockTransport` (part of core `httpx`, already a pinned dependency — no new dev
dependency needed) to fake the HA server, per the task instructions' preference for
`unittest.mock`/stdlib-adjacent mocking over adding `pytest-httpx`/`respx`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from gp_monitor.config import Config
from gp_monitor.publish import HA_TOKEN_ENV_VAR, PublishFailed, _build_payload, _entity_url, push_to_ha

_USAGE_URL = "http://ha.example.internal:8123/api/states/sensor.georgia_power_usage_kwh"
_BILL_URL = "http://ha.example.internal:8123/api/states/sensor.georgia_power_bill_to_date"
_LAST_POLL_URL = "http://ha.example.internal:8123/api/states/sensor.georgia_power_last_poll"


@pytest.fixture(autouse=True)
def _ha_token(monkeypatch):
    monkeypatch.setenv(HA_TOKEN_ENV_VAR, "test-ha-token")


def _client_with_responses(status_by_url: dict[str, int], recorded: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        status = status_by_url.get(str(request.url), 200)
        if status >= 400:
            # Deliberately include something token-shaped in the body to prove the app never
            # surfaces it in PublishFailed's message or in a log field.
            return httpx.Response(status, json={"message": "forbidden", "leaked": "should-never-appear"})
        return httpx.Response(status, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_push_to_ha_success_publishes_all_three_entities_and_returns_true():
    recorded: list[httpx.Request] = []
    client = _client_with_responses({}, recorded)
    config = Config()

    result = push_to_ha(
        config,
        usage_kwh=412.7,
        dollars_to_date=123.45,
        period_start="2026-08-01",
        period_end="2026-08-16",
        client=client,
    )

    assert result is True
    urls = [str(r.url) for r in recorded]
    assert urls == [_USAGE_URL, _BILL_URL, _LAST_POLL_URL]

    for request in recorded:
        assert request.headers["Authorization"] == "Bearer test-ha-token"
        assert request.headers["Content-Type"] == "application/json"

    usage_body = json.loads(recorded[0].content)
    assert usage_body["state"] == "412.7"
    assert usage_body["attributes"]["unit_of_measurement"] == "kWh"
    assert usage_body["attributes"]["device_class"] == "energy"
    assert usage_body["attributes"]["state_class"] == "total_increasing"
    assert usage_body["attributes"]["period_start"] == "2026-08-01"
    assert usage_body["attributes"]["period_end"] == "2026-08-16"
    assert usage_body["attributes"]["friendly_name"] == config.friendly_name_usage

    bill_body = json.loads(recorded[1].content)
    assert bill_body["state"] == "123.45"
    assert bill_body["attributes"]["unit_of_measurement"] == "USD"
    assert bill_body["attributes"]["device_class"] == "monetary"
    assert "state_class" not in bill_body["attributes"]
    assert bill_body["attributes"]["friendly_name"] == config.friendly_name_bill
    assert bill_body["attributes"]["period_start"] == "2026-08-01"
    assert bill_body["attributes"]["period_end"] == "2026-08-16"

    last_poll_body = json.loads(recorded[2].content)
    assert last_poll_body["attributes"]["friendly_name"] == config.friendly_name_last_poll
    assert last_poll_body["attributes"]["period_start"] == "2026-08-01"
    assert last_poll_body["attributes"]["period_end"] == "2026-08-16"
    assert last_poll_body["attributes"]["device_class"] == "timestamp"
    assert "unit_of_measurement" not in last_poll_body["attributes"]
    assert "state_class" not in last_poll_body["attributes"]
    # `now=` wasn't passed -- this exercises the real `datetime.now(UTC)` path and must produce a
    # timezone-aware timestamp (pins `UTC` against a mutant swapping in `None`/naive local time).
    parsed_timestamp = datetime.fromisoformat(last_poll_body["state"])
    assert parsed_timestamp.tzinfo is not None


def test_push_to_ha_one_entity_4xx_raises_publish_failed_and_does_not_leak_body():
    recorded: list[httpx.Request] = []
    client = _client_with_responses({_BILL_URL: 401}, recorded)
    config = Config()

    with pytest.raises(PublishFailed) as excinfo:
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
        )

    message = str(excinfo.value)
    assert "401" in message
    assert "leaked" not in message
    assert "should-never-appear" not in message
    assert "test-ha-token" not in message

    # Partial failure: usage was posted (first in sequence), bill failed, and the cycle must never
    # reach sensor.georgia_power_last_poll -- that's the "staleness stays visible" contract.
    urls = [str(r.url) for r in recorded]
    assert urls == [_USAGE_URL, _BILL_URL]
    assert _LAST_POLL_URL not in urls


def test_push_to_ha_last_poll_state_is_iso_formatted():
    recorded: list[httpx.Request] = []
    client = _client_with_responses({}, recorded)
    config = Config()
    fixed_now = datetime(2026, 8, 16, 12, 34, 56, tzinfo=UTC)

    push_to_ha(
        config,
        usage_kwh=412.7,
        dollars_to_date=123.45,
        period_start="2026-08-01",
        period_end="2026-08-16",
        client=client,
        now=fixed_now,
    )

    last_poll_body = json.loads(recorded[-1].content)
    state = last_poll_body["state"]
    # Must parse as a real ISO-8601 timestamp and round-trip to the fixed value passed in.
    parsed = datetime.fromisoformat(state)
    assert parsed == fixed_now
    assert last_poll_body["attributes"]["device_class"] == "timestamp"


def test_push_to_ha_advance_last_poll_false_skips_last_poll_entity():
    recorded: list[httpx.Request] = []
    client = _client_with_responses({}, recorded)
    config = Config()

    result = push_to_ha(
        config,
        usage_kwh=412.7,
        dollars_to_date=123.45,
        period_start="2026-08-01",
        period_end="2026-08-16",
        client=client,
        advance_last_poll=False,
    )

    assert result is True
    urls = [str(r.url) for r in recorded]
    assert urls == [_USAGE_URL, _BILL_URL]
    assert _LAST_POLL_URL not in urls


def test_push_to_ha_missing_token_raises_publish_failed(monkeypatch, capsys):
    monkeypatch.delenv(HA_TOKEN_ENV_VAR, raising=False)
    recorded: list[httpx.Request] = []
    client = _client_with_responses({}, recorded)
    config = Config()

    with pytest.raises(PublishFailed) as excinfo:
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
            run_id="run-missing",
        )

    assert str(excinfo.value) == "publish.failed: missing_token"
    assert recorded == []
    lines = _last_log_lines(capsys)
    assert len(lines) == 1
    assert lines[0]["level"] == "error"
    assert lines[0]["event"] == "publish.failed"
    assert lines[0]["reason"] == "missing_token"
    assert lines[0]["run_id"] == "run-missing"


def test_entity_url_strips_trailing_slash_from_base_url():
    """Pins `.rstrip('/')` -- a base URL with a trailing slash must not produce a double slash."""
    assert (
        _entity_url("http://ha.example.internal:8123/", "sensor.foo")
        == "http://ha.example.internal:8123/api/states/sensor.foo"
    )
    assert (
        _entity_url("http://ha.example.internal:8123", "sensor.foo")
        == "http://ha.example.internal:8123/api/states/sensor.foo"
    )


def test_build_payload_omits_none_fields_and_includes_provided_ones():
    payload = _build_payload(
        state="123",
        friendly_name="Test Sensor",
        period_start="2026-08-01",
        period_end="2026-08-16",
        unit=None,
        device_class="timestamp",
        state_class=None,
    )
    assert payload["state"] == "123"
    assert payload["attributes"]["friendly_name"] == "Test Sensor"
    assert payload["attributes"]["period_start"] == "2026-08-01"
    assert payload["attributes"]["period_end"] == "2026-08-16"
    assert payload["attributes"]["device_class"] == "timestamp"
    assert "unit_of_measurement" not in payload["attributes"]
    assert "state_class" not in payload["attributes"]


def test_build_payload_includes_unit_when_given():
    payload = _build_payload(
        state="1.0",
        friendly_name="x",
        period_start="a",
        period_end="b",
        unit="kWh",
        device_class=None,
        state_class=None,
    )
    assert payload["attributes"]["unit_of_measurement"] == "kWh"
    assert "device_class" not in payload["attributes"]


@pytest.mark.parametrize("status", [200, 201, 299])
def test_push_to_ha_accepts_every_2xx_status(status):
    recorded: list[httpx.Request] = []
    client = _client_with_responses({_USAGE_URL: status, _BILL_URL: status}, recorded)
    config = Config()

    result = push_to_ha(
        config,
        usage_kwh=412.7,
        dollars_to_date=123.45,
        period_start="2026-08-01",
        period_end="2026-08-16",
        client=client,
    )
    assert result is True


@pytest.mark.parametrize("status", [199, 300, 404])
def test_push_to_ha_rejects_status_codes_outside_2xx_boundary(status):
    """Pins the `200 <= status_code < 300` boundary exactly -- 199 and 300 must both fail even
    though they're adjacent to valid values."""
    recorded: list[httpx.Request] = []
    client = _client_with_responses({_USAGE_URL: status}, recorded)
    config = Config()

    with pytest.raises(PublishFailed) as excinfo:
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
        )
    assert str(status) in str(excinfo.value)


def _last_log_lines(capsys) -> list[dict]:
    captured = capsys.readouterr()
    return [json.loads(line) for line in captured.err.splitlines() if line.strip()]


def test_push_to_ha_http_error_log_line_carries_full_field_set(capsys):
    recorded: list[httpx.Request] = []
    client = _client_with_responses({_BILL_URL: 503}, recorded)
    config = Config()

    with pytest.raises(PublishFailed):
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
            run_id="run-abc",
        )

    lines = _last_log_lines(capsys)
    failed_line = lines[-1]
    assert failed_line["event"] == "publish.failed"
    assert failed_line["reason"] == "http_error"
    assert failed_line["entity_id"] == "sensor.georgia_power_bill_to_date"
    assert failed_line["status_code"] == 503
    assert failed_line["run_id"] == "run-abc"
    assert failed_line["level"] == "error"


def test_push_to_ha_last_poll_entity_failure_carries_the_same_run_id(capsys):
    """The `run_id` passed to `push_to_ha` must be threaded through to the *last* `_post_entity`
    call (`sensor.georgia_power_last_poll`) too, not just the first two -- only observable by
    making that specific POST fail and reading its own log line's `run_id`."""
    recorded: list[httpx.Request] = []
    client = _client_with_responses({_LAST_POLL_URL: 500}, recorded)
    config = Config()

    with pytest.raises(PublishFailed):
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
            run_id="run-last-poll",
        )

    lines = _last_log_lines(capsys)
    failed_line = lines[-1]
    assert failed_line["entity_id"] == "sensor.georgia_power_last_poll"
    assert failed_line["run_id"] == "run-last-poll"


def test_push_to_ha_timeout_log_line_carries_reason_entity_and_run_id(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("connect timeout")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = Config()

    with pytest.raises(PublishFailed):
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
            run_id="run-xyz",
        )

    lines = _last_log_lines(capsys)
    failed_line = lines[-1]
    assert failed_line["event"] == "publish.failed"
    assert failed_line["reason"] == "timeout"
    assert failed_line["entity_id"] == "sensor.georgia_power_usage_kwh"
    assert failed_line["run_id"] == "run-xyz"
    assert failed_line["level"] == "error"


def test_push_to_ha_request_error_log_line_carries_reason_entity_and_run_id(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = Config()

    with pytest.raises(PublishFailed) as excinfo:
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
            run_id="run-req",
        )

    assert "request_error" in str(excinfo.value)
    assert "sensor.georgia_power_usage_kwh" in str(excinfo.value)

    lines = _last_log_lines(capsys)
    failed_line = lines[-1]
    assert failed_line["event"] == "publish.failed"
    assert failed_line["reason"] == "request_error"
    assert failed_line["run_id"] == "run-req"
    assert failed_line["level"] == "error"
    assert failed_line["entity_id"] == "sensor.georgia_power_usage_kwh"


def test_push_to_ha_success_log_line_carries_run_id(capsys):
    recorded: list[httpx.Request] = []
    client = _client_with_responses({}, recorded)
    config = Config()

    push_to_ha(
        config,
        usage_kwh=412.7,
        dollars_to_date=123.45,
        period_start="2026-08-01",
        period_end="2026-08-16",
        client=client,
        run_id="run-success",
    )

    lines = _last_log_lines(capsys)
    assert lines[-1]["event"] == "publish.succeeded"
    assert lines[-1]["run_id"] == "run-success"
    assert lines[-1]["level"] == "info"


def test_push_to_ha_does_not_close_a_caller_provided_client():
    """`owns_client = client is None` -- when the caller passes its own client, `push_to_ha` must
    never close it (the caller owns its lifecycle). Only observable by checking the client is still
    usable/open afterward, not by checking return value or requests made."""
    recorded: list[httpx.Request] = []
    client = _client_with_responses({}, recorded)
    config = Config()

    push_to_ha(
        config,
        usage_kwh=412.7,
        dollars_to_date=123.45,
        period_start="2026-08-01",
        period_end="2026-08-16",
        client=client,
    )

    assert client.is_closed is False


def test_push_to_ha_closes_its_own_internally_created_client(monkeypatch):
    """When no `client=` is passed, `push_to_ha` builds and must close its own short-lived
    `httpx.Client` -- pins `owns_client` being truthy in that path via a spy on `httpx.Client`."""
    from gp_monitor import publish as publish_module

    created_clients = []
    real_client_cls = httpx.Client

    def spy_client_cls(*args, **kwargs):
        instance = real_client_cls(
            *args, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), **{
                k: v for k, v in kwargs.items() if k != "transport"
            }
        )
        created_clients.append(instance)
        return instance

    monkeypatch.setattr(publish_module.httpx, "Client", spy_client_cls)
    config = Config()

    push_to_ha(
        config,
        usage_kwh=412.7,
        dollars_to_date=123.45,
        period_start="2026-08-01",
        period_end="2026-08-16",
    )

    assert len(created_clients) == 1
    assert created_clients[0].is_closed is True


def test_post_entity_passes_the_configured_timeout(monkeypatch):
    """`client.post(..., timeout=_TIMEOUT_SECONDS)` -- pins the actual timeout kwarg value, which
    `httpx.MockTransport` itself doesn't enforce/observe, via a spy on `client.post`."""
    from gp_monitor import publish as publish_module

    recorded_kwargs = {}
    real_client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    original_post = real_client.post

    def spy_post(*args, **kwargs):
        recorded_kwargs.update(kwargs)
        return original_post(*args, **kwargs)

    monkeypatch.setattr(real_client, "post", spy_post)
    config = Config()

    push_to_ha(
        config,
        usage_kwh=1.0,
        dollars_to_date=1.0,
        period_start="2026-08-01",
        period_end="2026-08-16",
        client=real_client,
    )

    assert recorded_kwargs["timeout"] == publish_module._TIMEOUT_SECONDS


def test_push_to_ha_timeout_raises_publish_failed_without_leaking_exception_text():
    # The raw exception text is deliberately made to look like it contains a credential fragment,
    # to prove PublishFailed's message never echoes the underlying exception's str().
    raw_exception_text = "connect timeout to ha.example.internal:8123 with token abc123"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException(raw_exception_text)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = Config()

    with pytest.raises(PublishFailed) as excinfo:
        push_to_ha(
            config,
            usage_kwh=412.7,
            dollars_to_date=123.45,
            period_start="2026-08-01",
            period_end="2026-08-16",
            client=client,
        )

    message = str(excinfo.value)
    assert "timeout" in message  # closed-set reason code, not the raw exception text
    assert "abc123" not in message
    assert raw_exception_text not in message
