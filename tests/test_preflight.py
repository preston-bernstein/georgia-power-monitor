"""Tests for `ops/preflight.py` -- the deploy-time smoke check.

`ops/preflight.py` is a standalone script (not part of the `gp_monitor` package / Click CLI), so
it's loaded directly from its file path via `importlib` rather than imported as a module on
`sys.path`. Uses `httpx.MockTransport` (same pattern as `tests/test_publish.py`) to fake Home
Assistant -- no real network call, and this script must never contact Georgia Power at all.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import httpx
import pytest

_PREFLIGHT_PATH = Path(__file__).resolve().parents[1] / "ops" / "preflight.py"


def _load_preflight_module():
    spec = importlib.util.spec_from_file_location("gp_monitor_ops_preflight", _PREFLIGHT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight_module()

_BASE_URL = "http://10.0.0.5:8123"
_API_URL = f"{_BASE_URL}/api/"
_STATES_URL = f"{_BASE_URL}/api/states"


@pytest.fixture(autouse=True)
def _ha_token(monkeypatch):
    monkeypatch.setenv("HA_LONG_LIVED_TOKEN", "test-ha-token")


def _write_config(tmp_path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(f"ha_base_url: {_BASE_URL}\n")
    return str(path)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_both_checks_pass_returns_true_and_prints_pass(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) in (_API_URL, _STATES_URL)
        return httpx.Response(200, json={"ok": True})

    ok = preflight.run_preflight(_write_config(tmp_path), None, client=_client(handler))

    assert ok is True
    out = capsys.readouterr().out
    assert "PASS: HA reachability" in out
    assert "PASS: HA token validity" in out


def test_reachability_passes_on_401_from_api_root(tmp_path, capsys):
    """A real Home Assistant instance returns 401 for an unauthenticated GET /api/ -- that response
    still proves the host is up and speaking HTTP. Reachability must not require a 2xx status;
    only check (b) (token validity) judges the status code. Regression test: this exact behavior
    was missed by every other check in this repo's pipeline (mocks, mutation testing, code review)
    because none of them exercised a real Home Assistant instance -- only caught by a live
    deploy-time run against the real host."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _API_URL:
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json={"ok": True})

    ok = preflight.run_preflight(_write_config(tmp_path), None, client=_client(handler))

    assert ok is True
    out = capsys.readouterr().out
    assert "PASS: HA reachability" in out
    assert "HTTP 401" in out


def test_ha_unreachable_returns_false_with_clear_message(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    ok = preflight.run_preflight(_write_config(tmp_path), None, client=_client(handler))

    assert ok is False
    out = capsys.readouterr().out
    assert "FAIL: HA reachability" in out
    assert "could not connect" in out


def test_invalid_token_returns_false_with_clear_message_and_never_leaks_body(tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _API_URL:
            return httpx.Response(200, json={"ok": True})
        # A body deliberately shaped to prove it never gets echoed into preflight's own output.
        return httpx.Response(401, json={"message": "unauthorized", "leaked": "should-never-appear"})

    ok = preflight.run_preflight(_write_config(tmp_path), None, client=_client(handler))

    assert ok is False
    out = capsys.readouterr().out
    assert "PASS: HA reachability" in out
    assert "FAIL: HA token validity" in out
    assert "token rejected" in out
    assert "401" in out
    assert "leaked" not in out
    assert "should-never-appear" not in out
    assert "test-ha-token" not in out


def test_missing_token_fails_without_making_a_states_request(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HA_LONG_LIVED_TOKEN", raising=False)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    ok = preflight.run_preflight(_write_config(tmp_path), None, client=_client(handler))

    assert ok is False
    out = capsys.readouterr().out
    assert "FAIL: HA token validity" in out
    assert "no HA_LONG_LIVED_TOKEN" in out
    assert _STATES_URL not in requested


def test_env_file_supplies_token_when_not_in_process_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("HA_LONG_LIVED_TOKEN", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("HA_LONG_LIVED_TOKEN=from-env-file\n")

    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _STATES_URL:
            seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"ok": True})

    ok = preflight.run_preflight(_write_config(tmp_path), str(env_path), client=_client(handler))

    assert ok is True
    assert seen_auth == ["Bearer from-env-file"]


def test_main_returns_0_on_success_and_1_on_failure(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "run_preflight", lambda *a, **k: True)
    assert preflight.main(["--config", "config.yaml"]) == 0
    assert "all checks passed" in capsys.readouterr().out

    monkeypatch.setattr(preflight, "run_preflight", lambda *a, **k: False)
    assert preflight.main(["--config", "config.yaml"]) == 1
    assert "one or more checks failed" in capsys.readouterr().err


def test_never_imports_or_calls_anything_georgia_power_specific():
    """AST-based guard: preflight.py must never import or call anything Georgia-Power-specific --
    it validates Home Assistant only (see its module docstring, which is free to *mention* those
    names in prose explaining what this script deliberately does not do -- that's why this check
    walks the parsed AST rather than grepping raw text)."""
    tree = ast.parse(_PREFLIGHT_PATH.read_text())

    imported_names: set[str] = set()
    called_or_referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module)
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            called_or_referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            called_or_referenced.add(node.attr)

    forbidden_imports = {"southern_company_api", "gp_monitor.auth", "auth"}
    leaked_imports = imported_names & forbidden_imports
    assert not leaked_imports, f"ops/preflight.py must never import: {leaked_imports}"

    forbidden_names = {"SouthernCompanyAPI", "get_month_data", "login", "AuthFailedBounded"}
    leaked_names = called_or_referenced & forbidden_names
    assert not leaked_names, f"ops/preflight.py must never reference: {leaked_names}"
