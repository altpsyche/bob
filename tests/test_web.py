"""web_fetch SSRF guard; non-docker web_search fallback."""
import socket
import unittest
from unittest import mock

import _common
import web


class TestWebFetchGuard(unittest.TestCase):
    def setUp(self):
        web.configure(_common.fake_config())
        self._orig = socket.getaddrinfo

    def tearDown(self):
        socket.getaddrinfo = self._orig
        web._allow_private_fetch = False

    def _fake_resolve(self, ip):
        socket.getaddrinfo = lambda host, *a, **k: [(2, 1, 6, "", (ip, 0))]

    def test_blocks_loopback(self):
        self._fake_resolve("127.0.0.1")
        self.assertTrue(web._is_blocked_host("whatever"))

    def test_blocks_private(self):
        self._fake_resolve("10.1.2.3")
        self.assertTrue(web._is_blocked_host("whatever"))

    def test_allows_public(self):
        self._fake_resolve("93.184.216.34")  # example.com
        self.assertFalse(web._is_blocked_host("example.com"))

    def test_fetch_rejects_non_http_scheme(self):
        out = web._web_fetch("file:///etc/passwd")
        self.assertIn("blocked scheme", out)

    def test_fetch_rejects_private_host(self):
        self._fake_resolve("192.168.0.5")
        out = web._web_fetch("http://intranet.local/secret")
        self.assertIn("blocked host", out)

    def test_fetch_blocks_redirect_to_private(self):
        # a public URL that 302-redirects to a private host must be blocked at the second hop.
        socket.getaddrinfo = lambda host, *a, **k: [
            (2, 1, 6, "", (("93.184.216.34" if host == "public.example" else "10.0.0.5"), 0))
        ]

        class _Redirect:
            is_redirect = True
            headers = {"Location": "http://internal.local/secret"}

        orig_get = web.requests.get
        web.requests.get = lambda *a, **k: _Redirect()
        try:
            out = web._web_fetch("http://public.example/page")
        finally:
            web.requests.get = orig_get
        self.assertIn("blocked host", out)
        self.assertIn("internal.local", out)

    def test_opt_in_allows_private(self):
        web.configure(_common.fake_config(agent={"allowPrivateFetch": True}))
        self._fake_resolve("127.0.0.1")
        # host guard is bypassed; the call will fail on the network, not the guard
        out = web._web_fetch("http://127.0.0.1:9/nothing")
        self.assertNotIn("blocked host", out)


_DDG_HTML = (
    '<a class="result__a" rel="nofollow" '
    'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=x">Example &amp; Title</a>'
    '<a class="result__snippet" href="x">A useful &amp; short snippet</a>'
)


class TestWebSearchFallback(unittest.TestCase):
    """#5b — when SearXNG is unreachable (commonly: no Docker), web_search degrades to a direct
    provider (DuckDuckGo HTML) instead of just failing. Gated by agent.webSearchFallback (default on)."""

    def _resp(self, *, json_data=None, text=None):
        r = mock.Mock()
        r.raise_for_status = lambda: None
        if json_data is not None:
            r.json = lambda: json_data
        if text is not None:
            r.text = text
        return r

    def test_searxng_results_win_and_skip_fallback(self):
        web.configure(_common.fake_config())
        with mock.patch.object(web, "_ensure_searxng", return_value=""), \
             mock.patch.object(web.requests, "get",
                               return_value=self._resp(json_data={"results": [
                                   {"title": "T", "url": "http://u", "content": "c"}]})), \
             mock.patch.object(web.requests, "post",
                               side_effect=AssertionError("fallback must not run")) as post:
            out = web._web_search("q")
        self.assertIn("http://u", out)
        post.assert_not_called()

    def test_falls_back_to_duckduckgo_when_searxng_down(self):
        web.configure(_common.fake_config())
        with mock.patch.object(web, "_ensure_searxng", return_value="Docker not found"), \
             mock.patch.object(web.requests, "get", side_effect=Exception("searxng down")), \
             mock.patch.object(web.requests, "post", return_value=self._resp(text=_DDG_HTML)):
            out = web._web_search("q")
        self.assertIn("DuckDuckGo", out)
        self.assertIn("https://example.com/page", out)   # DDG redirect decoded to the real URL
        self.assertIn("Example & Title", out)            # HTML entities unescaped, tags stripped
        self.assertIn("useful & short snippet", out)

    def test_fallback_disabled_reports_unavailable(self):
        web.configure(_common.fake_config(agent={"webSearchFallback": False}))
        with mock.patch.object(web, "_ensure_searxng", return_value="Docker not found"), \
             mock.patch.object(web.requests, "get", side_effect=Exception("searxng down")), \
             mock.patch.object(web.requests, "post",
                               side_effect=AssertionError("fallback disabled")) as post:
            out = web._web_search("q")
        self.assertIn("web_search unavailable", out)
        self.assertIn("Docker not found", out)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
