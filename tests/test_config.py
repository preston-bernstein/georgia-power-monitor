"""Config-loading tests for `gp_monitor.config`.

Mirrors `internal-monitor-service/tests/test_config.py`'s coverage of the shared `fleet_logging.load_config`
mechanism (missing-file/malformed-file behavior, env_prefix, `_NO_DOTENV` guard), plus this repo's
own validation rules (positive numeric fields, `ha_base_url` hostname allowlist, entity_id format)
called out in plan.md's Integration points / config.example.yaml section.
"""

from __future__ import annotations

import json
from pathlib import Path

import fleet_logging.config
import pytest
import yaml

from gp_monitor.config import Config, load_config


def _write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


def test_missing_file_falls_back_to_defaults_and_warns(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.yaml"
    cfg = load_config(str(missing))
    assert cfg == Config()
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines, "expected a config.missing warning on stderr"
    line = json.loads(lines[-1])
    assert line["level"] == "warn"
    assert line["service"] == "gp-monitor"
    assert line["event"] == "config.missing"
    assert line["path"] == str(missing.resolve())
    # The line must never land on stdout (see config.py module docstring).
    assert captured.out == ""


def test_valid_overrides_load_successfully(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "ha_base_url": "http://127.0.0.1:8123",
            "poll_interval_seconds": 3600,
            "max_login_attempts": 3,
            "max_consecutive_failures": 10,
        },
    )
    cfg = load_config(path)
    assert cfg.ha_base_url == "http://127.0.0.1:8123"
    assert cfg.poll_interval_seconds == 3600
    assert cfg.max_login_attempts == 3
    assert cfg.max_consecutive_failures == 10


@pytest.mark.parametrize(
    "field_name",
    ["poll_interval_seconds", "max_login_attempts", "max_consecutive_failures"],
)
@pytest.mark.parametrize("bad_value", [0, -1, -100])
def test_numeric_fields_reject_non_positive(tmp_path, field_name, bad_value):
    path = _write_config(tmp_path, {field_name: bad_value})
    with pytest.raises(ValueError, match="must be a positive integer"):
        load_config(path)


def test_direct_construction_also_validates():
    """`Config.__post_init__` runs regardless of construction path -- `fleet_logging.load_config`
    constructs `Config` via a plain `Config(**kwargs)` call, so direct construction must behave
    identically. Guards against the validation silently not firing under the real load path."""
    with pytest.raises(ValueError, match="must be a positive integer"):
        Config(max_consecutive_failures=0)


@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "ftp://127.0.0.1:8123",
        "http://",
        "http://example.com",  # public host -- must not exfiltrate the HA Bearer token
        "https://georgia-power-attacker.example.net:8123",
    ],
)
def test_ha_base_url_rejects_disallowed_values(tmp_path, bad_url):
    path = _write_config(tmp_path, {"ha_base_url": bad_url})
    with pytest.raises(ValueError, match="ha_base_url"):
        load_config(path)


@pytest.mark.parametrize(
    "good_url",
    [
        "http://10.20.30.40:8123",
        "http://lan.example.internal:8123",
        "http://127.0.0.1:8123",
        "http://localhost:8123",
        "http://homeassistant.local:8123",
        "https://172.16.0.5:8123",
    ],
)
def test_ha_base_url_accepts_private_and_local_hosts(tmp_path, good_url):
    path = _write_config(tmp_path, {"ha_base_url": good_url})
    cfg = load_config(path)
    assert cfg.ha_base_url == good_url


@pytest.mark.parametrize(
    "field_name,bad_value",
    [
        ("entity_id_usage", "Not.Valid"),
        ("entity_id_usage", "no_domain_separator"),
        ("entity_id_bill", "sensor.$invalid$"),
        ("entity_id_last_poll", ".starts_with_dot"),
    ],
)
def test_entity_id_fields_reject_invalid_values(tmp_path, field_name, bad_value):
    path = _write_config(tmp_path, {field_name: bad_value})
    with pytest.raises(ValueError, match="entity_id"):
        load_config(path)


def test_malformed_yaml_logs_then_raises(tmp_path, capsys):
    bad = tmp_path / "config.yaml"
    bad.write_text("ha_base_url: [unterminated\n  - broken")
    with pytest.raises(yaml.YAMLError):
        load_config(str(bad))
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["level"] == "error"
    assert line["service"] == "gp-monitor"
    assert line["event"] == "config.parse_failed"
    assert line["path"] == str(bad.resolve())


def test_env_override_requires_gp_monitor_prefix(tmp_path, monkeypatch):
    monkeypatch.delenv("POLL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("GP_MONITOR_POLL_INTERVAL_SECONDS", raising=False)
    missing = tmp_path / "does-not-exist.yaml"

    # A bare, unprefixed POLL_INTERVAL_SECONDS must NOT reach this service's config -- avoids an
    # unrelated process's generic env var accidentally overriding a live config value.
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "1")
    cfg = load_config(str(missing))
    assert cfg.poll_interval_seconds == Config().poll_interval_seconds

    # The GP_MONITOR_-prefixed name is honored.
    monkeypatch.setenv("GP_MONITOR_POLL_INTERVAL_SECONDS", "1800")
    cfg2 = load_config(str(missing))
    assert cfg2.poll_interval_seconds == 1800


def test_load_config_never_triggers_dotenv_tree_walk(tmp_path, monkeypatch):
    """`fleet_logging.load_config` calls `load_dotenv()` internally. With no args, that walks
    *up the filesystem tree from wherever the `fleet_logging` package is installed* looking for
    any file named `.env` and silently overlays it into `os.environ` -- exactly the stealth
    tree-walking credential leak `_NO_DOTENV` guards against (this repo's secrets -- GP_USERNAME,
    GP_PASSWORD, HA_LONG_LIVED_TOKEN -- come only from systemd's `EnvironmentFile=`, never dotenv).
    Asserts on the actual mechanism (the argument passed to `load_dotenv`), not just an absence of
    symptoms.
    """
    calls: list[tuple] = []
    real_load_dotenv = fleet_logging.config.load_dotenv

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_load_dotenv(*args, **kwargs)

    monkeypatch.setattr(fleet_logging.config, "load_dotenv", spy)
    missing = tmp_path / "does-not-exist.yaml"
    load_config(str(missing))

    assert len(calls) == 1
    args, kwargs = calls[0]
    dotenv_path = args[0] if args else kwargs.get("dotenv_path")
    assert dotenv_path is not None
    assert not Path(dotenv_path).exists()
