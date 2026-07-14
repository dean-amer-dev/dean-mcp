"""VPN dead man's switch tests for secure-search-mcp.

Invariants under test:
  1. _require_vpn() raises immediately when tun0 is absent.
  2. Both search() and read_url() invoke _require_vpn() BEFORE any network I/O.
  3. VPN going down between calls blocks the very next call with no grace period.
  4. VPN state is re-checked on every single call — no caching, no bypass.
  5. Cached URL content is NOT served when the VPN is down.

If any of these invariants breaks, the security guarantee is void.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

import secure_search_mcp.server as srv

VPN_PATH = srv._VPN_IFACE_PATH


# ── helpers ────────────────────────────────────────────────────────────────────

def _mock_search_response(results=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"results": results or []}
    return resp


def _mock_url_response(html="<html><body><h1>Test</h1></body></html>"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.text = html
    return resp


# ── _require_vpn() ─────────────────────────────────────────────────────────────

class TestRequireVpn:
    def test_raises_when_tun0_absent(self):
        with patch("os.path.exists", return_value=False):
            with pytest.raises(RuntimeError, match="VPN gate blocked"):
                srv._require_vpn()

    def test_passes_when_tun0_present(self):
        with patch("os.path.exists", return_value=True):
            srv._require_vpn()  # must not raise

    def test_checks_exact_vpn_path(self):
        """Gate must fire specifically for the VPN interface path."""
        def exists(path):
            return path != VPN_PATH  # everything else present; VPN path absent

        with patch("os.path.exists", side_effect=exists):
            with pytest.raises(RuntimeError, match="VPN gate blocked"):
                srv._require_vpn()

    def test_error_message_names_interface(self):
        """Error message must mention the interface name so failures are debuggable."""
        with patch("os.path.exists", return_value=False):
            with pytest.raises(RuntimeError) as exc_info:
                srv._require_vpn()
        assert srv._VPN_IFACE in str(exc_info.value)


# ── Dead man's switch ──────────────────────────────────────────────────────────

class TestDeadMansSwitch:
    def test_blocks_immediately_when_vpn_drops(self):
        """VPN dropping between calls blocks the very next call with no grace period."""
        vpn_up = [True]

        def exists(path):
            return vpn_up[0] if path == VPN_PATH else True

        with patch("os.path.exists", side_effect=exists):
            srv._require_vpn()       # call 1: VPN up — passes
            vpn_up[0] = False        # VPN drops
            with pytest.raises(RuntimeError, match="VPN gate blocked"):
                srv._require_vpn()   # call 2: immediately blocked

    def test_vpn_check_not_cached(self):
        """VPN state must be re-evaluated on every call, never short-circuited."""
        check_count = [0]

        def tracking_exists(path):
            if path == VPN_PATH:
                check_count[0] += 1
                return True
            return True

        with patch("os.path.exists", side_effect=tracking_exists):
            srv._require_vpn()
            srv._require_vpn()
            srv._require_vpn()

        assert check_count[0] == 3, (
            f"Expected 3 VPN checks (one per call), got {check_count[0]}. "
            "VPN state must not be cached."
        )

    def test_recovery_when_vpn_comes_back(self):
        """Gate reopens once tun0 reappears — not latched in the failed state."""
        vpn_up = [False]

        def exists(path):
            return vpn_up[0] if path == VPN_PATH else True

        with patch("os.path.exists", side_effect=exists):
            with pytest.raises(RuntimeError):
                srv._require_vpn()   # VPN down — blocked
            vpn_up[0] = True
            srv._require_vpn()       # VPN back up — must pass


# ── search() VPN gate ──────────────────────────────────────────────────────────

class TestSearchVpnGate:
    def test_blocked_before_any_network_call(self):
        """search() must not touch the network at all when VPN is down."""
        with patch("os.path.exists", return_value=False), \
             patch("httpx.get") as mock_get:
            with pytest.raises(RuntimeError, match="VPN gate blocked"):
                srv.search("query")
            mock_get.assert_not_called()

    def test_vpn_check_precedes_network(self):
        """search() must verify VPN BEFORE the first httpx call."""
        call_order = []

        def tracking_exists(path):
            if path == VPN_PATH:
                call_order.append("vpn_check")
                return True
            return True

        def tracking_get(*a, **kw):
            call_order.append("http_get")
            return _mock_search_response()

        with patch("os.path.exists", side_effect=tracking_exists), \
             patch("httpx.get", side_effect=tracking_get):
            srv.search("query")

        assert call_order, "No calls recorded"
        assert call_order[0] == "vpn_check", (
            f"First call must be 'vpn_check', got '{call_order[0]}'. "
            "VPN gate is not positioned before the network call."
        )

    def test_dead_mans_switch(self):
        """VPN dropping between search() calls blocks the next call immediately."""
        vpn_up = [True]

        def exists(path):
            return vpn_up[0] if path == VPN_PATH else True

        with patch("os.path.exists", side_effect=exists), \
             patch("httpx.get", return_value=_mock_search_response()):
            srv.search("first query")   # succeeds
            vpn_up[0] = False           # VPN drops

        with patch("os.path.exists", side_effect=exists), \
             patch("httpx.get") as blocked_get:
            with pytest.raises(RuntimeError, match="VPN gate blocked"):
                srv.search("second query")
            blocked_get.assert_not_called()

    def test_succeeds_when_vpn_up(self):
        """search() returns results normally when VPN is active."""
        results = [{"title": "T", "url": "https://example.com", "content": "c", "score": 0.9}]
        with patch("os.path.exists", return_value=True), \
             patch("httpx.get", return_value=_mock_search_response(results)):
            result = srv.search("query")
        assert result["count"] == 1
        assert "error" not in result


# ── read_url() VPN gate ────────────────────────────────────────────────────────

class TestReadUrlVpnGate:
    def test_blocked_before_any_network_call(self):
        """read_url() must not touch the network at all when VPN is down."""
        with patch("os.path.exists", return_value=False), \
             patch("httpx.get") as mock_get:
            with pytest.raises(RuntimeError, match="VPN gate blocked"):
                srv.read_url("https://example.com")
            mock_get.assert_not_called()

    def test_vpn_check_precedes_network(self):
        """read_url() must verify VPN BEFORE the first httpx call."""
        call_order = []

        def tracking_exists(path):
            if path == VPN_PATH:
                call_order.append("vpn_check")
                return True
            return True

        def tracking_get(*a, **kw):
            call_order.append("http_get")
            return _mock_url_response()

        with patch("os.path.exists", side_effect=tracking_exists), \
             patch("httpx.get", side_effect=tracking_get):
            srv.read_url("https://example.com/nocache-" + str(time.time()))

        assert call_order, "No calls recorded"
        assert call_order[0] == "vpn_check", (
            f"First call must be 'vpn_check', got '{call_order[0]}'. "
            "VPN gate is not positioned before the network call."
        )

    def test_cache_bypassed_when_vpn_down(self):
        """read_url() must NOT serve cached content when VPN is down.

        The cache is an optimization, not a data store — serving cached content
        with VPN down would still leak the fact that a request was made.
        """
        url = "https://example.com/cached-test"
        srv._url_cache[url] = (time.monotonic(), "cached content")
        try:
            with patch("os.path.exists", return_value=False):
                with pytest.raises(RuntimeError, match="VPN gate blocked"):
                    srv.read_url(url)
        finally:
            srv._url_cache.pop(url, None)

    def test_dead_mans_switch(self):
        """VPN dropping between read_url() calls blocks the next call immediately."""
        vpn_up = [True]

        def exists(path):
            return vpn_up[0] if path == VPN_PATH else True

        url1 = "https://example.com/url1"
        url2 = "https://example.com/url2"

        with patch("os.path.exists", side_effect=exists), \
             patch("httpx.get", return_value=_mock_url_response()):
            srv.read_url(url1)  # succeeds
            vpn_up[0] = False   # VPN drops

        with patch("os.path.exists", side_effect=exists), \
             patch("httpx.get") as blocked_get:
            with pytest.raises(RuntimeError, match="VPN gate blocked"):
                srv.read_url(url2)
            blocked_get.assert_not_called()

    def test_succeeds_when_vpn_up(self):
        """read_url() returns content normally when VPN is active."""
        url = "https://example.com/unique-" + str(time.time())
        with patch("os.path.exists", return_value=True), \
             patch("httpx.get", return_value=_mock_url_response()):
            result = srv.read_url(url)
        assert "error" not in result
        assert "content" in result
