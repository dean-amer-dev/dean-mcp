"""Tests for komodo-mcp server."""

from unittest.mock import patch

from komodo_mcp.server import list_stacks, get_health, get_stack, run_sync


def test_get_health_healthy():
    with patch("komodo_mcp.server.KOMODO_API_KEY", "test-key"):
        with patch("komodo_mcp.server.KOMODO_API_SECRET", "test-secret"):
            with patch("komodo_mcp.server.httpx.post") as mock_post:
                mock_post.return_value.raise_for_status = lambda: None
                mock_post.return_value.json.return_value = {"result": []}
                result = get_health()
                assert result["status"] == "healthy"


def test_get_health_missing_key():
    with patch("komodo_mcp.server.KOMODO_API_KEY", ""):
        with patch("komodo_mcp.server.KOMODO_API_SECRET", "test-secret"):
            result = get_health()
            assert result["status"] == "unhealthy"
            assert "KOMODO_API_KEY" in result["error"]


def test_list_stacks():
    with patch("komodo_mcp.server.KOMODO_API_KEY", "test-key"):
        with patch("komodo_mcp.server.KOMODO_API_SECRET", "test-secret"):
            with patch("komodo_mcp.server.httpx.post") as mock_post:
                mock_post.return_value.raise_for_status = lambda: None
                mock_post.return_value.json.return_value = {
                    "result": {"stacks": [{"name": "test"}]}
                }
                result = list_stacks()
                assert result["stacks"] == [{"name": "test"}]
