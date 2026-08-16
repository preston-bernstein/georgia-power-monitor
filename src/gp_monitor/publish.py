"""Home Assistant publisher — POSTs usage/billing state to HA's REST API.

Follows plan.md's API/interface contract exactly: ``POST <ha_base_url>/api/states/<entity_id>``
with a Bearer token (from the process environment, never config.yaml — see ``config.py``'s module
docstring) and a ``{"state": ..., "attributes": {...}}`` JSON body. Publishes three entities per
cycle:

* ``sensor.georgia_power_usage_kwh`` — state = ``total_kwh_used``, unit ``kWh``, device_class
  ``energy``, state_class ``total_increasing``.
* ``sensor.georgia_power_bill_to_date`` — state = ``dollars_to_date``, unit ``USD``, device_class
  ``monetary`` (no ``state_class`` — a running dollar balance isn't a strictly-increasing meter).
* ``sensor.georgia_power_last_poll`` — state = an ISO-8601 UTC timestamp, device_class
  ``timestamp``. Only POSTed once the usage and bill entities have both succeeded, and only when
  the caller asks for it (``advance_last_poll=True``, the default) — this is the mechanism that
  keeps a partial cycle from ever looking complete in Home Assistant: if either upstream POST
  fails, `push_to_ha` raises `PublishFailed` before ever touching `sensor.georgia_power_last_poll`,
  so that entity's timestamp simply stops moving and staleness is visible in HA (plan.md).

Security (plan.md's Data model section, fleet-wide): the HA long-lived Bearer token must never be
logged, and neither `PublishFailed`'s message nor any `log_event` field here ever carries raw
exception text or an HTTP response body — both could echo the token back. Only closed-set
diagnostic reason codes (``missing_token``, ``timeout``, ``request_error``, ``http_error``) and
plain HTTP status codes are used.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Config
from .log import log_event

# Read directly from the process environment (systemd `EnvironmentFile=`), never through
# config.py's `load_config` — see config.py's module docstring for why the two are kept apart.
HA_TOKEN_ENV_VAR = "HA_LONG_LIVED_TOKEN"

_TIMEOUT_SECONDS = 10.0


class PublishFailed(Exception):
    """Raised when a publish cycle could not push an entity to Home Assistant.

    The message carries only a closed-set diagnostic reason code and/or a bare HTTP status code —
    never the underlying exception's `str()` or an HTTP response body, either of which could echo
    the HA Bearer token back (plan.md's Data model section). Callers must not append those to this
    message either.
    """


def _entity_url(base_url: str, entity_id: str) -> str:
    return f"{base_url.rstrip('/')}/api/states/{entity_id}"


def _build_payload(
    *,
    state: str,
    friendly_name: str,
    period_start: str,
    period_end: str,
    unit: str | None,
    device_class: str | None,
    state_class: str | None,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "friendly_name": friendly_name,
        "period_start": period_start,
        "period_end": period_end,
    }
    if unit is not None:
        attributes["unit_of_measurement"] = unit
    if device_class is not None:
        attributes["device_class"] = device_class
    if state_class is not None:
        attributes["state_class"] = state_class
    return {"state": state, "attributes": attributes}


def _post_entity(
    client: httpx.Client,
    base_url: str,
    token: str,
    entity_id: str,
    payload: dict[str, Any],
    *,
    run_id: str | None,
) -> None:
    url = _entity_url(base_url, entity_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = client.post(url, json=payload, headers=headers, timeout=_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        log_event("error", "publish.failed", reason="timeout", entity_id=entity_id, run_id=run_id)
        raise PublishFailed(f"publish.failed: timeout POSTing to {entity_id}") from None
    except httpx.HTTPError:
        log_event(
            "error", "publish.failed", reason="request_error", entity_id=entity_id, run_id=run_id
        )
        raise PublishFailed(f"publish.failed: request_error POSTing to {entity_id}") from None

    if not (200 <= response.status_code < 300):
        log_event(
            "error",
            "publish.failed",
            reason="http_error",
            entity_id=entity_id,
            status_code=response.status_code,
            run_id=run_id,
        )
        raise PublishFailed(
            f"publish.failed: HTTP {response.status_code} POSTing to {entity_id}"
        )


def push_to_ha(
    config: Config,
    usage_kwh: float,
    dollars_to_date: float,
    period_start: str,
    period_end: str,
    *,
    advance_last_poll: bool = True,
    run_id: str | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> bool:
    """Publish one poll cycle's usage + bill entities to Home Assistant.

    Posts ``sensor.georgia_power_usage_kwh`` and ``sensor.georgia_power_bill_to_date`` first. Only
    if both succeed — and only when `advance_last_poll` is True (the default) — does it then post
    ``sensor.georgia_power_last_poll`` with the current UTC time (or `now`, if given, for tests) as
    an ISO-8601 string. Entity IDs and friendly names come from `config`, never hardcoded, so a
    `config.yaml` override (validated by `config.py`) is honored.

    Raises `PublishFailed` — never returns False — if the HA long-lived token is missing from the
    environment, or if any POST times out, errors, or returns a non-2xx status. In every failure
    case, `sensor.georgia_power_last_poll` is left untouched: callers (cli.py) are expected to
    catch `PublishFailed`, mark the cycle `partial_failure`, and move on — this function never
    silently advances "last successful poll" on a partial cycle.

    `client` lets a caller (tests, or a future long-lived daemon) supply its own `httpx.Client`
    (e.g. one built on `httpx.MockTransport`); when omitted, a short-lived client is created and
    closed for this call only.
    """
    token = os.environ.get(HA_TOKEN_ENV_VAR)
    if not token:
        log_event("error", "publish.failed", reason="missing_token", run_id=run_id)
        raise PublishFailed("publish.failed: missing_token")

    owns_client = client is None
    http_client = client if client is not None else httpx.Client()
    try:
        usage_payload = _build_payload(
            state=str(usage_kwh),
            friendly_name=config.friendly_name_usage,
            period_start=period_start,
            period_end=period_end,
            unit="kWh",
            device_class="energy",
            state_class="total_increasing",
        )
        bill_payload = _build_payload(
            state=str(dollars_to_date),
            friendly_name=config.friendly_name_bill,
            period_start=period_start,
            period_end=period_end,
            unit="USD",
            device_class="monetary",
            state_class=None,
        )

        _post_entity(
            http_client, config.ha_base_url, token, config.entity_id_usage, usage_payload,
            run_id=run_id,
        )
        _post_entity(
            http_client, config.ha_base_url, token, config.entity_id_bill, bill_payload,
            run_id=run_id,
        )

        if advance_last_poll:
            timestamp = (now if now is not None else datetime.now(UTC)).isoformat()
            last_poll_payload = _build_payload(
                state=timestamp,
                friendly_name=config.friendly_name_last_poll,
                period_start=period_start,
                period_end=period_end,
                unit=None,
                device_class="timestamp",
                state_class=None,
            )
            _post_entity(
                http_client,
                config.ha_base_url,
                token,
                config.entity_id_last_poll,
                last_poll_payload,
                run_id=run_id,
            )

        log_event("info", "publish.succeeded", run_id=run_id)
        return True
    finally:
        if owns_client:
            http_client.close()
