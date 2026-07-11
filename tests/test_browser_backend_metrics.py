from types import SimpleNamespace

from src import browser_backend_metrics as metrics
from src.browser_agent import transport


def _upstream_agent_tools():
    return [SimpleNamespace(name=name) for name in transport._AGENT_REMOTE_REQUIRED]


def test_agent_browser_contract_metrics_are_deterministic_and_secure():
    first = metrics.summarize_backend("agent_browser", _upstream_agent_tools(), "1.2.3")
    second = metrics.summarize_backend("agent_browser", _upstream_agent_tools(), "1.2.3")
    assert first == second
    assert first["password_returned_to_model"] is False
    assert first["risk_tools_exposed_to_model"] == {}
    assert "semantic_locator" in first["capabilities"]
    assert "snapshot_diff" in first["capabilities"]
    assert "secure_credential_login" in first["capabilities"]


def test_playwright_baseline_detects_shared_browser_close_and_plaintext_lookup():
    upstream = [
        SimpleNamespace(
            name="browser_snapshot", description="snapshot", inputSchema={}),
        SimpleNamespace(
            name="browser_close", description="close", inputSchema={}),
        SimpleNamespace(
            name="browser_evaluate", description="eval", inputSchema={}),
    ]
    result = metrics.summarize_backend("playwright", upstream, "0.0.0")
    assert result["password_returned_to_model"] is True
    assert result["risk_tools_exposed_to_model"] == {
        "browser_close": "shared_browser_lifecycle",
    }
    assert "browser_evaluate" not in result["risk_tools_exposed_to_model"]


def test_canonical_contract_bytes_ignore_dictionary_insertion_order():
    left = [{"type": "function", "function": {"name": "x", "parameters": {"b": 2, "a": 1}}}]
    right = [{"function": {"parameters": {"a": 1, "b": 2}, "name": "x"}, "type": "function"}]
    assert metrics._canonical_bytes(left) == metrics._canonical_bytes(right)
